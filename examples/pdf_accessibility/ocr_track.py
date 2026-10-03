"""
Remediation track "ocr": add a text layer to scanned PDFs, then let the manager decide what
happens to the result.

Candidates are PDFs whose detection result has OCR-001 (pages that are images without text).
For each one the track:

1. downloads the PDF and OCRs its image-only pages in a worker process (ocr.py, pip-only);
2. checks the result with outloud against the original: no lost structure, text or changed
   appearance (SEM-001..003), no new text or font problems, and enough OCR confidence;
3. saves the result locally, and then, depending on --placement:
     local      nothing else; the manager reviews the files first (default)
     alongside  upload "<name>_ocr.pdf" next to the original in ORIGINAL
     replace    upload the OCR'd file under the original name to ORIGINAL and move the
                original to a preservation bundle (default PRESERVATION); the primary
                bitstream flag moves with it. Nothing is deleted.

Uploading happens only with --apply, after retype-to-confirm, and only when the original can be
downloaded by Anonymous. A new upload takes on its bundle's access policies, not the original's,
so an embargoed or restricted original must not get a more open OCR copy by accident. Such files
are saved locally for the manager to handle by hand.
"""

from __future__ import annotations

import argparse
import csv
import re
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from dspace_client import DSpaceAPIError, DSpaceClientError

from .engine import CheckEngine, EngineConfig
from .scope import ScopedItem, first_value, item_state, pdf_bitstreams
from .session import Session, ask
from .state import ResultStore, host_slug, timestamp

PLACEMENTS = ("local", "alongside", "replace")
DEFAULT_KEEP_BUNDLE = "PRESERVATION"
REGRESSION_RULES = frozenset({"SEM-001", "SEM-002", "SEM-003"})
SECONDS_PER_PAGE_ALLOWED = 6.0

CSV_COLUMNS = [
    "item_uuid",
    "item_title",
    "bitstream_uuid",
    "bitstream_filename",
    "pages",
    "pages_ocrd",
    "mean_confidence",
    "status",
    "placement",
    "new_bitstream_uuid",
    "local_file",
    "notes",
]


def needs_ocr(record: dict[str, Any]) -> bool:
    findings = (record.get("analysis") or {}).get("findings") or []
    return any(f.get("rule") == "OCR-001" for f in findings)


def pdf_language(item: dict[str, Any], fallback: str) -> str:
    """Document language for /Lang: from dc.language.iso ('en_US' -> 'en-US'), else fallback."""
    for field in ("dc.language.iso", "dc.language"):
        value = (first_value(item, field) or "").strip()
        if re.fullmatch(r"[A-Za-z]{2,3}([_-][A-Za-z0-9]{2,8})*", value):
            return value.replace("_", "-")
    return fallback


def ocr_name(original: str, placement: str) -> str:
    if placement == "replace":
        return original
    stem, dot, ext = original.rpartition(".")
    return f"{stem}_ocr.{ext}" if dot else f"{original}_ocr.pdf"


async def find_or_create_bundle(session: Session, item_uuid: str, name: str) -> dict[str, Any]:
    listing = await session.client.get_item_bundles(item_uuid)
    for bundle in (listing.get("_embedded") or {}).get("bundles") or []:
        if bundle.get("name") == name:
            return dict(bundle)
    return await session.client.create_bundle(item_uuid, name)


def choose_placement(console: Console, given: str | None) -> str:
    if given:
        return given
    console.print("\n[bold]What should happen with each OCR'd PDF?[/bold]")
    console.print(
        "  1. local:     save it in the report folder for review; change nothing in DSpace"
    )
    console.print("  2. alongside: upload it next to the original (as <name>_ocr.pdf)")
    console.print(
        "  3. replace:   upload it in place of the original; move the original to a "
        f"'{DEFAULT_KEEP_BUNDLE}' bundle"
    )
    choice = ask(console, "Choice", "1")
    return {"1": "local", "2": "alongside", "3": "replace"}.get(choice, choice)


