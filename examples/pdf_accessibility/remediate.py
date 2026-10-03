"""
PDF accessibility audit for DSpace: remediation.

Reads the results that detect.py wrote (results.jsonl) and offers three tracks:

    exceptions  Existing content: record an exception for failing archived items that
                pre-date a cutoff date. Writes metadata (dry run unless --apply).
    contacts    New content: a CSV per submitter of in-progress items with problems, for the
                institution to send. Read-only.
    verify      A fixed PDF was uploaded next to the original: check that it passes and lost
                nothing compared with the original. Read-only.

    python examples/pdf_accessibility/remediate.py                  # interactive
    python examples/pdf_accessibility/remediate.py --base-url https://repo.example.org \\
        --username admin@example.org --track exceptions --cutoff 2027-04-26

See README.md in this folder. Requires the a11y extra:  pip install -e ".[a11y]"
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from dspace_client import DSpaceAPIError, DSpaceClient, DSpaceClientError, show_script_attribution
from pdf_accessibility.advice import load_overrides, render_report, report_for_record
from pdf_accessibility.analysis import analyse, failed_check, load_rule_catalogue
from pdf_accessibility.eligibility import eligibility_basis, parse_cutoff
from pdf_accessibility.engine import CheckEngine, EngineConfig
from pdf_accessibility.fields import prepare_metadata_field
from pdf_accessibility.ocr_track import DEFAULT_KEEP_BUNDLE, PLACEMENTS, track_ocr
from pdf_accessibility.pairing import File, pair_files
from pdf_accessibility.policy import PolicyError, load_policy
from pdf_accessibility.scope import ScopedItem, first_value, item_state, pdf_bitstreams
from pdf_accessibility.session import SCRIPT_AUTHORS, Session, ask, open_admin_session
from pdf_accessibility.state import (
    DEFAULT_REPORT_DIR,
    ResultStore,
    host_slug,
    results_path,
    timestamp,
)

console = Console()

TRACKS = ("exceptions", "contacts", "verify", "ocr")
EXCEPTIONS_CONFIG = _HERE / "exceptions.yml"
IN_PROGRESS = frozenset({"workspace", "workflow", "in-progress"})
# outloud rules that compare a remediated file with its original (--source).
REGRESSION_RULES = frozenset({"SEM-001", "SEM-002", "SEM-003"})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Act on the results of detect.py.")
    p.add_argument("--base-url", help="DSpace base URL, e.g. https://repo.example.org")
    p.add_argument("--username", help="Site administrator username (password is prompted)")
    p.add_argument("--track", choices=TRACKS, help="Which remediation track to run")
    p.add_argument(
        "--results", help="results.jsonl from detect.py (default: the one for this host)"
    )
    p.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR), help="Where reports go")
    p.add_argument(
        "--include-review", action="store_true", help="Also act on REVIEW, not only FAIL"
    )
    p.add_argument("--cutoff", help="exceptions: items dated before this day (YYYY-MM-DD) qualify")
    p.add_argument(
        "--config", default=str(EXCEPTIONS_CONFIG), help="exceptions: field and value template"
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="exceptions/ocr: write to the repository (default: dry run)",
    )
    p.add_argument(
        "--placement",
        choices=PLACEMENTS,
        help="ocr: local (save only), alongside (upload next to the original) or replace",
    )
    p.add_argument(
        "--keep-bundle",
        default=DEFAULT_KEEP_BUNDLE,
        help="ocr --placement replace: bundle the original is moved to",
    )
    p.add_argument("--lang", default="en", help="ocr: document language when the item has none")
    p.add_argument("--item", help="ocr: only this item UUID (try it on one item first)")
    p.add_argument(
        "--min-confidence",
        type=float,
        default=0.80,
        help="ocr: below this mean OCR confidence a file is saved for review, not uploaded",
    )
    p.add_argument("--policy", help="verify: severity policy YAML (default: policy_default.yml)")
    p.add_argument(
        "--accept-review",
        action="store_true",
        help="verify: a replacement with verdict REVIEW counts as verified",
    )
    p.add_argument(
        "--workers", type=int, default=EngineConfig().workers, help="verify: parallel checks"
    )
    p.add_argument("--timeout", type=float, default=120.0, help="verify: seconds allowed per check")
    p.add_argument("--max-size-mb", type=float, default=250.0, help="verify: skip larger PDFs")
    p.add_argument("--delay", type=float, default=0.1, help="Seconds between REST requests")
    p.add_argument(
        "--skip-version-check", action="store_true", help="Do not verify the DSpace version"
    )
    return p.parse_args(argv)


def wanted_verdicts(args: argparse.Namespace) -> set[str]:
    return {"FAIL", "REVIEW"} if args.include_review else {"FAIL"}


def by_item(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["item_uuid"]].append(record)
    return grouped


async def still_present(
    client: DSpaceClient, item: dict[str, Any], records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The records whose PDF is still in the item. results.jsonl keeps removed files too."""
    current = {
        t.uuid for t in await pdf_bitstreams(client, ScopedItem(item=item, state=item_state(item)))
    }
    return [r for r in records if r["bitstream_uuid"] in current]


