"""Count items that have at least one bitstream in PDF format via the REST API.

ANTI-PATTERN WARNING
--------------------
Counting items with PDF bitstreams via the REST API is significantly slower and
less accurate than a direct SQL query against the DSpace database. For any
non-trivial repository, run SQL against the DSpace DB instead; the authoritative
counts live there. This example is preserved as a working reference for library
patterns (paging, caching, slow-request logging), not as a recommended approach
to this particular problem.

Uses persistent cache (item UUID -> has_pdf) so resumed runs skip already-known items.
Assumes items are immutable; use "force rerun" to re-check everything.
Logs slow requests to help identify heavy endpoints.
"""

import asyncio
import getpass
import os
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from dspace_client import (
    RestPDFCountCache,
    ServerVersionMismatchError,
    create_validated_client,
    show_script_attribution,
)

# DEVELOPER DECLARES: compatible with DSpace 7.6, 8.0, 9.0, 10.0 (REST discovery + item bundles)
TARGET_VERSIONS = ["7.6", "8.0", "9.0", "10.0"]
SCRIPT_AUTHORS = "Bram Luyten (Atmire)"

console = Console()


async def main():
    """Count items with at least one PDF bitstream and print result."""
    show_script_attribution(SCRIPT_AUTHORS, console=console)
    console.print(
        Panel(
            "This script counts items with a PDF bitstream via the REST API.\n"
            "For any repository of non-trivial size, a direct SQL query against\n"
            "the DSpace database is dramatically faster and more accurate.\n"
            "This example is preserved as a reference for library patterns\n"
            "(paging, caching, slow-request logging), not as the recommended\n"
            "way to answer this question. Use SQL if you have DB access.",
            title="Anti-pattern warning",
            border_style="yellow",
            padding=(1, 2),
        )
    )
    console.print("\n[bold cyan]Count items with PDF bitstream (REST, authenticated)[/bold cyan]")
    console.print(
        "[dim]Uses discovery + item bundles/bitstreams. Cache skips known items; "
        "slow requests are logged.[/dim]\n"
    )

    base_url = console.input(
        "[bold cyan]DSpace base URL[/bold cyan] [dim](Enter for https://demo.dspace.org):[/dim] "
    ).strip()
    if not base_url:
        base_url = "https://demo.dspace.org"
        console.print("[dim]→ Using https://demo.dspace.org[/dim]")

    base_url_normalized = base_url.rstrip("/").lower()
    is_demo = "demo.dspace.org" in base_url_normalized
    if is_demo:
        console.print("[dim]Using demo credentials: dspacedemo+admin@gmail.com[/dim]")
        username = "dspacedemo+admin@gmail.com"
        password = "dspace"
    else:
        username = console.input("[bold cyan]Username:[/bold cyan] ").strip()
        password = getpass.getpass("Password: ")

    delay = console.input(
        "[bold cyan]Delay between discovery pages (seconds)[/bold cyan] [dim](Enter for 1.0):[/dim] "
    ).strip()
    delay_between_pages = float(delay) if delay else 1.0
    if delay_between_pages > 0:
        console.print(f"[dim]→ Throttle: {delay_between_pages}s between pages[/dim]")

    cache_dir = os.environ.get("DSPACE_REST_PDF_CACHE_DIR")
    if cache_dir:
        console.print(f"[dim]Cache dir: {cache_dir}[/dim]")
    cache = RestPDFCountCache(base_url=base_url, cache_dir=Path(cache_dir) if cache_dir else None)
    cache.load()
    cached_total, cached_with_pdf = cache.totals()
    if cached_total > 0:
        console.print(
            f"[dim]Cache loaded: {cached_total} items ({cached_with_pdf} with PDF). "
            "Resume will skip these unless you force rerun.[/dim]"
        )

    force_rerun = console.input(
        "[bold cyan]Force rerun (re-check all items, ignore cache)?[/bold cyan] [dim](y/N):[/dim] "
    ).strip().lower() in ("y", "yes")
    if force_rerun:
        console.print("[dim]→ Force rerun: all items will be re-checked.[/dim]")

    slow_requests: list[tuple[str, str, float]] = []
    slow_threshold = 2.0

    def on_slow_request(method: str, endpoint: str, duration: float) -> None:
        slow_requests.append((method, endpoint, duration))

    try:
        auth, client = await create_validated_client(
            base_url=base_url,
            username=username,
            password=password,
            target_versions=TARGET_VERSIONS,
            show_atmire_promo=True,
            slow_request_threshold_seconds=slow_threshold,
            slow_request_callback=on_slow_request,
        )
    except ServerVersionMismatchError as e:
        console.print(f"[red]Server version mismatch: {e}[/red]")
        return
    except Exception as e:
        console.print(f"[red]Authentication failed: {e}[/red]")
        return

    console.print("[green]Authenticated.[/green] Resolving PDF format and counting items…\n")

    start = time.perf_counter()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console,
    ) as progress:
        task_id = progress.add_task("Discovering items...", total=None)

        latest_delay: float | None = None
        latest_page_duration: float | None = None
        courtesy_delay: float | None = getattr(client, "courtesy_delay", None)

        def debug_page_callback(
            page_index: int,
            page_duration: float,
            current_delay: float,
            current_courtesy: float | None,
        ) -> None:
            nonlocal latest_delay, latest_page_duration, courtesy_delay
            latest_delay = current_delay
            latest_page_duration = page_duration
            if current_courtesy is not None:
                courtesy_delay = current_courtesy

        def progress_callback(processed: int, with_pdf: int, total: int | None) -> None:
            without_pdf = processed - with_pdf
            delay_str = f"{latest_delay:.2f}s" if latest_delay is not None else "?"
            page_str = f"{latest_page_duration:.2f}s" if latest_page_duration is not None else "?"
            courtesy_str = f"{courtesy_delay:.2f}s" if courtesy_delay is not None else "?"
            description = (
                f"Items: {processed} | With PDF: {with_pdf} | Without PDF: {without_pdf} "
                f"| Delay: {delay_str} | Last page: {page_str} | Courtesy: {courtesy_str}"
            )
            if total is not None:
                progress.update(task_id, total=total, completed=processed, description=description)
            else:
                progress.update(task_id, completed=processed, description=description)

        result = await client.count_items_with_pdf_bitstream(
            pdf_format_id=None,
            page_size=100,
            delay_between_pages=delay_between_pages,
            delay_between_items=0,
            progress_callback=progress_callback,
            adaptive_delay=True,
            debug_page_callback=debug_page_callback,
            cache=cache,
            force_rerun=force_rerun,
        )

        total_items = result.get("total_items_processed", 0)
        with_pdf = result.get("count", 0)
        without_pdf = total_items - with_pdf
        progress.update(
            task_id,
            total=total_items or None,
            completed=total_items,
            description=(
                f"Done. Items processed: {total_items} | With PDF: {with_pdf} | Without PDF: {without_pdf}"
            ),
        )

    cache.save()
    elapsed = time.perf_counter() - start

    await auth.close()

    if result.get("pdf_format_id") is None:
        console.print("[yellow]PDF format not found in bitstream format registry.[/yellow]")
        console.print("[dim]Check that the server has PDF registered (e.g. id 3).[/dim]")
        return

    console.print("[bold]Result[/bold]")
    console.print(f"  Items with ≥1 PDF bitstream: [bold]{result['count']}[/bold]")
    console.print(f"  Total items processed:       [bold]{result['total_items_processed']}[/bold]")
    console.print(f"  PDF format id used:          {result['pdf_format_id']}")
    console.print(f"  Time:                        {elapsed:.1f}s")
    console.print(f"  Cache saved to:              [dim]{cache.cache_path}[/dim]")

    if slow_requests:
        console.print()
        console.print(
            f"[bold yellow]Slow requests (>{slow_threshold}s)[/bold yellow] — check for patterns:"
        )
        table = Table(show_header=True, header_style="bold")
        table.add_column("Method", style="dim")
        table.add_column("Endpoint", style="dim")
        table.add_column("Duration (s)", justify="right")
        for method, endpoint, duration in sorted(slow_requests, key=lambda x: -x[2])[:50]:
            table.add_row(method, endpoint, f"{duration:.2f}")
        if len(slow_requests) > 50:
            table.caption = f"Showing top 50 of {len(slow_requests)} slow requests"
        console.print(table)


if __name__ == "__main__":
    asyncio.run(main())