async def place_result(
    session: Session,
    record: dict[str, Any],
    item: dict[str, Any],
    ocr_file: Path,
    placement: str,
    keep_bundle: str,
    engine_versions: dict[str, str],
) -> tuple[str, str, str]:
    """Upload (and for replace, move the original). Returns (status, new uuid, notes)."""
    client = session.client
    original_uuid = record["bitstream_uuid"]
    original_name = record["bitstream_name"]
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    engine = ", ".join(f"{k} {v}" for k, v in sorted(engine_versions.items()))
    new_name = ocr_name(original_name, placement)
    description = (
        f"Searchable version of {original_name}: a text layer was added by OCR ({engine}, "
        f"dspace-a11y-audit) on {today}. Original bitstream: {original_uuid}."
    )
    await session.refresh()
    bundle = await client.get_bitstream_bundle(original_uuid)
    was_primary = ((await client.get_primary_bitstream(bundle["uuid"])) or {}).get(
        "uuid"
    ) == original_uuid
    new = await client.upload_bitstream(
        bundle["uuid"],
        new_name,
        ocr_file.read_bytes(),
        metadata={"dc.description": [{"value": description, "language": None}]},
    )
    notes = [f"uploaded {new_name} to {bundle.get('name')}"]
    status = "UPLOADED"
    if placement == "replace":
        try:
            keep = await find_or_create_bundle(session, item["uuid"], keep_bundle)
            await client.move_bitstream(original_uuid, keep["uuid"])
            notes.append(f"moved original to {keep_bundle}")
            if was_primary:
                await client.set_primary_bitstream(bundle["uuid"], new["uuid"])
                notes.append("new file is now the primary bitstream")
            status = "REPLACED"
        except DSpaceAPIError as exc:
            # The OCR'd file is uploaded; the original is still in ORIGINAL. Nothing is
            # rolled back or deleted: the manager finishes this by hand.
            status = "PARTIAL"
            notes.append(f"could not move the original: {str(exc)[:200]}")
    provenance = (
        f"Accessibility OCR (dspace-a11y-audit, {engine}): added {new_name} ({new['uuid']}) "
        f"with a text layer for {original_name} ({original_uuid})"
        + (f"; original moved to bundle {keep_bundle}" if status == "REPLACED" else "")
        + f". By {session.eperson.get('email', session.username)} on {today}."
    )
    try:
        await client.patch_item(
            item["uuid"],
            [
                {
                    "op": "add",
                    "path": "/metadata/dc.description.provenance/-",
                    "value": {"value": provenance, "language": "en"},
                }
            ],
        )
    except DSpaceAPIError as exc:
        notes.append(f"provenance not written: {str(exc)[:120]}")
    return status, str(new["uuid"]), "; ".join(notes)


async def track_ocr(
    console: Console,
    session: Session,
    store: ResultStore,
    args: argparse.Namespace,
    out: Path,
    still_present: Any,
) -> int:
    try:
        import rapidocr  # noqa: F401
    except ImportError:
        console.print(
            '[red]The OCR track needs the a11y-ocr extra:[/red] pip install -e ".[a11y-ocr]"'
        )
        return 2

    placement = choose_placement(console, args.placement)
    if placement not in PLACEMENTS:
        console.print(f"[red]Unknown placement {placement!r}.[/red]")
        return 2
    candidates: dict[str, list[dict[str, Any]]] = {}
    for record in store.records():
        if args.item and record["item_uuid"] != args.item:
            continue
        if needs_ocr(record):
            candidates.setdefault(record["item_uuid"], []).append(record)
    console.print(
        f"{sum(len(v) for v in candidates.values())} PDF(s) in {len(candidates)} item(s) have "
        "image-only pages (OCR-001)."
    )
    if not candidates:
        return 0

    uploads = placement != "local"
    apply = uploads and args.apply
    if uploads and not apply:
        console.print(
            "[yellow]Dry run:[/yellow] PDFs are OCR'd and saved locally, but nothing is uploaded. "
            "Re-run with --apply to upload."
        )
    if apply:
        console.print(
            f"[bold red]This run uploads OCR'd PDFs ({placement})[/bold red]"
            + (
                f" and moves each original to the '{args.keep_bundle}' bundle"
                if placement == "replace"
                else ""
            )
            + ". Only originals that Anonymous can download are touched. Nothing is deleted."
        )
        if ask(console, f"Type '{placement}' to confirm") != placement:
            console.print("[yellow]Not confirmed; stopping.[/yellow]")
            return 1

    client = session.client
    local_dir = out / f"{host_slug(client.base_url)}-ocr"
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    max_bytes = int(args.max_size_mb * 1024 * 1024)

    with tempfile.TemporaryDirectory(prefix="pdf-a11y-ocr-") as tmp:
        tmpdir = Path(tmp)
        config = EngineConfig(workers=args.workers, timeout_seconds=args.timeout)
        async with CheckEngine(config) as engine:
            for item_uuid, records in candidates.items():
                await session.refresh()
                try:
                    item = await client.get_item(item_uuid)
                except DSpaceAPIError:
                    counts["item gone"] += 1
                    continue
                current = await pdf_bitstreams(
                    client, ScopedItem(item=item, state=item_state(item))
                )
                current_names = {t.name for t in current}
                for record in await still_present(client, item, records):
                    if ocr_name(record["bitstream_name"], "alongside") in current_names:
                        counts["ALREADY_DONE"] += 1
                        continue  # an OCR'd copy was uploaded next to it earlier
                    row = {
                        "item_uuid": item_uuid,
                        "item_title": first_value(item, "dc.title") or "(untitled)",
                        "bitstream_uuid": record["bitstream_uuid"],
                        "bitstream_filename": record["bitstream_name"],
                        "pages": record.get("pages", ""),
                        "placement": placement,
                    }
                    status, notes = await ocr_one(
                        console,
                        session,
                        engine,
                        record,
                        item,
                        row,
                        tmpdir,
                        local_dir,
                        placement,
                        apply,
                        args,
                        max_bytes,
                    )
                    row.update(status=status, notes=notes)
                    counts[status] += 1
                    rows.append(row)
                    style = {"UPLOADED": "green", "REPLACED": "green", "SAVED": "cyan"}.get(
                        status, "yellow"
                    )
                    console.print(
                        f"[{style}]{status:<16}[/{style}] {row['item_title'][:45]} / "
                        f"{record['bitstream_name'][:40]} {row.get('mean_confidence', '')}"
                    )

    path = out / f"{host_slug(client.base_url)}-{timestamp()}-ocr.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in CSV_COLUMNS})
    table = Table(title=f"OCR ({placement}{', applied' if apply else ''})")
    table.add_column("Outcome")
    table.add_column("PDFs", justify="right")
    for key, value in sorted(counts.items(), key=lambda kv: -kv[1]):
        table.add_row(key, str(value))
    console.print(table)
    console.print(f"Details: {path}\nOCR'd files: {local_dir}")
    console.print(
        "[dim]OCR adds text, not tags: the files still need tagging to pass. Run detect.py on the "
        "items afterwards to record the new files.[/dim]"
    )
    return 0