def write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})


def count_table(title: str, counts: Counter[str]) -> Table:
    table = Table(title=title)
    table.add_column("Outcome")
    table.add_column("Items", justify="right")
    for key, value in sorted(counts.items(), key=lambda kv: -kv[1]):
        table.add_row(key, str(value))
    return table


# ---------------------------------------------------------------- exceptions


async def track_exceptions(
    session: Session, store: ResultStore, args: argparse.Namespace, out: Path
) -> int:
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    field = str(config.get("field") or "").strip()
    template = str(config.get("value") or "").strip()
    if not field or not template:
        console.print(f"[red]{args.config} must define 'field' and 'value'.[/red]")
        return 2
    cutoff_text = args.cutoff or ask(
        console, "Cutoff date (YYYY-MM-DD): items dated before it qualify"
    )
    try:
        cutoff = parse_cutoff(cutoff_text)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2

    verdicts = wanted_verdicts(args)
    candidates = by_item(
        [
            r
            for r in store.records()
            if r.get("item_state") == "archived" and r.get("verdict") in verdicts
        ]
    )
    console.print(
        f"{len(candidates)} archived item(s) with verdict {' or '.join(sorted(verdicts))} in the results."
    )
    if not candidates:
        return 0
    if not await prepare_metadata_field(
        console,
        session,
        field,
        args.apply,
        purpose="record accessibility exceptions in",
        scope_note="Accessibility exception recorded by dspace-a11y-audit (see exceptions.yml).",
        replaces="Values are only added to items that have no value in this field yet; existing values are never changed.",
    ):
        return 1

    client = session.client
    today = datetime.now(UTC).date().isoformat()
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for item_uuid, records in candidates.items():
        await session.refresh()
        try:
            item = await client.get_item(item_uuid)
        except DSpaceAPIError as exc:
            counts["item not readable"] += 1
            rows.append({"item_uuid": item_uuid, "decision": "error", "reason": str(exc)[:200]})
            continue
        records = await still_present(client, item, records)
        if not records:
            counts["failing files no longer in item"] += 1
            continue
        metadata = item.get("metadata") or {}
        row: dict[str, Any] = {
            "item_uuid": item_uuid,
            "item_title": first_value(item, "dc.title") or "",
            "files": "; ".join(f"{r['bitstream_name']}: {r['verdict']}" for r in records),
        }
        basis = eligibility_basis(metadata)
        if item_state(item) != "archived":
            decision, reason = "skipped", f"item is now {item_state(item)}"
        elif metadata.get(field):
            decision, reason = "skipped", f"already has a value in {field}"
        elif basis is None:
            decision, reason = "no usable date", "no valid dc.date.issued or dc.date.accessioned"
        elif basis.date >= cutoff:
            decision, reason = (
                "does not qualify",
                f"{basis.field} {basis.value} is not before {cutoff}",
            )
        else:
            decision, reason = "qualifies", f"{basis.field} {basis.value} is before {cutoff}"
        row.update(decision=decision, reason=reason)
        if basis:
            row.update(
                basis_field=basis.field,
                basis_value=basis.value,
                eligibility_date=basis.date.isoformat(),
            )
        if decision == "qualifies" and basis is not None:
            value = template.format(
                cutoff=cutoff.isoformat(),
                basis_field=basis.field,
                basis_value=basis.value,
                today=today,
            )
            row["value"] = value
            if args.apply:
                try:
                    await client.patch_item(
                        item_uuid,
                        [
                            {
                                "op": "add",
                                "path": f"/metadata/{field}/-",
                                "value": {"value": value, "language": None},
                            }
                        ],
                    )
                    decision = "written"
                except DSpaceAPIError as exc:
                    decision = "write failed"
                    row["reason"] = str(exc)[:200]
                row["decision"] = decision
        counts[decision] += 1
        rows.append(row)

    path = out / f"{host_slug(client.base_url)}-{timestamp()}-exceptions.csv"
    write_csv(
        path,
        [
            "item_uuid",
            "item_title",
            "decision",
            "reason",
            "basis_field",
            "basis_value",
            "eligibility_date",
            "files",
            "value",
        ],
        rows,
    )
    console.print(count_table(f"Exceptions ({'applied' if args.apply else 'dry run'})", counts))
    console.print(f"Details: {path}")
    if not args.apply and counts.get("qualifies"):
        console.print(
            "[yellow]Dry run: nothing was written. Re-run with --apply to record these exceptions.[/yellow]"
        )
    return 0


