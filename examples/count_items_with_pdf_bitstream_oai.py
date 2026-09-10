"""Count items with at least one PDF bitstream via OAI-PMH (dc:format).

ANTI-PATTERN WARNING
--------------------
Counting items with PDF bitstreams via OAI-PMH is significantly slower and less
accurate than a direct SQL query against the DSpace database. For any non-trivial
repository, run SQL against the DSpace DB instead; the authoritative counts live
there. This example is preserved as a working reference for OAI-PMH harvesting
patterns (CSV caching, incremental `from=` harvests), not as a recommended
approach to this particular problem.

Uses the repository's OAI endpoint at {base_url}/server/oai/request. No authentication
required. Infers PDF from <dc:format>application/pdf</dc:format> in oai_dc metadata.
Supports a persistent CSV cache so resumed runs skip already-seen items and optional
incremental harvest (from= last_until).
"""

import asyncio
import os
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

from dspace_client import show_script_attribution
from dspace_client.oai import (
    OAIClient,
    OAIError,
    OAIPDFCountCache,
    iterate_oai_dc_records,
)

SCRIPT_AUTHORS = "Bram Luyten (Atmire)"

console = Console()


async def main() -> None:
    """Harvest OAI oai_dc, count items with dc:format=application/pdf, optionally use cache."""
    show_script_attribution(SCRIPT_AUTHORS, console=console)
    console.print(
        Panel(
            "This script counts items with a PDF bitstream via OAI-PMH harvesting.\n"
            "For any repository of non-trivial size, a direct SQL query against\n"
            "the DSpace database is dramatically faster and more accurate.\n"
            "This example is preserved as a reference for OAI-PMH harvesting\n"
            "patterns, not as the recommended way to answer this question.\n"
            "Use SQL if you have DB access.",
            title="Anti-pattern warning",
            border_style="yellow",
            padding=(1, 2),
        )
    )
    console.print("\n[bold cyan]Count items with PDF bitstream (OAI-PMH)[/bold cyan]")
    console.print("[dim]Uses ListRecords oai_dc and dc:format. No auth required.[/dim]\n")

    base_url = console.input(
        "[bold cyan]DSpace base URL[/bold cyan] [dim](e.g. https://bradscholars.brad.ac.uk):[/dim] "
    ).strip()
    if not base_url:
        base_url = "https://demo.dspace.org"
        console.print("[dim]→ Using https://demo.dspace.org[/dim]")

    cache_dir = os.environ.get("DSPACE_OAI_CACHE_DIR")
    if cache_dir is None:
        cache_dir = str(Path.home() / ".cache" / "dspace-oai-pdf")
    cache = OAIPDFCountCache(base_url=base_url, cache_dir=Path(cache_dir))
    cache.load()
    console.print(f"[dim]Cache: {cache.cache_path}[/dim]")

    from_cache_total, from_cache_with_pdf = cache.totals()
    if from_cache_total > 0:
        console.print(
            f"[dim]Loaded cache: {from_cache_total} items, {from_cache_with_pdf} with PDF[/dim]"
        )

    use_incremental = cache.last_until is not None
    if use_incremental:
        console.print(f"[dim]Incremental harvest from {cache.last_until}[/dim]")

    console.print("[green]Harvesting OAI ListRecords (oai_dc)…[/green]\n")

    start = time.perf_counter()
    new_count = 0
    updated_count = 0
    total_running = from_cache_total
    with_pdf_running = from_cache_with_pdf
    max_datestamp: str | None = None

    try:
        async with OAIClient(base_url=base_url) as client:
            from_param = cache.last_until if use_incremental else None
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                console=console,
            ) as progress:
                task_id = progress.add_task("Harvesting records...", total=None)
                async for parsed in iterate_oai_dc_records(client, from_=from_param):
                    ident = parsed["identifier"]
                    datestamp = parsed["datestamp"]
                    has_pdf = parsed["has_pdf"]
                    if max_datestamp is None or (datestamp and datestamp > max_datestamp):
                        max_datestamp = datestamp
                    existing = cache.get(ident)
                    if existing is None:
                        new_count += 1
                        total_running += 1
                        if has_pdf:
                            with_pdf_running += 1
                    elif existing["datestamp"] != datestamp or existing["has_pdf"] != has_pdf:
                        updated_count += 1
                        if has_pdf and not existing["has_pdf"]:
                            with_pdf_running += 1
                        elif not has_pdf and existing["has_pdf"]:
                            with_pdf_running -= 1
                    cache.update(ident, datestamp, has_pdf)
                    progress.update(
                        task_id,
                        description=f"Items: {total_running} | With PDF: {with_pdf_running} | New: {new_count} | Updated: {updated_count}",
                    )
                progress.update(
                    task_id,
                    description=f"Done. Items: {total_running} | With PDF: {with_pdf_running}",
                )
    except OAIError as e:
        console.print(f"[red]OAI error: {e.code} - {e.message}[/red]")
        return

    cache.save(last_until=max_datestamp)
    elapsed = time.perf_counter() - start
    total, with_pdf = cache.totals()

    console.print("\n[bold]Result[/bold]")
    console.print(f"  Items with ≥1 PDF (dc:format): [bold]{with_pdf}[/bold]")
    console.print(f"  Total items:                    [bold]{total}[/bold]")
    if new_count or updated_count:
        console.print(f"  New this run:                    {new_count}")
        console.print(f"  Updated this run:               {updated_count}")
    console.print(f"  Time:                            {elapsed:.1f}s")
    console.print(f"  Cache:                           {cache.cache_path}")


if __name__ == "__main__":
    asyncio.run(main())
