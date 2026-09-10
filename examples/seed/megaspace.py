"""
MegaSpace-style example: groups, EPeople, collections with READ groups, batch items, view events.

Uses ``dspace_client`` only for HTTP/API. Data from ``examples/seed/seedpacks/default.yml`` via
``seed_data.py``. Batch creation uses ``BatchItemCreator`` + ``ConcurrencyConfig`` from the library.

Install deps: ``pip install -e ".[examples]"`` (PyYAML).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from dspace_client import (
    AuthenticationError,
    BatchItemCreator,
    ConcurrencyConfig,
    ServerVersionMismatchError,
    show_script_attribution,
)
from dspace_client.concurrency import PerformanceMetrics
from dspace_client.exceptions import DSpaceAPIError

# DEVELOPER DECLARES: admin-heavy APIs; align with bulk_import
TARGET_VERSIONS = ["9.0", "10.0"]
SCRIPT_AUTHORS = "Bram Luyten (Atmire)"
SLOW_REQUEST_THRESHOLD_SECONDS = 2.0
DIAGNOSTICS_SCHEMA_VERSION = "1.0"
SAMPLE_EVERY_N = 50

_SEED_DIR = Path(__file__).resolve().parent
if str(_SEED_DIR) not in sys.path:
    sys.path.insert(0, str(_SEED_DIR))

from seed_client import connect_seed_client  # noqa: E402
from seed_data import (  # noqa: E402
    DEFAULT_SEED_HTTP_TIMEOUT,
    DataFactory,
    Discipline,
    build_mega_metadata,
    generate_unique_email,
    load_seed_pack,
)

console = Console()
DEFAULT_SEEDPACK = _SEED_DIR / "seedpacks" / "default.yml"
READERS_GROUP_NAME = "MegaSpace Readers"


def _sanitize_hostname(base_url: str) -> str:
    host = urlparse(base_url).hostname or "unknown"
    host = host.lower()
    host = re.sub(r"[^a-z0-9._-]+", "-", host)
    return host.strip("-") or "unknown"


def _diagnostics_basename(base_url: str) -> str:
    """UTC date + hour.minute for filenames (when diagnostics are written)."""
    now = datetime.now(UTC)
    d = now.strftime("%Y-%m-%d")
    t = now.strftime("%H.%M")
    host = _sanitize_hostname(base_url)
    return f"{d}-{t}-megaspace-{host}"


def _compute_degradation_regular_items(samples: list[dict]) -> dict | None:
    ri = [s for s in samples if s.get("phase") == "regular_items"]
    if len(ri) < 2:
        return None
    first, last = ri[0], ri[-1]
    tp0 = float(first.get("throughput") or 0)
    tp1 = float(last.get("throughput") or 0)
    p95_0 = first.get("p95_latency_s")
    p95_1 = last.get("p95_latency_s")
    out: dict[str, float | str | None] = {
        "first_sample_completed": first.get("completed"),
        "last_sample_completed": last.get("completed"),
        "throughput_first": round(tp0, 6),
        "throughput_last": round(tp1, 6),
    }
    if tp0 > 0 and tp1 >= 0:
        out["throughput_last_vs_first_ratio"] = round(tp1 / tp0, 6)
    if isinstance(p95_0, (int, float)) and isinstance(p95_1, (int, float)) and p95_0 > 0:
        out["p95_latency_last_vs_first_ratio"] = round(float(p95_1) / float(p95_0), 6)
    if tp0 > 0 and tp1 < tp0:
        out["note"] = "lower throughput at end vs start (possible degradation)"
    elif tp0 > 0 and tp1 >= tp0:
        out["note"] = "throughput at last sample >= first sample"
    return out


def _build_diagnostics_payload(
    *,
    base_url: str,
    elapsed_seconds: float,
    courtesy_delay: float,
    slow_requests: list[tuple[str, str, float]],
    samples: list[dict],
    seed: int,
    num_collections: int,
    num_items_per_collection: int,
    num_epeople: int,
    num_item_views: int,
    mega_bitstreams: int,
    strict_versions: bool,
    metrics: dict[str, int | str],
) -> dict:
    hostname = _sanitize_hostname(base_url)
    slow_list = [
        {"method": m, "endpoint": ep, "duration_s": round(d, 4)} for m, ep, d in slow_requests
    ]
    degradation = _compute_degradation_regular_items(samples)
    return {
        "schema_version": DIAGNOSTICS_SCHEMA_VERSION,
        "tool": "megaspace",
        "saved_at_utc": datetime.now(UTC).isoformat(),
        "timezone": "UTC",
        "base_url": base_url.rstrip("/"),
        "hostname": hostname,
        "config": {
            "seed": seed,
            "courtesy_delay_s": courtesy_delay,
            "skip_version_check": not strict_versions,
            "num_collections": num_collections,
            "num_items_per_collection": num_items_per_collection,
            "num_epeople": num_epeople,
            "num_item_views": num_item_views,
            "mega_bitstreams_requested": mega_bitstreams,
            "mega_bitstreams_cap": min(mega_bitstreams, 200),
        },
        "slow_request_threshold_seconds": SLOW_REQUEST_THRESHOLD_SECONDS,
        "slow_requests": slow_list,
        "wall_time_content_phase_s": round(elapsed_seconds, 3),
        "metrics": dict(metrics),
        "samples": samples,
        "degradation": degradation,
    }


def _render_readable_markdown(payload: dict) -> str:
    cfg = payload.get("config") or {}
    lines = [
        "# MegaSpace diagnostics",
        "",
        f"- **Host:** `{payload.get('hostname', '')}`",
        f"- **Base URL:** `{payload.get('base_url', '')}`",
        f"- **Saved (UTC):** {payload.get('saved_at_utc', '')}",
        f"- **Schema:** {payload.get('schema_version', '')}",
        "",
        "## Configuration",
        "",
        "| Key | Value |",
        "|-----|-------|",
        f"| seed | {cfg.get('seed')} |",
        f"| courtesy_delay_s | {cfg.get('courtesy_delay_s')} |",
        f"| skip_version_check | {cfg.get('skip_version_check')} |",
        f"| collections | {cfg.get('num_collections')} |",
        f"| items_per_collection | {cfg.get('num_items_per_collection')} |",
        f"| epeople | {cfg.get('num_epeople')} |",
        f"| item_views | {cfg.get('num_item_views')} |",
        f"| mega_bitstreams_cap | {cfg.get('mega_bitstreams_cap')} |",
        "",
        "## Summary",
        "",
        f"- Wall time (content phase after login): **{payload.get('wall_time_content_phase_s')}s**",
        f"- Slow request threshold: **{payload.get('slow_request_threshold_seconds')}s**",
        "",
        "## Degradation (regular_items batch)",
        "",
    ]
    deg = payload.get("degradation")
    if deg:
        lines.append("```json")
        lines.append(json.dumps(deg, indent=2))
        lines.append("```")
    else:
        lines.append(
            "_Not enough `regular_items` samples to compare first vs last (need at least two)._"
        )
        lines.append("")
    lines.extend(
        [
            "",
            "## Time-series samples",
            "",
            f"Total sample rows: **{len(payload.get('samples') or [])}** (phases include epeople, collections, mega_bitstreams, regular_items, item_views, …).",
            "",
            "## Slow requests",
            "",
        ]
    )
    slow = payload.get("slow_requests") or []
    if slow:
        lines.append("| Method | Endpoint | Duration (s) |")
        lines.append("|--------|----------|----------------|")
        for row in sorted(slow, key=lambda x: -x.get("duration_s", 0))[:50]:
            lines.append(
                f"| {row.get('method', '')} | `{row.get('endpoint', '')}` | {row.get('duration_s', '')} |"
            )
        if len(slow) > 50:
            lines.append(f"\n_Showing top 50 of {len(slow)}._")
    else:
        lines.append("_None over threshold._")
    lines.extend(
        [
            "",
            "## Full machine-readable export",
            "",
            "The sibling `*-raw.json` file contains the same payload as below for tools and LLMs.",
            "",
            "```json",
            json.dumps(payload, indent=2),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _write_diagnostics_exports(out_dir: Path, payload: dict) -> tuple[Path, Path]:
    base = _diagnostics_basename(payload["base_url"])
    raw_path = out_dir / f"{base}-raw.json"
    md_path = out_dir / f"{base}-readable.md"
    raw_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path.write_text(_render_readable_markdown(payload), encoding="utf-8")
    return raw_path, md_path


def _append_phase_sample(
    samples: list[dict],
    *,
    phase: str,
    completed: int,
    total: int,
    phase_start: float,
    extra: dict | None = None,
) -> None:
    elapsed = time.perf_counter() - phase_start
    row: dict = {
        "phase": phase,
        "completed": completed,
        "total": total,
        "elapsed_since_phase_start_s": round(elapsed, 3),
    }
    if elapsed > 0 and completed > 0:
        row["throughput_approx_per_s"] = round(completed / elapsed, 4)
    if extra:
        row.update(extra)
    samples.append(row)


async def _ensure_session(auth, client, username: str) -> bool:
    if await auth.is_session_valid():
        return True
    console.print("[yellow]Session expired. Re-authentication required.[/yellow]")
    password = getpass.getpass("Password: ")
    try:
        jwt_token, _status = await auth.authenticate(username, password)
        client.jwt_token = jwt_token
        client.csrf_token = auth.csrf_token
        console.print("[green]Re-authentication successful.[/green]")
        return True
    except AuthenticationError as e:
        console.print(f"[red]Re-authentication failed: {e}[/red]")
        return False


async def run_megaspace(
    *,
    seed_pack_path: Path,
    seed: int,
    base_url: str,
    username: str,
    password: str,
    num_collections: int,
    num_items_per_collection: int,
    num_epeople: int,
    num_item_views: int,
    mega_bitstreams: int,
    strict_versions: bool,
    courtesy_delay: float,
) -> bool:
    if not seed_pack_path.exists():
        console.print(f"[red]Seed pack not found: {seed_pack_path}[/red]")
        return False

    try:
        seed_pack = load_seed_pack(seed_pack_path)
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as e:
        console.print(f"[red]Failed to load seed pack: {e}[/red]")
        return False

    if not strict_versions:
        console.print(
            "[dim]Skipping server version probe (faster; use default behaviour to verify DSpace 9.0).[/dim]"
        )

    slow_requests: list[tuple[str, str, float]] = []

    def on_slow_request(method: str, endpoint: str, duration: float) -> None:
        slow_requests.append((method, endpoint, duration))

    try:
        auth, client = await connect_seed_client(
            base_url=base_url,
            username=username,
            password=password,
            target_versions=TARGET_VERSIONS,
            strict_versions=strict_versions,
            courtesy_delay=courtesy_delay,
            slow_request_threshold_seconds=SLOW_REQUEST_THRESHOLD_SECONDS,
            slow_request_callback=on_slow_request,
        )
    except ServerVersionMismatchError as e:
        console.print(f"[red]Version mismatch:[/red] {e}")
        return False
    except AuthenticationError as e:
        console.print(f"[red]Authentication failed:[/red] {e}")
        err = str(e).lower()
        if "timeout" in err or "readtimeout" in err:
            console.print(
                "[yellow]The server did not respond in time. "
                f"Public demo hosts are often slow; this script uses a {int(DEFAULT_SEED_HTTP_TIMEOUT)}s "
                "HTTP timeout. Retry, or run against a server closer to you.[/yellow]"
            )
        return False

    run_started = time.perf_counter()
    factory = DataFactory(seed_pack, seed=seed)
    discipline = factory.get_first_discipline()

    created_top_community_uuid: str | None = None
    created_readers_group_uuid: str | None = None
    created_eperson_uuids: list[str] = []
    created_collection_uuids: list[str] = []
    created_item_uuids: list[str] = []
    mega_metadata_item_uuid: str | None = None
    mega_bitstreams_item_uuid: str | None = None
    metrics: dict[str, int | str] = {
        "communities": 0,
        "groups": 0,
        "epeople": 0,
        "collections": 0,
        "items": 0,
        "bitstreams": 0,
        "special_items": 0,
        "item_views": 0,
    }
    samples: list[dict] = []

    try:
        console.print("\n[bold cyan]Creating MegaSpace content…[/bold cyan]\n")

        community_title = factory.get_discipline_title(discipline)
        community = await client.create_community(
            name=community_title,
            metadata={
                "dc.title": [
                    {
                        "value": community_title,
                        "language": "en",
                        "authority": None,
                        "confidence": -1,
                    }
                ],
                "dc.description": [
                    {
                        "value": f"MegaSpace repository for {discipline.name} (dspace-python-client example).",
                        "language": "en",
                        "authority": None,
                        "confidence": -1,
                    }
                ],
            },
        )
        created_top_community_uuid = community["uuid"]
        metrics["communities"] = 1
        console.print(f"  [green]✓[/green] Community: {created_top_community_uuid}\n")

        pre_existing_readers = await client.search_group_by_name(READERS_GROUP_NAME)
        readers_group = await client.find_or_create_group(
            name=READERS_GROUP_NAME,
            description=(
                "Shared READ group for MegaSpace collections — grants access to items and bitstreams"
            ),
        )
        created_readers_group_uuid = readers_group["uuid"]
        if pre_existing_readers is None:
            metrics["groups"] = int(metrics["groups"]) + 1
            console.print(
                f"  [green]✓[/green] Created MegaSpace Readers: {created_readers_group_uuid}\n"
            )
        else:
            console.print(
                f"  [green]✓[/green] Using existing MegaSpace Readers: {created_readers_group_uuid}\n"
            )

        console.print(f"[yellow]Creating {num_epeople} EPeople…[/yellow]")
        t_ep = time.perf_counter()
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("EPeople", total=num_epeople)
            for i in range(num_epeople):
                scientist = seed_pack.scientists[i % len(seed_pack.scientists)]
                email = generate_unique_email(scientist.first, scientist.last)
                eperson = await client.create_eperson(
                    email=email,
                    first_name=scientist.first,
                    last_name=scientist.last,
                )
                created_eperson_uuids.append(eperson["uuid"])
                metrics["epeople"] = int(metrics["epeople"]) + 1
                progress.update(task, advance=1)
                done = i + 1
                if done % SAMPLE_EVERY_N == 0 or done == num_epeople:
                    _append_phase_sample(
                        samples,
                        phase="epeople",
                        completed=done,
                        total=num_epeople,
                        phase_start=t_ep,
                    )
        console.print(f"  [green]✓[/green] EPeople created: {metrics['epeople']}\n")

        num_to_add = max(1, num_epeople // 10)
        console.print(f"[yellow]Adding {num_to_add} EPeople to {READERS_GROUP_NAME}…[/yellow]")
        t_ag = time.perf_counter()
        for i in range(num_to_add):
            await client.add_eperson_to_group(created_readers_group_uuid, created_eperson_uuids[i])
        _append_phase_sample(
            samples,
            phase="eperson_group_links",
            completed=num_to_add,
            total=num_to_add,
            phase_start=t_ag,
        )
        console.print("  [green]✓[/green] EPeople added to readers group\n")

        console.print(
            f"[yellow]Creating {num_collections} collections with default READ groups…[/yellow]"
        )
        t_col = time.perf_counter()
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            ctask = progress.add_task("Collections", total=num_collections)
            for i in range(num_collections):
                subfield_index = i % len(discipline.subfields) if discipline.subfields else 0
                collection_title = factory.get_collection_title(discipline, subfield_index)
                collection = await client.create_collection(
                    name=collection_title,
                    parent_community_uuid=created_top_community_uuid,
                    metadata={
                        "dc.title": [
                            {
                                "value": collection_title,
                                "language": "en",
                                "authority": None,
                                "confidence": -1,
                            }
                        ],
                        "dc.description": [
                            {
                                "value": f"Collection for {collection_title} publications.",
                                "language": "en",
                                "authority": None,
                                "confidence": -1,
                            }
                        ],
                    },
                )
                created_collection_uuids.append(collection["uuid"])
                metrics["collections"] = int(metrics["collections"]) + 1

                item_read_group = await client.create_collection_item_read_group(
                    collection["uuid"],
                    description=f"Default item READ group for {collection_title}",
                )
                bitstream_read_group = await client.create_collection_bitstream_read_group(
                    collection["uuid"],
                    description=f"Default bitstream READ group for {collection_title}",
                )
                metrics["groups"] = int(metrics["groups"]) + 2

                await client.add_subgroup_to_group(
                    item_read_group["uuid"], created_readers_group_uuid
                )
                await client.add_subgroup_to_group(
                    bitstream_read_group["uuid"],
                    created_readers_group_uuid,
                )
                progress.update(ctask, advance=1)
                cdone = i + 1
                if cdone % SAMPLE_EVERY_N == 0 or cdone == num_collections:
                    _append_phase_sample(
                        samples,
                        phase="collections",
                        completed=cdone,
                        total=num_collections,
                        phase_start=t_col,
                    )
        console.print(f"  [green]✓[/green] Collections: {metrics['collections']}\n")

        if num_collections >= 2 and num_items_per_collection > 0:
            console.print("[yellow]Special test items…[/yellow]")
            mega_meta_title = "Mega-Metadata Test Item: A Study with Extensive Annotations"
            mega_meta_metadata = build_mega_metadata(
                factory, seed_pack, mega_meta_title, discipline
            )
            mega_meta_item = await client.create_item(
                name=mega_meta_title,
                owning_collection_uuid=created_collection_uuids[0],
                metadata=mega_meta_metadata,
            )
            mega_metadata_item_uuid = mega_meta_item["uuid"]
            created_item_uuids.append(mega_meta_item["uuid"])
            metrics["items"] = int(metrics["items"]) + 1
            metrics["special_items"] = int(metrics["special_items"]) + 1
            bundle = await client.create_bundle(mega_meta_item["uuid"], "ORIGINAL")
            await client.upload_bitstream(
                bundle_uuid=bundle["uuid"],
                filename="mega-metadata-document.pdf",
                content=factory.generate_sample_pdf_content(mega_meta_title),
            )
            metrics["bitstreams"] = int(metrics["bitstreams"]) + 1
            console.print(f"    [green]✓[/green] Mega-metadata item: {mega_meta_item['uuid']}")

            if len(created_collection_uuids) >= 2:
                mega_bits_title = "Mega-Bitstreams Test Item: Multi-File Archive"
                mega_bits_metadata = factory.get_item_metadata(mega_bits_title, discipline, 1)
                mega_bits_item = await client.create_item(
                    name=mega_bits_title,
                    owning_collection_uuid=created_collection_uuids[1],
                    metadata=mega_bits_metadata,
                )
                mega_bitstreams_item_uuid = mega_bits_item["uuid"]
                created_item_uuids.append(mega_bits_item["uuid"])
                metrics["items"] = int(metrics["items"]) + 1
                metrics["special_items"] = int(metrics["special_items"]) + 1
                bundle_b = await client.create_bundle(mega_bits_item["uuid"], "ORIGINAL")
                n_bits = min(mega_bitstreams, 200)
                console.print(
                    f"[yellow]Uploading {n_bits} PDF bitstreams sequentially "
                    "(may take a while on slow or public servers)…[/yellow]"
                )
                t_mb = time.perf_counter()
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                    TimeElapsedColumn(),
                    console=console,
                ) as progress:
                    btask = progress.add_task("Mega-bitstreams", total=n_bits)
                    for j in range(n_bits):
                        fname = f"file-{j + 1:03d}.pdf"
                        await client.upload_bitstream(
                            bundle_uuid=bundle_b["uuid"],
                            filename=fname,
                            content=factory.generate_sample_pdf_content(
                                f"{mega_bits_title} - {fname}"
                            ),
                        )
                        metrics["bitstreams"] = int(metrics["bitstreams"]) + 1
                        progress.update(btask, advance=1)
                        bdone = j + 1
                        if bdone % SAMPLE_EVERY_N == 0 or bdone == n_bits:
                            _append_phase_sample(
                                samples,
                                phase="mega_bitstreams",
                                completed=bdone,
                                total=n_bits,
                                phase_start=t_mb,
                            )
                console.print(
                    f"    [green]✓[/green] Mega-bitstreams item: {mega_bits_item['uuid']} ({n_bits} files)\n"
                )

        total_regular = num_collections * num_items_per_collection
        if total_regular > 0:
            console.print(
                f"[yellow]Creating {total_regular} regular items (adaptive concurrency)…[/yellow]"
            )
            item_data = _build_item_data_list(
                factory,
                discipline,
                created_collection_uuids,
                num_items_per_collection,
            )
            config = ConcurrencyConfig(
                initial=2,
                max_concurrency=32,
                min_concurrency=2,
            )
            batch_creator = BatchItemCreator(client, config)
            regular_items_phase_start = time.perf_counter()

            def on_batch_metrics(completed: int, total: int, pm: PerformanceMetrics) -> None:
                samples.append(
                    {
                        "phase": "regular_items",
                        "completed": completed,
                        "total": total,
                        "throughput": round(pm.throughput, 6),
                        "p50_latency_s": round(pm.p50_latency, 6),
                        "p95_latency_s": round(pm.p95_latency, 6),
                        "current_concurrency": pm.current_concurrency,
                        "total_operations": pm.total_operations,
                        "successful_operations": pm.successful_operations,
                        "failed_operations": pm.failed_operations,
                        "elapsed_since_phase_start_s": round(
                            time.perf_counter() - regular_items_phase_start, 3
                        ),
                    }
                )

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                ptask = progress.add_task("Items", total=total_regular)
                (
                    created_items,
                    _bundles,
                    created_bitstreams,
                ) = await batch_creator.create_items_batch(
                    collection_uuids=created_collection_uuids,
                    item_data=item_data,
                    progress=progress,
                    task_id=ptask,
                    on_metrics_sample=on_batch_metrics,
                )
            for it in created_items:
                created_item_uuids.append(it["uuid"])
            metrics["items"] = int(metrics["items"]) + len(created_items)
            metrics["bitstreams"] = int(metrics["bitstreams"]) + len(created_bitstreams)

            final_metrics = await batch_creator.get_final_metrics()
            console.print(
                f"  [green]✓[/green] Batch: {final_metrics.throughput:.1f} items/s, "
                f"p95 {final_metrics.p95_latency * 1000:.0f}ms, "
                f"max concurrency {final_metrics.current_concurrency}\n"
            )

        if num_item_views > 0 and created_item_uuids:
            console.print(f"[yellow]Generating {num_item_views} item view events…[/yellow]")
            referrers = [
                f"{base_url.rstrip('/')}/search",
                f"{base_url.rstrip('/')}/browse",
                f"{base_url.rstrip('/')}/communities/{created_top_community_uuid}",
                None,
            ]
            weights = [
                2.0 ** (-i / len(created_item_uuids) * 3) for i in range(len(created_item_uuids))
            ]
            t_views = time.perf_counter()
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                vtask = progress.add_task("Views", total=num_item_views)
                for i in range(num_item_views):
                    chosen = factory.rng.choices(created_item_uuids, weights=weights, k=1)[0]
                    ref = referrers[i % len(referrers)]
                    await client.create_item_view(
                        target_uuid=chosen, target_type="item", referrer=ref
                    )
                    metrics["item_views"] = int(metrics["item_views"]) + 1
                    progress.update(vtask, advance=1)
                    vdone = i + 1
                    if vdone % SAMPLE_EVERY_N == 0 or vdone == num_item_views:
                        _append_phase_sample(
                            samples,
                            phase="item_views",
                            completed=vdone,
                            total=num_item_views,
                            phase_start=t_views,
                        )
            console.print(f"  [green]✓[/green] View events: {metrics['item_views']}\n")

        console.print("[bold green]✓ MegaSpace content created.[/bold green]\n")

        elapsed = time.perf_counter() - run_started
        diagnostics_payload = _build_diagnostics_payload(
            base_url=base_url,
            elapsed_seconds=elapsed,
            courtesy_delay=courtesy_delay,
            slow_requests=slow_requests,
            samples=samples,
            seed=seed,
            num_collections=num_collections,
            num_items_per_collection=num_items_per_collection,
            num_epeople=num_epeople,
            num_item_views=num_item_views,
            mega_bitstreams=mega_bitstreams,
            strict_versions=strict_versions,
            metrics=metrics,
        )
        _print_summary(
            base_url=base_url,
            community_uuid=created_top_community_uuid,
            readers_uuid=created_readers_group_uuid,
            mega_meta=mega_metadata_item_uuid,
            mega_bits=mega_bitstreams_item_uuid,
            metrics=metrics,
        )
        _print_run_diagnostics(
            elapsed_seconds=elapsed,
            courtesy_delay=courtesy_delay,
            slow_threshold=SLOW_REQUEST_THRESHOLD_SECONDS,
            slow_requests=slow_requests,
            base_url=base_url,
            num_collections=num_collections,
            num_items_per_collection=num_items_per_collection,
            mega_bitstreams_cap=min(mega_bitstreams, 200),
            degradation=diagnostics_payload.get("degradation"),
        )

        save_diag = (
            console.input(
                "[bold cyan]Save diagnostics[/bold cyan] (JSON + Markdown in current directory)? "
                "[dim](yes/no, default yes):[/dim] "
            )
            .strip()
            .lower()
        )
        if save_diag in ("", "y", "yes"):
            try:
                raw_path, md_path = _write_diagnostics_exports(Path.cwd(), diagnostics_payload)
                console.print(f"[green]Saved:[/green] {raw_path.name} and {md_path.name}")
            except OSError as exc:
                console.print(f"[red]Could not save diagnostics: {exc}[/red]")

        cleanup = (
            console.input(
                "[bold yellow]Delete created EPeople and the community? (yes/no):[/bold yellow] "
            )
            .strip()
            .lower()
        )
        if cleanup not in ("yes", "y"):
            console.print("[cyan]Cleanup skipped. MegaSpace Readers group may remain.[/cyan]")
            return True

        if not await _ensure_session(auth, client, username):
            return False

        console.print("\n[yellow]Cleaning up…[/yellow]")
        for uid in created_eperson_uuids:
            try:
                await client.delete_eperson(uid)
            except DSpaceAPIError:
                pass
        if created_top_community_uuid:
            await client.delete_community(created_top_community_uuid)
        console.print("[green]✓ EPeople and community removed. Collection groups cascade.[/green]")
        console.print("[dim]MegaSpace Readers group (site-wide) was not deleted.[/dim]")

        return True

    except DSpaceAPIError as e:
        console.print(f"[red]API error: {e}[/red]")
        return False
    finally:
        await auth.close()


def _build_item_data_list(
    factory: DataFactory,
    discipline: Discipline,
    collection_uuids: list[str],
    items_per_collection: int,
) -> list[dict]:
    """Build item_data in the same order BatchItemCreator assigns to collections (round-robin)."""
    out: list[dict] = []
    n = len(collection_uuids)
    total = n * items_per_collection
    for i in range(total):
        coll_idx = i % n
        subfield_index = coll_idx % len(discipline.subfields) if discipline.subfields else 0
        base_title = factory.get_item_title(discipline, subfield_index)
        title = f"{base_title} ({i + 1})"
        metadata = factory.get_item_metadata(title, discipline, subfield_index)
        pdf = factory.generate_sample_pdf_content(title)
        out.append(
            {
                "title": title,
                "metadata": metadata,
                "content": pdf,
                "filename": "document.pdf",
            }
        )
    return out


def _print_summary(
    *,
    base_url: str,
    community_uuid: str,
    readers_uuid: str,
    mega_meta: str | None,
    mega_bits: str | None,
    metrics: dict[str, int | str],
) -> None:
    bu = base_url.rstrip("/")
    special = ""
    if mega_meta:
        special += f"\n  Mega-metadata: {bu}/items/{mega_meta}"
    if mega_bits:
        special += f"\n  Mega-bitstreams: {bu}/items/{mega_bits}"
    text = f"""[bold cyan]MegaSpace summary[/bold cyan]