# ------------------------------------------------------------------ contacts


async def track_contacts(
    session: Session, store: ResultStore, args: argparse.Namespace, out: Path
) -> int:
    verdicts = wanted_verdicts(args)
    candidates = by_item(
        [
            r
            for r in store.records()
            if r.get("item_state") in IN_PROGRESS and r.get("verdict") in verdicts
        ]
    )
    console.print(
        f"{len(candidates)} in-progress item(s) with verdict {' or '.join(sorted(verdicts))}."
    )
    try:
        policy = load_policy(args.policy)
    except PolicyError as exc:
        console.print(f"[red]Policy error:[/red] {exc}")
        return 2
    overrides = load_overrides()
    client = session.client
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for item_uuid, records in candidates.items():
        await session.refresh()
        try:
            item = await client.get_item(item_uuid)
        except DSpaceAPIError:
            counts["item gone"] += 1
            continue
        records = await still_present(client, item, records)
        if not records:
            counts["failing files no longer in item"] += 1
            continue
        submitter = await client.get_item_submitter(item_uuid) or {}
        name = " ".join(
            filter(
                None,
                [
                    first_value(submitter, "eperson.firstname"),
                    first_value(submitter, "eperson.lastname"),
                ],
            )
        )
        counts["with submitter email" if submitter.get("email") else "no submitter found"] += 1
        rows.append(
            {
                "submitter_name": name,
                "submitter_email": submitter.get("email", ""),
                "item_uuid": item_uuid,
                "item_title": first_value(item, "dc.title") or "(untitled)",
                "item_state": records[0].get("item_state"),
                "files": "; ".join(f"{r['bitstream_name']}: {r['verdict']}" for r in records),
                "analysis": "\n\n".join(
                    f"{r['bitstream_name']}\n{report_for_record(r, policy, overrides)}"
                    for r in records
                ),
            }
        )
    rows.sort(key=lambda r: (r["submitter_email"], r["item_title"]))
    path = out / f"{host_slug(client.base_url)}-{timestamp()}-author-contacts.csv"
    write_csv(
        path,
        [
            "submitter_name",
            "submitter_email",
            "item_uuid",
            "item_title",
            "item_state",
            "files",
            "analysis",
        ],
        rows,
    )
    console.print(count_table("Author contacts", counts))
    console.print(
        f"Contact list: {path}  (nothing was sent; the institution decides how to contact authors)"
    )
    return 0


# -------------------------------------------------------------------- verify