async def ocr_one(
    console: Console,
    session: Session,
    engine: CheckEngine,
    record: dict[str, Any],
    item: dict[str, Any],
    row: dict[str, Any],
    tmpdir: Path,
    local_dir: Path,
    placement: str,
    apply: bool,
    args: argparse.Namespace,
    max_bytes: int,
) -> tuple[str, str]:
    """OCR, validate and place one PDF. Returns (status, notes); fills ``row`` as it goes."""
    client = session.client
    original = tmpdir / f"{record['bitstream_uuid']}.pdf"
    try:
        await session.refresh()
        await client.download_bitstream(record["bitstream_uuid"], original, max_bytes=max_bytes)
    except DSpaceClientError as exc:
        return "DOWNLOAD_FAILED", str(exc)[:200]

    target = local_dir / item["uuid"] / ocr_name(record["bitstream_name"], "alongside")
    target.parent.mkdir(parents=True, exist_ok=True)
    pages = int(record.get("pages") or 0)
    timeout = max(args.timeout, SECONDS_PER_PAGE_ALLOWED * pages)
    lang = pdf_language(item, args.lang)
    try:
        outcome = await engine.ocr(original, target, lang, timeout=timeout)
        if outcome.status != "ok" or outcome.raw is None:
            return "OCR_FAILED", outcome.error or outcome.status
        summary = outcome.raw
        row.update(pages_ocrd=summary["pages_ocrd"], mean_confidence=summary["mean_confidence"])
        if not summary["pages_ocrd"]:
            return "NO_TEXT_FOUND", "OCR recognised no text on the image-only pages"
        row["local_file"] = str(target)

        before = await engine.check(original)
        after = await engine.check(target, source=original)
        if after.status != "ok" or after.raw is None or before.raw is None:
            return "NEEDS_REVIEW", f"could not check the OCR'd file: {after.error}"
        rules_before = {f["rule"] for f in before.raw.get("findings", [])}
        rules_after = {f["rule"] for f in after.raw.get("findings", [])}
        problems = sorted(
            r for r in rules_after - rules_before if r in REGRESSION_RULES or r.startswith("TXT-")
        )
        still_image_only = len((after.raw.get("ocr") or {}).get("pages") or [])
        notes = [f"lang {lang}", f"{still_image_only} page(s) still without text"]
        if problems:
            return "NEEDS_REVIEW", "; ".join([*notes, f"new findings: {', '.join(problems)}"])
        if summary["mean_confidence"] < args.min_confidence:
            return "NEEDS_REVIEW", "; ".join(
                [*notes, f"OCR confidence {summary['mean_confidence']} < {args.min_confidence}"]
            )
        if placement == "local":
            return "SAVED", "; ".join(notes)
        if not await client.anonymous_can_download(record["bitstream_uuid"]):
            return "SAVED_RESTRICTED", "; ".join(
                [*notes, "original is not public; upload by hand with matching access policies"]
            )
        if not apply:
            return f"WOULD_{'REPLACE' if placement == 'replace' else 'UPLOAD'}", "; ".join(notes)
        try:
            status, new_uuid, placed = await place_result(
                session, record, item, target, placement, args.keep_bundle, summary["engine"]
            )
        except DSpaceAPIError as exc:
            return "UPLOAD_FAILED", "; ".join([*notes, str(exc)[:200]])
        row["new_bitstream_uuid"] = new_uuid
        return status, "; ".join([*notes, placed])
    finally:
        original.unlink(missing_ok=True)