[yellow]Community[/yellow]  {community_uuid}
  {bu}/communities/{community_uuid}

[yellow]Readers group[/yellow] {readers_uuid}{special}

[yellow]Counts[/yellow]
  EPeople: {metrics["epeople"]}
  Collections: {metrics["collections"]}
  Items: {metrics["items"]}
  Bitstreams: {metrics["bitstreams"]}
  View events: {metrics["item_views"]}
"""
    console.print(Panel(text, title="Done", border_style="green"))


def _print_run_diagnostics(
    *,
    elapsed_seconds: float,
    courtesy_delay: float,
    slow_threshold: float,
    slow_requests: list[tuple[str, str, float]],
    base_url: str,
    num_collections: int,
    num_items_per_collection: int,
    mega_bitstreams_cap: int,
    degradation: dict | None = None,
) -> None:
    deg_line = ""
    if degradation:
        ratio = degradation.get("throughput_last_vs_first_ratio")
        if ratio is not None:
            deg_line = f"\n[yellow]Batch throughput[/yellow] last vs first sample ratio: {ratio}"
        note = degradation.get("note")
        if isinstance(note, str):
            deg_line += f"\n[yellow]Note[/yellow] {note}"
    text = f"""[bold cyan]Run diagnostics[/bold cyan]

[yellow]Base URL[/yellow] {base_url.rstrip("/")}
[yellow]Wall time[/yellow] {elapsed_seconds:.1f}s (content-creation phase after login)
[yellow]Courtesy delay[/yellow] {courtesy_delay}s between REST requests
[yellow]Slow request threshold[/yellow] {slow_threshold}s
[yellow]Scale[/yellow] collections={num_collections}, items/collection={num_items_per_collection}, mega-bitstreams cap={mega_bitstreams_cap}{deg_line}
"""
    console.print(Panel(text, title="Performance", border_style="cyan"))
    if not slow_requests:
        return
    console.print()
    console.print(
        f"[bold yellow]Slow requests (>{slow_threshold}s)[/bold yellow] — examples with timings:"
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


async def main_async(args: argparse.Namespace) -> None:
    show_script_attribution(SCRIPT_AUTHORS, console=console)
    supported = ", ".join(TARGET_VERSIONS)
    console.print(
        Panel.fit(
            "[bold cyan]MegaSpace[/bold cyan]\n"
            "Large-scale seed scenario (groups, EPeople, batch items, statistics).\n"
            f"Target versions: {supported}",
            border_style="cyan",
        )
    )
    console.print("[yellow]This script creates a lot of repository content.[/yellow]\n")

    proceed = console.input("[bold yellow]Continue? (yes/no):[/bold yellow] ").strip().lower()
    if proceed not in ("yes", "y"):
        console.print("[dim]Cancelled.[/dim]")
        return

    base_url = console.input(
        "[bold cyan]DSpace base URL[/bold cyan] [dim](Enter for https://demo.dspace.org):[/dim] "
    ).strip()
    if not base_url:
        base_url = "https://demo.dspace.org"
        console.print("[dim]→ https://demo.dspace.org[/dim]")

    is_demo = "demo.dspace.org" in base_url.rstrip("/").lower()
    if is_demo:
        console.print("[dim]Using demo admin credentials.[/dim]")
        username = "dspacedemo+admin@gmail.com"
        password = "dspace"
    else:
        username = console.input("[bold cyan]Username:[/bold cyan] ").strip()
        password = getpass.getpass("Password: ")

    courtesy_delay = args.courtesy_delay
    if courtesy_delay is None:
        raw = console.input(
            "[bold cyan]Delay between REST requests (seconds)[/bold cyan] [dim](Enter for 1.0):[/dim] "
        ).strip()
        courtesy_delay = float(raw) if raw else 1.0
    console.print(f"[dim]→ Courtesy delay: {courtesy_delay}s between REST requests[/dim]")

    ok = await run_megaspace(
        seed_pack_path=Path(args.seedpack).resolve(),
        seed=args.seed,
        base_url=base_url,
        username=username,
        password=password,
        num_collections=args.collections,
        num_items_per_collection=args.items_per_collection,
        num_epeople=args.epeople,
        num_item_views=args.item_views,
        mega_bitstreams=args.mega_bitstreams,
        strict_versions=not args.skip_version_check,
        courtesy_delay=courtesy_delay,
    )
    if not ok:
        raise SystemExit(1)


def _at_least_two(value: str) -> int:
    n = int(value)
    if n < 2:
        raise argparse.ArgumentTypeError(
            "MegaSpace requires at least 2 collections (mega-metadata and mega-bitstreams stress items "
            "use different owning collections; regular batch round-robins across collections)."
        )
    return n


def main() -> None:
    p = argparse.ArgumentParser(description="MegaSpace seed example for dspace-python-client.")
    p.add_argument("--seedpack", type=Path, default=DEFAULT_SEEDPACK, help="Path to seed pack YAML")
    p.add_argument("--seed", type=int, default=42, help="RNG seed for factory")
    p.add_argument(
        "--collections",
        type=_at_least_two,
        default=2,
        help="Number of collections (minimum 2; default 2). Required for the full scenario.",
    )
    p.add_argument(
        "--items-per-collection",
        type=int,
        default=2,
        help="Regular items per collection (default: 2)",
    )
    p.add_argument(
        "--epeople", type=int, default=5, help="Number of EPeople to create (default: 5)"
    )
    p.add_argument(
        "--item-views", type=int, default=50, dest="item_views", help="View events (default: 50)"
    )
    p.add_argument(
        "--mega-bitstreams",
        type=int,
        default=50,
        dest="mega_bitstreams",
        help="Bitstreams on mega-bitstreams item (default: 50, capped at 200)",
    )
    p.add_argument(
        "--skip-version-check",
        action="store_true",
        help="Skip verify_server_version after login (faster; default is to probe and enforce 9.0).",
    )
    p.add_argument(
        "--courtesy-delay",
        type=float,
        default=None,
        metavar="SEC",
        help="Seconds between REST requests (omit to prompt; default when prompted is 1.0).",
    )
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