async def track_verify(
    session: Session, store: ResultStore, args: argparse.Namespace, out: Path
) -> int:
    try:
        policy = load_policy(args.policy)
        catalogue = load_rule_catalogue()
    except PolicyError as exc:
        console.print(f"[red]Policy error:[/red] {exc}")
        return 2
    overrides = load_overrides()
    verdicts = {"FAIL", "REVIEW"}  # any original that had problems may get a replacement
    known_by_item: dict[str, set[str]] = defaultdict(set)
    for record in store.records():
        known_by_item[record["item_uuid"]].add(record["bitstream_uuid"])
    originals_by_item = by_item([r for r in store.records() if r.get("verdict") in verdicts])
    console.print(f"{len(originals_by_item)} item(s) had a PDF with verdict FAIL or REVIEW.")

    client = session.client
    max_bytes = int(args.max_size_mb * 1024 * 1024)
    ok_verdicts = {"PASS", "REVIEW"} if args.accept_review else {"PASS"}
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    checked_on = datetime.now(UTC).date().isoformat()

    with tempfile.TemporaryDirectory(prefix="pdf-a11y-verify-") as tmp:
        tmpdir = Path(tmp)
        async with CheckEngine(
            EngineConfig(workers=args.workers, timeout_seconds=args.timeout)
        ) as engine:
            for item_uuid, original_records in originals_by_item.items():
                await session.refresh()
                try:
                    item = await client.get_item(item_uuid)
                    current = await pdf_bitstreams(
                        client, ScopedItem(item=item, state=item_state(item))
                    )
                except DSpaceAPIError:
                    counts["item gone"] += 1
                    continue
                title = first_value(item, "dc.title") or "(untitled)"
                current_by_uuid = {t.uuid: t for t in current}
                originals = [
                    File(r["bitstream_uuid"], r["bitstream_name"])
                    for r in original_records
                    if r["bitstream_uuid"] in current_by_uuid
                ]
                candidates = [
                    File(t.uuid, t.name) for t in current if t.uuid not in known_by_item[item_uuid]
                ]
                if not candidates:
                    continue  # nothing new uploaded to this item
                base = {"item_uuid": item_uuid, "item_title": title}
                if not originals:
                    for cand in candidates:
                        counts["ORIGINAL_MISSING"] += 1
                        rows.append(
                            {
                                **base,
                                "replacement_uuid": cand.uuid,
                                "replacement_filename": cand.name,
                                "result": "ORIGINAL_MISSING",
                                "analysis": "The original is no longer in the item, so there is nothing to compare "
                                "with. Run detect.py on this item to check the new file on its own.",
                            }
                        )
                    continue
                pairing = pair_files(originals, candidates)
                for orig in pairing.unpaired_originals:
                    counts["UNPAIRED"] += 1
                    rows.append(
                        {
                            **base,
                            "original_uuid": orig.uuid,
                            "original_filename": orig.name,
                            "result": "UNPAIRED",
                            "analysis": "No unambiguous replacement found.",
                        }
                    )
                for cand in pairing.unpaired_candidates:
                    counts["UNPAIRED"] += 1
                    rows.append(
                        {
                            **base,
                            "replacement_uuid": cand.uuid,
                            "replacement_filename": cand.name,
                            "result": "UNPAIRED",
                            "analysis": "Could not tell which original this file replaces.",
                        }
                    )

                original_verdicts = {r["bitstream_uuid"]: r["verdict"] for r in original_records}
                for orig, cand in pairing.pairs:
                    row = {
                        **base,
                        "original_uuid": orig.uuid,
                        "original_filename": orig.name,
                        "original_verdict": original_verdicts.get(orig.uuid, ""),
                        "replacement_uuid": cand.uuid,
                        "replacement_filename": cand.name,
                    }
                    orig_path, cand_path = tmpdir / f"{orig.uuid}.pdf", tmpdir / f"{cand.uuid}.pdf"
                    try:
                        await session.refresh()
                        await client.download_bitstream(orig.uuid, orig_path, max_bytes=max_bytes)
                        await client.download_bitstream(cand.uuid, cand_path, max_bytes=max_bytes)
                        outcome = await engine.check(cand_path, source=orig_path)
                    except DSpaceClientError as exc:
                        counts["NOT_CHECKED"] += 1
                        rows.append({**row, "result": "NOT_CHECKED", "analysis": str(exc)[:300]})
                        continue
                    finally:
                        orig_path.unlink(missing_ok=True)
                        cand_path.unlink(missing_ok=True)
                    if outcome.status == "ok" and outcome.raw is not None:
                        analysis = analyse(outcome.raw, policy, catalogue)
                        regressions = sorted(
                            {
                                f["rule"]
                                for f in outcome.raw.get("findings", [])
                                if f["rule"] in REGRESSION_RULES
                            }
                        )
                    else:
                        analysis = failed_check(outcome.status, outcome.error)
                        regressions = []
                    verified = analysis.verdict in ok_verdicts and not regressions
                    result = "VERIFIED" if verified else "NOT_VERIFIED"
                    counts[result] += 1
                    rows.append(
                        {
                            **row,
                            "replacement_verdict": analysis.verdict,
                            "regressions": ", ".join(regressions),
                            "result": result,
                            "analysis": render_report(analysis, policy, overrides, checked_on),
                        }
                    )
                    style = "green" if verified else "red"
                    console.print(
                        f"[{style}]{result:<12}[/{style}] {title[:50]} / {orig.name} -> {cand.name}"
                    )

    path = out / f"{host_slug(client.base_url)}-{timestamp()}-verify.csv"
    write_csv(
        path,
        [
            "item_uuid",
            "item_title",
            "original_uuid",
            "original_filename",
            "original_verdict",
            "replacement_uuid",
            "replacement_filename",
            "replacement_verdict",
            "regressions",
            "result",
            "analysis",
        ],
        rows,
    )
    console.print(count_table("Replacement verification", counts))
    console.print(f"Details: {path}")
    console.print(
        "[dim]Run detect.py on verified items to record the new files in the results.[/dim]"
    )
    return 0


