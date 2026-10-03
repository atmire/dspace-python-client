"""
PDF accessibility audit for DSpace: detection.

Finds PDFs in a chosen scope, checks each one with outloud (PDF/UA-1 + WCAG 2.2 rules) plus
an image-only-scan check, applies a severity policy, and reports the result as a CSV and/or
as a metadata value on each item.

    python examples/pdf_accessibility/detect.py                    # interactive
    python examples/pdf_accessibility/detect.py --base-url https://repo.example.org \\
        --username admin@example.org --scope collection --uuid <collection-uuid> --output csv

Writing metadata is a dry run unless --apply is given. See README.md in this folder.
Requires the a11y extra:  pip install -e ".[a11y]"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from dspace_client import DSpaceAPIError, DSpaceClientError, show_script_attribution
from pdf_accessibility.advice import Advice, load_overrides, report_for_record
from pdf_accessibility.analysis import analyse, failed_check, load_rule_catalogue
from pdf_accessibility.engine import CheckEngine, EngineConfig
from pdf_accessibility.fields import DEFAULT_FIELD, prepare_metadata_field
from pdf_accessibility.policy import Policy, PolicyError, RuleInfo, load_policy
from pdf_accessibility.scope import SCOPES, PdfTarget, ScopedItem, iter_scope, pdf_bitstreams
from pdf_accessibility.session import SCRIPT_AUTHORS, Session, ask, open_admin_session
from pdf_accessibility.sinks import (
    CsvSink,
    metadata_text,
    plan_metadata_patch,
    stale_markers,
)
from pdf_accessibility.state import (
    DEFAULT_REPORT_DIR,
    ResultStore,
    host_slug,
    now_iso,
    results_path,
    timestamp,
)

console = Console()


OUTPUTS = ("csv", "metadata", "both")

# Verdicts whose error/warning counts are worth printing on the progress line.
COUNTED = frozenset({"PASS", "REVIEW", "FAIL", "INCOMPLETE"})
VERDICT_STYLE = {
    "PASS": "green",
    "REVIEW": "yellow",
    "FAIL": "red",
    "INCOMPLETE": "magenta",
    "UNREADABLE": "red",
    "CHECK_FAILED": "magenta",
    "SKIPPED_TOO_LARGE": "dim",
    "DOWNLOAD_FAILED": "magenta",
    "NOT_PDF": "dim",
    "NO_PDF": "dim",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Check the accessibility of PDFs in a DSpace repository."
    )
    p.add_argument("--base-url", help="DSpace base URL, e.g. https://repo.example.org")
    p.add_argument("--username", help="Site administrator username (password is prompted)")
    p.add_argument("--scope", choices=SCOPES, help="What to check")
    p.add_argument("--uuid", help="Bitstream, item or collection UUID (for those scopes)")
    p.add_argument(
        "--which",
        choices=("mine", "all"),
        help="workspace: my submissions or everyone's; workflow: my tasks or all workflow items",
    )
    p.add_argument(
        "--output",
        choices=OUTPUTS,
        help="csv, or metadata/both (item metadata; the CSV is always written as well)",
    )
    p.add_argument("--field", help=f"Metadata field for the report (default {DEFAULT_FIELD})")
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually write metadata (default: dry run that only lists the planned changes)",
    )
    p.add_argument("--policy", help="Severity policy YAML (default: policy_default.yml)")
    p.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR), help="Where reports go")
    p.add_argument("--workers", type=int, default=EngineConfig().workers, help="Parallel checks")
    p.add_argument("--timeout", type=float, default=120.0, help="Seconds allowed per PDF check")
    p.add_argument("--max-size-mb", type=float, default=250.0, help="Skip PDFs larger than this")
    p.add_argument("--delay", type=float, default=0.1, help="Seconds between REST requests")
    p.add_argument("--limit", type=int, help="Stop after this many items (for a trial run)")
    p.add_argument("--recheck", action="store_true", help="Ignore cached results; check everything")
    p.add_argument(
        "--skip-version-check", action="store_true", help="Do not verify the DSpace version"
    )
    return p.parse_args(argv)


def choose(prompt: str, options: dict[str, str], default: str) -> str:
    """Numbered menu; returns the chosen key."""
    keys = list(options)
    console.print(f"\n[bold]{prompt}[/bold]")
    for i, key in enumerate(keys, 1):
        console.print(f"  {i}. {options[key]}")
    while True:
        raw = ask(console, "Choice", str(keys.index(default) + 1))
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            return keys[int(raw) - 1]
        if raw in options:
            return raw
        console.print("[yellow]Please enter one of the numbers above.[/yellow]")


def complete_args_interactively(args: argparse.Namespace) -> None:
    if not args.scope:
        args.scope = choose(
            "What should be checked?",
            {
                "bitstream": "One bitstream (UUID)",
                "item": "All PDFs of one item (UUID)",
                "collection": "All PDFs of all items in a collection (UUID)",
                "workspace": "In-progress submissions (workspace)",
                "workflow": "Items in workflow",
            },
            "collection",
        )
    if args.scope in ("bitstream", "item", "collection") and not args.uuid:
        args.uuid = ask(console, f"{args.scope.capitalize()} UUID")
    if args.scope == "workspace" and not args.which:
        args.which = choose(
            "Which workspace items?",
            {"mine": "Only my own submissions", "all": "Every user's in-progress submissions"},
            "all",
        )
    if args.scope == "workflow" and not args.which:
        args.which = choose(
            "Which workflow items?",
            {"all": "All items in workflow", "mine": "Only tasks in my pool or claimed by me"},
            "all",
        )
    if not args.output:
        args.output = choose(
            "Where should the results go?",
            {
                "csv": "CSV file only",
                "both": "Item metadata and CSV (dry run unless --apply)",
            },
            "csv",
        )
    if args.output in ("metadata", "both") and not args.field:
        args.field = ask(console, "Metadata field for the report", DEFAULT_FIELD)


@dataclass
class RunContext:
    session: Session
    engine: CheckEngine
    policy: Policy
    catalogue: dict[str, RuleInfo]
    overrides: dict[str, Advice]
    store: ResultStore
    csv: CsvSink
    tmpdir: Path
    max_bytes: int
    recheck: bool
    engine_version: str
    metadata: MetadataWriter | None = None
    counts: Counter[str] = field(default_factory=Counter)
    items_seen: int = 0


def report_text(record: dict[str, Any], ctx: RunContext) -> str:
    return report_for_record(record, ctx.policy, ctx.overrides)


def base_record(scoped: ScopedItem, target: PdfTarget) -> dict[str, Any]:
    return {
        "item_uuid": scoped.uuid,
        "item_title": scoped.title,
        "item_state": scoped.state,
        "workspaceitem_id": scoped.workspaceitem_id,
        "workflowitem_id": scoped.workflowitem_id,
        "bitstream_uuid": target.uuid,
        "bitstream_name": target.name,
        "checksum": target.checksum,
        "size": target.size,
    }


async def check_target(ctx: RunContext, scoped: ScopedItem, target: PdfTarget) -> dict[str, Any]:
    """Check one PDF (or reuse a cached result) and return its record."""
    record = base_record(scoped, target)
    if not ctx.recheck:
        cached = ctx.store.cached(target.uuid, target.checksum, ctx.engine_version, ctx.policy.hash)
        if cached:
            fresh = {**cached, **record}
            if any(
                cached.get(k) != record[k] for k in ("item_title", "item_state", "bitstream_name")
            ):
                ctx.store.append(fresh)  # keep item state current for remediation
            fresh["_cached"] = True
            return fresh

    record.update({"policy_hash": ctx.policy.hash, "analysis": None, "error": None})
    if target.size is not None and target.size > ctx.max_bytes:
        record.update(
            verdict="SKIPPED_TOO_LARGE", error=f"{target.size} bytes is over the size limit"
        )
        return finish_record(ctx, record)

    path = ctx.tmpdir / f"{target.uuid}.pdf"
    try:
        await ctx.session.refresh()
        await ctx.session.client.download_bitstream(target.uuid, path, max_bytes=ctx.max_bytes)
    except DSpaceAPIError as exc:
        record.update(verdict="DOWNLOAD_FAILED", error=str(exc)[:300])
        return finish_record(ctx, record)
    except DSpaceClientError as exc:  # over the size limit while streaming
        record.update(verdict="SKIPPED_TOO_LARGE", error=str(exc)[:300])
        return finish_record(ctx, record)

    try:
        if not target.format_known:
            with path.open("rb") as fh:
                if not fh.read(1024).lstrip().startswith(b"%PDF"):
                    record.update(verdict="NOT_PDF", error="the file does not start with %PDF")
                    return finish_record(ctx, record)
        try:
            outcome = await ctx.engine.check(path)
        except Exception as exc:  # the engine must never stop the run
            analysis = failed_check("error", f"{type(exc).__name__}: {exc}")
        else:
            if outcome.status == "ok" and outcome.raw is not None:
                analysis = analyse(outcome.raw, ctx.policy, ctx.catalogue)
            else:
                analysis = failed_check(outcome.status, outcome.error)
                analysis.engine = {"outloud": ctx.engine_version}
    finally:
        path.unlink(missing_ok=True)

    record.update(
        verdict=analysis.verdict,
        errors=analysis.errors,
        warnings=analysis.warnings,
        pages=analysis.pages,
        analysis=analysis.to_dict(),
        error=analysis.error,
    )
    return finish_record(ctx, record)


def finish_record(ctx: RunContext, record: dict[str, Any]) -> dict[str, Any]:
    record["checked_at"] = now_iso()
    record.setdefault("errors", 0)
    record.setdefault("warnings", 0)
    record.setdefault("pages", 0)
    record["engine_version"] = ctx.engine_version
    ctx.store.append(record)
    return record


class MetadataWriter:
    """Plans (and with --apply, applies) the report values for each item."""

    def __init__(self, ctx_field: str, apply: bool, plan_path: Path) -> None:
        self.field = ctx_field
        self.apply = apply
        self.plan_path = plan_path
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        self._plan = plan_path.open("w", encoding="utf-8")
        self.items_changed = 0
        self.values_written = 0
        self.failures = 0

    async def handle(
        self, ctx: RunContext, scoped: ScopedItem, records: list[dict[str, Any]]
    ) -> None:
        new_values = {
            r["bitstream_uuid"]: metadata_text(
                r["bitstream_uuid"], r["bitstream_name"], report_text(r, ctx)
            )
            for r in records
            if r["verdict"] != "NOT_PDF"
        }
        if not new_values:
            return
        client = ctx.session.client
        # Read the item fresh: embedded copies from search results can be out of date.
        item = await client.get_item(scoped.uuid)
        metadata = item.get("metadata") or {}
        ops = plan_metadata_patch(metadata, self.field, new_values)
        current = {r["bitstream_uuid"] for r in records} if scoped.only_bitstream is None else set()
        stale = stale_markers(metadata, self.field, current) if current else []
        self._plan.write(
            json.dumps(
                {
                    "item_uuid": scoped.uuid,
                    "item_title": scoped.title,
                    "operations": ops,
                    "stale_bitstreams": stale,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        self._plan.flush()
        if stale:
            console.print(
                f"  [dim]{len(stale)} report value(s) refer to bitstreams no longer in this item; left as they are.[/dim]"
            )
        if not ops:
            return
        self.items_changed += 1
        self.values_written += len(ops)
        if self.apply:
            try:
                await ctx.session.refresh()
                await client.patch_item(scoped.uuid, ops)
            except DSpaceAPIError as exc:
                self.failures += 1
                console.print(f"  [red]Could not write metadata to item {scoped.uuid}: {exc}[/red]")

    def close(self) -> None:
        self._plan.close()


async def process_item(ctx: RunContext, scoped: ScopedItem) -> None:
    await ctx.session.refresh()
    try:
        targets = await pdf_bitstreams(ctx.session.client, scoped)
    except DSpaceAPIError as exc:
        console.print(f"[red]Could not list files of item {scoped.uuid}: {exc}[/red]")
        ctx.counts["ITEM_ERROR"] += 1
        return
    if not targets:
        ctx.counts["NO_PDF"] += 1
        ctx.csv.write({"item_uuid": scoped.uuid, "item_title": scoped.title, "verdict": "NO_PDF"})
        return

    records = []
    for target in targets:
        record = await check_target(ctx, scoped, target)
        records.append(record)
        verdict = record["verdict"]
        ctx.counts[verdict] += 1
        style = VERDICT_STYLE.get(verdict, "white")
        cached = " [dim](cached)[/dim]" if record.get("_cached") else ""
        detail = (
            f" {record['errors']} error(s), {record['warnings']} warning(s)"
            if verdict in COUNTED
            else ""
        )
        console.print(
            f"[{style}]{verdict:<11}[/{style}] {scoped.title[:60]} / {target.name[:50]}{detail}{cached}"
        )
        ctx.csv.write(
            {
                "item_uuid": scoped.uuid,
                "item_title": scoped.title,
                "bitstream_uuid": target.uuid,
                "bitstream_filename": target.name,
                "verdict": verdict,
                "errors": record.get("errors", 0),
                "warnings": record.get("warnings", 0),
                "pages": record.get("pages", 0),
                "analysis": report_text(record, ctx),
            }
        )
    if ctx.metadata is not None:
        await ctx.metadata.handle(ctx, scoped, records)


async def run_scope(ctx: RunContext, args: argparse.Namespace) -> None:
    """Feed scoped items to a few concurrent item processors."""
    queue: asyncio.Queue[ScopedItem | None] = asyncio.Queue(maxsize=args.workers * 2)

    async def produce() -> None:
        try:
            async for scoped in iter_scope(
                ctx.session.client,
                args.scope,
                uuid=args.uuid,
                mine=args.which == "mine",
                eperson_uuid=ctx.session.eperson_uuid,
            ):
                if args.limit and ctx.items_seen >= args.limit:
                    break
                ctx.items_seen += 1
                await queue.put(scoped)
        finally:
            for _ in range(args.workers):
                await queue.put(None)

    async def consume() -> None:
        while (scoped := await queue.get()) is not None:
            try:
                await process_item(ctx, scoped)
            except Exception as exc:  # one item must not stop the run
                ctx.counts["ITEM_ERROR"] += 1
                console.print(f"[red]Item {scoped.uuid} failed: {type(exc).__name__}: {exc}[/red]")

    await asyncio.gather(produce(), *(consume() for _ in range(args.workers)))


def print_summary(ctx: RunContext, csv_path: Path, plan_path: Path | None) -> None:
    table = Table(title="Result per PDF")
    table.add_column("Verdict")
    table.add_column("Count", justify="right")
    for verdict, count in sorted(ctx.counts.items(), key=lambda kv: -kv[1]):
        style = VERDICT_STYLE.get(verdict, "white")
        table.add_row(f"[{style}]{verdict}[/{style}]", str(count))
    console.print()
    console.print(f"Items looked at: {ctx.items_seen}")
    console.print(table)
    console.print(f"CSV:      {csv_path}")
    console.print(f"Results:  {ctx.store.path}  (input for remediate.py)")
    if ctx.metadata and plan_path:
        mode = "written" if ctx.metadata.apply else "planned (dry run)"
        console.print(
            f"Metadata: {ctx.metadata.values_written} value change(s) on {ctx.metadata.items_changed} item(s) {mode}; "
            f"details in {plan_path}"
        )
        if ctx.metadata.failures:
            console.print(f"[red]{ctx.metadata.failures} item(s) could not be updated.[/red]")


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    show_script_attribution(SCRIPT_AUTHORS, console=console)
    console.print(
        Panel.fit(
            "[bold cyan]PDF accessibility audit: detection[/bold cyan]\n\n"
            "Checks PDF bitstreams against PDF/UA-1 and WCAG with outloud, and reports what is wrong, "
            "on which pages, and how to fix it. A PASS means no machine-detectable problems; some "
            "WCAG criteria always need a person.",
            border_style="cyan",
        )
    )

    try:
        policy = load_policy(args.policy)
        catalogue = load_rule_catalogue()
        overrides = load_overrides()
    except ImportError:
        console.print('[red]outloud is not installed.[/red] Run: pip install -e ".[a11y]"')
        return 2
    except PolicyError as exc:
        console.print(f"[red]Policy error:[/red] {exc}")
        return 2
    import outloud

    session = await open_admin_session(
        console,
        args.base_url,
        args.username,
        skip_version_check=args.skip_version_check,
        courtesy_delay=args.delay,
    )
    if session is None:
        return 1
    try:
        complete_args_interactively(args)
        apply = args.apply and args.output in ("metadata", "both")
        if args.output in ("metadata", "both"):
            args.field = args.field or DEFAULT_FIELD
            if not await prepare_metadata_field(
                console,
                session,
                args.field,
                apply,
                purpose="write accessibility reports to",
                scope_note="PDF accessibility report per bitstream, written by dspace-a11y-audit.",
                replaces="Existing values of that field written by this script are replaced; nothing else changes.",
            ):
                return 1

        report_dir = Path(args.report_dir)
        stamp = f"{host_slug(session.client.base_url)}-{timestamp()}"
        csv_path = report_dir / f"{stamp}-{args.scope}.csv"
        plan_path = report_dir / f"{stamp}-planned-patches.jsonl" if args.output != "csv" else None
        store = ResultStore(results_path(report_dir, session.client.base_url))

        with tempfile.TemporaryDirectory(prefix="pdf-a11y-") as tmp:
            async with CheckEngine(
                EngineConfig(workers=args.workers, timeout_seconds=args.timeout)
            ) as engine:
                ctx = RunContext(
                    session=session,
                    engine=engine,
                    policy=policy,
                    catalogue=catalogue,
                    overrides=overrides,
                    store=store,
                    csv=CsvSink(csv_path),
                    tmpdir=Path(tmp),
                    max_bytes=int(args.max_size_mb * 1024 * 1024),
                    recheck=args.recheck,
                    engine_version=outloud.__version__,
                    metadata=MetadataWriter(args.field, apply, plan_path) if plan_path else None,
                )
                console.print(
                    f"\nChecking scope [bold]{args.scope}[/bold]"
                    + (f" {args.uuid}" if args.uuid else "")
                    + (f" ({args.which})" if args.which else "")
                    + f" with policy {policy.standard}/{policy.hash[:6]}, {args.workers} worker(s).\n"
                )
                if args.scope == "collection":
                    console.print(
                        "[dim]Collection scope uses the Discovery (Solr) index; reindex first if it may be stale.[/dim]"
                    )
                try:
                    await run_scope(ctx, args)
                finally:
                    ctx.csv.close()
                    if ctx.metadata:
                        ctx.metadata.close()
        print_summary(ctx, csv_path, plan_path)
        return 0
    except DSpaceClientError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    except (EOFError, KeyboardInterrupt):
        console.print(
            "\n[yellow]Stopped. Results checked so far are saved and will be reused.[/yellow]"
        )
        return 130
    finally:
        await session.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
