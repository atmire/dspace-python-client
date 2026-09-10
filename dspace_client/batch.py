"""Batch item creation with adaptive concurrency control."""

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import Any

from rich.console import Console
from rich.progress import Progress

from .concurrency import ConcurrencyConfig, ConcurrencyController, PerformanceMetrics
from .core import DSpaceClient

console = Console()


class BatchItemCreator:
    """Creates items in batches with adaptive concurrency control."""

    def __init__(
        self,
        client: DSpaceClient,
        config: ConcurrencyConfig | None = None,
    ):
        """
        Initialize batch item creator.

        Args:
            client: DSpace API client
            config: Concurrency configuration
        """
        self.client = client
        self.config = config or ConcurrencyConfig()
        self.controller = ConcurrencyController(self.config)

        # Track created items for progress
        self.created_items: list[dict] = []
        self.created_bundles: list[dict] = []
        self.created_bitstreams: list[dict] = []

    async def create_items_batch(
        self,
        collection_uuids: list[str],
        item_data: list[dict[str, Any]],
        progress: Progress | None = None,
        task_id: int | None = None,
        on_metrics_sample: Callable[[int, int, PerformanceMetrics], None | Awaitable[None]]
        | None = None,
    ) -> tuple[list[dict], list[dict], list[dict]]:
        """
        Create items in batches with adaptive concurrency.

        Args:
            collection_uuids: List of collection UUIDs to create items in
            item_data: List of item data dictionaries with keys:
                - title: Item title
                - metadata: Optional metadata dictionary
                - content: Optional binary content for bitstream
                - filename: Optional filename for bitstream
            progress: Rich progress bar (optional)
            task_id: Progress task ID (optional)
            on_metrics_sample: Optional callback ``(completed, total, metrics)`` invoked whenever
                progress metrics are logged (every 50 completions and at the end). May be async.

        Returns:
            Tuple of (created_items, created_bundles, created_bitstreams)
        """
        total_items = len(item_data)
        batch_size = 20  # Process in chunks of 20 for concurrency adjustment

        # Print through the live progress's console when one is supplied, so adaptive
        # readouts integrate with the progress bar instead of fighting a second console.
        out = progress.console if progress is not None else console

        out.print(
            f"\n[cyan]Starting batch creation of {total_items} items with adaptive concurrency[/cyan]"
        )
        out.print(
            f"[dim]Initial concurrency: {self.config.initial}, "
            f"range: {self.config.min_concurrency}-{self.config.max_concurrency} "
            f"(ramps up as latency stays healthy)[/dim]"
        )

        # Narrate concurrency changes as they happen, via the same console as the progress bar.
        def _narrate_adjust(old_limit: int, new_limit: int, reason: str) -> None:
            if new_limit > old_limit:
                out.print(
                    f"[green]▲ Concurrency {old_limit} → {new_limit}[/green] [dim]({reason})[/dim]"
                )
            else:
                out.print(
                    f"[yellow]▼ Concurrency {old_limit} → {new_limit}[/yellow] [dim]({reason})[/dim]"
                )

        self.controller.on_adjust = _narrate_adjust

        # Clear previous results
        self.created_items.clear()
        self.created_bundles.clear()
        self.created_bitstreams.clear()

        # Build lightweight specs; coroutines are created lazily per chunk (inside the
        # semaphore) so a cancel/Ctrl-C never leaves un-awaited coroutines behind.
        specs: list[tuple[dict[str, Any], str]] = []
        for idx, item_info in enumerate(item_data):
            collection_uuid = collection_uuids[idx % len(collection_uuids)]
            specs.append((item_info, collection_uuid))

        # Process specs in batches
        completed = 0
        for i in range(0, len(specs), batch_size):
            batch_specs = specs[i : i + batch_size]

            # Execute batch with concurrency control
            batch_results = await self._execute_batch_with_concurrency(batch_specs)

            # Process results
            for result in batch_results:
                if result["success"]:
                    self.created_items.append(result["item"])
                    self.created_bundles.append(result["bundle"])
                    if result.get("bitstream"):
                        self.created_bitstreams.append(result["bitstream"])
                else:
                    out.print(f"[red]Failed to create item: {result['error']}[/red]")

            completed += len(batch_specs)

            # Update progress
            if progress and task_id is not None:
                progress.update(task_id, advance=len(batch_specs))

            # Show current metrics after each chunk (and at the end)
            await self._show_current_metrics(
                completed, total_items, out=out, on_metrics_sample=on_metrics_sample
            )

        out.print(
            f"[green]✓[/green] Batch creation complete: {len(self.created_items)} items created"
        )
        return self.created_items, self.created_bundles, self.created_bitstreams

    async def _execute_batch_with_concurrency(
        self, specs: list[tuple[dict[str, Any], str]]
    ) -> list[dict]:
        """Execute a batch of item specs with concurrency control."""

        async def execute_with_semaphore(spec: tuple[dict[str, Any], str]):
            item_info, collection_uuid = spec
            duration = 0.0
            # Time only the work itself: the wait for a concurrency slot is queueing time,
            # not server latency, and folding it in would make the controller ramp down in
            # response to its own throttling.
            try:
                async with self.controller.semaphore:
                    start_time = time.time()
                    try:
                        result = await self._create_single_item_with_bitstream(
                            item_info=item_info,
                            collection_uuid=collection_uuid,
                        )
                    finally:
                        duration = time.time() - start_time
                outcome = {"success": True, **result}
            except Exception as e:
                outcome = {"success": False, "error": str(e)}

            # Recorded *after* the slot is released. record_operation() may adjust the
            # concurrency limit, and doing that while still holding a slot is what used to
            # let a ramp-down wedge the whole batch.
            await self.controller.record_operation(duration, success=outcome["success"])
            return outcome

        # Execute all tasks concurrently
        return await asyncio.gather(*[execute_with_semaphore(spec) for spec in specs])

    async def _create_single_item_with_bitstream(
        self,
        item_info: dict[str, Any],
        collection_uuid: str,
    ) -> dict:
        """
        Create a single item with bundle and optional bitstream (atomic operation).

        This is the core operation that gets executed concurrently.
        """
        title = item_info["title"]
        metadata = item_info.get("metadata", {})
        content = item_info.get("content")
        filename = item_info.get("filename", "document.pdf")

        # Create item
        item = await self.client.create_item(
            name=title,
            owning_collection_uuid=collection_uuid,
            metadata=metadata,
        )

        # Create bundle
        bundle = await self.client.create_bundle(item["uuid"], "ORIGINAL")

        result = {
            "item": item,
            "bundle": bundle,
        }

        # Upload bitstream if content provided
        if content:
            bitstream = await self.client.upload_bitstream(
                bundle_uuid=bundle["uuid"],
                filename=filename,
                content=content,
            )
            result["bitstream"] = bitstream

        return result

    async def _show_current_metrics(
        self,
        completed: int,
        total: int,
        *,
        out: Console | None = None,
        on_metrics_sample: Callable[[int, int, PerformanceMetrics], None | Awaitable[None]]
        | None = None,
    ) -> None:
        """Show current performance metrics and optionally notify ``on_metrics_sample``."""
        out = out or console
        metrics = await self.controller.get_metrics()

        # Format metrics
        throughput = f"{metrics.throughput:.1f}" if metrics.throughput > 0 else "0.0"
        p95_latency = f"{metrics.p95_latency * 1000:.0f}ms" if metrics.p95_latency > 0 else "0ms"
        concurrency = metrics.current_concurrency

        # Calculate progress percentage
        progress_pct = (completed / total) * 100 if total > 0 else 0

        out.print(
            f"\n[dim]Progress: {completed}/{total} ({progress_pct:.1f}%) | "
            f"Concurrency: {concurrency} | "
            f"Throughput: {throughput} items/s | "
            f"p95 Latency: {p95_latency}[/dim]"
        )

        if on_metrics_sample:
            result = on_metrics_sample(completed, total, metrics)
            if inspect.isawaitable(result):
                await result

    async def get_final_metrics(self) -> PerformanceMetrics:
        """Get final performance metrics after batch creation."""
        return await self.controller.get_metrics()

    def get_created_counts(self) -> dict[str, int]:
        """Get counts of created objects."""
        return {
            "items": len(self.created_items),
            "bundles": len(self.created_bundles),
            "bitstreams": len(self.created_bitstreams),
        }