# ---------------------------------------------------------------------- main


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    show_script_attribution(SCRIPT_AUTHORS, console=console)
    console.print(
        Panel.fit(
            "[bold cyan]PDF accessibility audit: remediation[/bold cyan]\n\n"
            "Acts on the results of detect.py: record exceptions for existing content, list authors "
            "to contact about new content, or verify replacement files.",
            border_style="cyan",
        )
    )
    try:
        import outloud  # noqa: F401 - fail early if the extra is missing
    except ImportError:
        console.print('[red]outloud is not installed.[/red] Run: pip install -e ".[a11y]"')
        return 2

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
        if not args.track:
            console.print("\n[bold]Which remediation track?[/bold]")
            console.print(
                "  1. exceptions: record an exception for failing pre-existing archived items"
            )
            console.print("  2. contacts:   list submitters of in-progress items with problems")
            console.print("  3. verify:     check replacement PDFs against their originals")
            console.print("  4. ocr:        add a text layer to scanned PDFs (pip-only OCR)")
            choice = ask(console, "Choice", "1")
            args.track = {"1": "exceptions", "2": "contacts", "3": "verify", "4": "ocr"}.get(
                choice, choice
            )
            if args.track not in TRACKS:
                console.print("[red]Unknown track.[/red]")
                return 2
        report_dir = Path(args.report_dir)
        results = (
            Path(args.results)
            if args.results
            else results_path(report_dir, session.client.base_url)
        )
        if not results.exists():
            console.print(
                f"[red]No results file at {results}.[/red] Run detect.py first, or pass --results."
            )
            return 1
        store = ResultStore(results)
        console.print(f"Using results from {results} ({len(store.latest)} PDF(s)).")
        if args.track == "ocr":
            return await track_ocr(console, session, store, args, report_dir, still_present)
        track = {
            "exceptions": track_exceptions,
            "contacts": track_contacts,
            "verify": track_verify,
        }[args.track]
        return await track(session, store, args, report_dir)
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]Stopped.[/yellow]")
        return 130
    finally:
        await session.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
