"""
Find open-access full text by DOI and attach PDFs to DSpace items (ORIGINAL bundle).

Interactive order: attribution + Full Text Finder panel → DSpace URL/credentials → mode/UUID
→ preview question (Enter = see each PDF before upload; type No for automatic upload) → run.

Modes: [S]ingle / [B]ulk / [I]tem UUID. See README.

Env: see README. Logs: FULLTEXT_FINDER_LOG_DIR, files full_text_finder_*.log (gitignored).

Run from repo root::
  python examples/full-text-finder/main.py
  python examples/full-text-finder/main.py <item-uuid>
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import re
from pathlib import Path
from typing import Literal, TextIO
from urllib.parse import urlparse

import httpx
import typer
from config import ExternalApiConfig, load_external_config
from connect import connect_fulltext_client
from download import download_full_text
from dspace_candidates import (
    DEFAULT_DOI_FIELD,
    extract_doi_from_metadata,
    find_eligible_items,
    first_metadata_value,
    item_has_pdf_in_original,
)
from interactive import open_pdf_in_viewer, prompt_upload, write_temp_pdf
from logging_audit import log_line, open_audit_log
from resolve_chain import get_full_text_from_sources
from rich.console import Console
from rich.panel import Panel
from upload import (
    add_item_provenance,
    build_pdf_filename,
    build_provenance_statement,
    item_has_provenance_value,
    upload_pdf_bitstream,
    verify_bitstream_checksum,
)

from dspace_client import show_script_attribution
from dspace_client.exceptions import DSpaceAPIError

TARGET_VERSIONS = ["7.0", "8.0", "9.0", "10.0"]
SCRIPT_AUTHORS = "Bram Luyten (Atmire)"

DEFAULT_DISCOVERY_QUERY = "dc.identifier.doi:*"

# Default metadata field that holds the DOI, by server major version.
# DSpace 9 and older store the DOI in dc.identifier.doi; as of DSpace 10 the
# versioned-DOI workflow records it in dc.relation.hasversion.
DOI_FIELD_PRE_DSPACE10 = DEFAULT_DOI_FIELD  # "dc.identifier.doi"
DOI_FIELD_DSPACE10_PLUS = "dc.relation.hasversion"

app = typer.Typer(add_completion=False, help="Full-text finder for DSpace items with DOI, no PDF.")
console = Console()


def default_doi_field_for_version(server_version: str | None) -> str:
    """Return the default DOI metadata field for the detected server version."""
    if server_version:
        m = re.match(r"\s*(\d+)", server_version)
        if m and int(m.group(1)) >= 10:
            return DOI_FIELD_DSPACE10_PLUS
    return DOI_FIELD_PRE_DSPACE10


def _resolve_doi_field_interactive(default_field: str, server_version: str | None) -> str:
    """Prompt for the DOI metadata field; Enter keeps the version-based default."""
    ver = server_version or "unknown"
    ans = console.input(
        "[bold cyan]Which metadata field holds the DOI?[/bold cyan]\n"
        f"[dim](Detected server version: {ver}. Press Enter for default "
        f"'{default_field}', or type another field, e.g. dc.identifier.doi):[/dim] "
    ).strip()
    return ans or default_field


RunMode = Literal["single", "bulk", "item"]


async def _close_auth_session(auth_client) -> None:
    """Close the auth client (runs session-end Atmire messaging)."""
    await auth_client.close()


async def process_item(
    *,
    client,
    ext_http: httpx.AsyncClient,
    cfg: ExternalApiConfig,
    item_uuid: str,
    doi: str,
    title: str,
    mode_label: str,
    log_file: TextIO | None,
    dry_run: bool,
    no_user_verify: bool,
    skip_open: bool,
    admin_email: str,
) -> str:
    """
    Returns outcome: uploaded, skipped, failed, dry_run, quit_requested (only from prompt).
    """
    log_line(
        log_file,
        f"START item={item_uuid} doi={doi!r} mode={mode_label} title={title[:200]!r}",
    )
    hit, ctx = await get_full_text_from_sources(ext_http, doi, cfg)
    if ctx.openalex_summary:
        log_line(log_file, f"INFO item={item_uuid} openalex={ctx.openalex_summary[:500]!r}")
    if not hit:
        console.print(f"[yellow]No full-text URL found for {doi}[/yellow]")
        log_line(log_file, f"SKIP item={item_uuid} reason=no_url_found")
        return "skipped"

    console.print(f"[green]Resolved[/green] {hit.provenance} → {hit.url[:120]}…")
    log_line(
        log_file,
        f"RESOLVED item={item_uuid} provenance={hit.provenance!r} url={hit.url!r}",
    )

    if dry_run:
        console.print("[cyan]DRY_RUN — would download and upload PDF[/cyan]")
        log_line(
            log_file,
            f"DRY_RUN item={item_uuid} would_upload_from={hit.url!r} provenance={hit.provenance!r}",
        )
        return "dry_run"

    try:
        data, _download_name = await download_full_text(
            ext_http, hit.url, timeout_s=cfg.timeout_seconds
        )
    except Exception as e:
        console.print(f"[red]Download failed: {e}[/red]")
        log_line(log_file, f"FAIL item={item_uuid} reason=download_error detail={e!s}")
        return "failed"

    temp_path: Path | None = None
    try:
        filename = build_pdf_filename(hit.provenance)
        temp_path = write_temp_pdf(data, prefix=f"ft_{item_uuid[:8]}_")

        if not no_user_verify:
            if not skip_open:
                open_pdf_in_viewer(temp_path, console)
            action = prompt_upload(console)
            if action == "quit":
                log_line(log_file, f"QUIT item={item_uuid} reason=user_quit")
                return "quit_requested"
            if action != "upload":
                log_line(log_file, f"SKIP item={item_uuid} reason=user_declined")
                return "skipped"

        bs = await upload_pdf_bitstream(client, item_uuid, filename, data)
        bs_uuid = bs.get("uuid", "")
        console.print(
            f"[green]Uploaded bitstream[/green] {bs_uuid} as {filename} ({len(data)} bytes)"
        )
        log_line(
            log_file,
            f"UPLOAD item={item_uuid} doi={doi!r} bitstream_uuid={bs_uuid} "
            f"filename={filename!r} bytes={len(data)} provenance={hit.provenance!r} "
            f"url={hit.url!r}",
        )

        checksum = verify_bitstream_checksum(bs, data)
        if checksum.comparable and not checksum.matched:
            console.print(
                f"[bold red]CHECKSUM MISMATCH[/bold red] for item {item_uuid} bitstream "
                f"{bs_uuid}: DSpace stored {checksum.algorithm}={checksum.stored} but the "
                f"uploaded bytes hash to {checksum.local}. The stored file may be corrupt; "
                "verify the bitstream in DSpace."
            )
            log_line(
                log_file,
                f"CHECKSUM_MISMATCH item={item_uuid} bitstream_uuid={bs_uuid} "
                f"algorithm={checksum.algorithm} stored={checksum.stored!r} local={checksum.local!r}",
            )
        elif not checksum.comparable:
            console.print(
                f"[yellow]Could not verify checksum (algorithm={checksum.algorithm}); "
                "no comparable value available.[/yellow]"
            )
            log_line(
                log_file,
                f"CHECKSUM_UNVERIFIED item={item_uuid} bitstream_uuid={bs_uuid} "
                f"algorithm={checksum.algorithm} stored={checksum.stored!r} local={checksum.local!r}",
            )
        else:
            console.print(
                f"[green]Checksum verified[/green] ({checksum.algorithm}={checksum.stored})"
            )
            log_line(
                log_file,
                f"CHECKSUM_OK item={item_uuid} bitstream_uuid={bs_uuid} "
                f"algorithm={checksum.algorithm} value={checksum.stored!r}",
            )

        statement = build_provenance_statement(
            admin_email=admin_email,
            source=hit.provenance,
            url=hit.url,
            inspected=not no_user_verify,
            checksum=checksum.checksum,
            checksum_algorithm=checksum.algorithm,
        )
        try:
            await add_item_provenance(client, item_uuid, statement)
            # Confirm it persisted: DSpace filters language-less provenance values out
            # of the representation, so we write with language "en" and verify here.
            if await item_has_provenance_value(client, item_uuid, statement):
                console.print("[green]Recorded provenance metadata on the item.[/green]")
                log_line(
                    log_file,
                    f"PROVENANCE item={item_uuid} bitstream_uuid={bs_uuid} statement={statement!r}",
                )
            else:
                console.print(
                    "[yellow]Provenance write returned OK but the value was not visible "
                    "on re-fetch.[/yellow]"
                )
                log_line(
                    log_file,
                    f"PROVENANCE_UNCONFIRMED item={item_uuid} bitstream_uuid={bs_uuid} "
                    f"statement={statement!r}",
                )
        except Exception as e:
            console.print(f"[yellow]Bitstream uploaded but provenance write failed: {e}[/yellow]")
            log_line(
                log_file,
                f"WARN item={item_uuid} reason=provenance_write_failed detail={e!s}",
            )

        return "uploaded"
    except DSpaceAPIError as e:
        console.print(f"[red]DSpace upload failed: {e}[/red]")
        log_line(log_file, f"FAIL item={item_uuid} reason=dspace_api detail={e!s}")
        return "failed"
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        log_line(log_file, f"FAIL item={item_uuid} reason=error detail={e!s}")
        return "failed"
    finally:
        if temp_path is not None and temp_path.exists():
            with contextlib.suppress(OSError):
                temp_path.unlink()


def _resolve_no_user_verify_interactive(*, dry_run: bool, no_user_verify_flag: bool) -> bool:
    """
    Return True to skip human verification (upload PDFs without opening each one first).

    Interactive default (Enter): **False** — user sees each PDF and confirms before upload.
    Type ``no`` or ``n`` to add PDFs automatically without preview.
    ``--no-user-verify`` forces True (no preview).
    """
    if no_user_verify_flag:
        console.print("[dim]Using --no-user-verify: PDFs are uploaded without preview.[/dim]")
        return True
    if dry_run:
        console.print("[dim]Dry-run: no uploads; skipping preview question.[/dim]")
        return True
    ans = (
        console.input(
            "[bold cyan]Do you want to see each individual PDF before it gets added to the item?[/bold cyan]\n"
            "[dim](Press Enter for Yes — type No to add PDFs automatically without preview):[/dim] "
        )
        .strip()
        .lower()
    )
    return ans in ("no", "n")


def _resolve_run_mode_interactive() -> tuple[RunMode, str | None]:
    """Ask for mode and optional item UUID (after repository URL is known)."""
    choice = (
        console.input(
            "[bold cyan]Mode[/bold cyan] [dim]([S]ingle first match / [B]ulk / [I]tem UUID):[/dim] "
        )
        .strip()
        .lower()
    )
    if choice in ("b", "bulk", "r", "repository"):
        return "bulk", None
    if choice in ("i", "item", "u", "uuid"):
        uid = console.input("[bold cyan]Item UUID:[/bold cyan] ").strip()
        if not uid:
            console.print("[red]No UUID provided.[/red]")
            raise SystemExit(2)
        return "item", uid
    # default single
    return "single", None


async def run_async(
    cli_mode: str | None,
    cli_item_uuid: str | None,
    discovery_query: str,
    max_items: int | None,
    dry_run: bool,
    no_user_verify_flag: bool,
    skip_open: bool,
    courtesy_delay: float,
    strict_versions: bool,
    cli_doi_field: str | None,
) -> None:
    # 1) Attribution and script description first
    show_script_attribution(SCRIPT_AUTHORS, console=console)
    console.print(
        Panel.fit(
            "For your metadata-only items that DO have a DOI, this script can try to find the "
            "full text in Unpaywall, OpenAlex, OpenAIRE or CORE, and subsequently attach the "
            "retrieved full text to your DSpace items.",
            title="Full Text Finder",
        )
    )

    # 2) Repository URL and credentials (before mode / UUID)
    base_url = (
        console.input(
            "[bold cyan]DSpace base URL[/bold cyan] [dim](Enter for https://demo.dspace.org):[/dim] "
        ).strip()
        or "https://demo.dspace.org"
    )
    if urlparse(base_url).hostname == "demo.dspace.org":
        console.print("[dim]Using demo admin account.[/dim]")
        username = "dspacedemo+admin@gmail.com"
        password = "dspace"
    else:
        username = console.input("[bold cyan]Username:[/bold cyan] ").strip()
        password = getpass.getpass("Password: ")

    try:
        ext_cfg = load_external_config(console, prompt=True)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(2) from e

    # 3) Mode and optional UUID (CLI or interactive)
    resolved_mode: RunMode
    item_uuid_arg: str | None
    if cli_item_uuid:
        resolved_mode = "item"
        item_uuid_arg = cli_item_uuid.strip()
        if not item_uuid_arg:
            console.print("[red]Empty item UUID.[/red]")
            raise SystemExit(2)
    elif cli_mode is not None:
        m = cli_mode.lower().strip()
        if m not in ("single", "bulk", "item"):
            console.print(f"[red]Invalid --mode: {cli_mode}[/red]")
            raise SystemExit(2)
        resolved_mode = m  # type: ignore[assignment]
        if resolved_mode == "item":
            item_uuid_arg = console.input("[bold cyan]Item UUID:[/bold cyan] ").strip()
            if not item_uuid_arg:
                console.print("[red]Item mode requires a UUID.[/red]")
                raise SystemExit(2)
        else:
            item_uuid_arg = None
    else:
        resolved_mode, item_uuid_arg = _resolve_run_mode_interactive()

    # 4) Preview each PDF vs automatic upload (Enter = see each PDF first)
    no_user_verify = _resolve_no_user_verify_interactive(
        dry_run=dry_run,
        no_user_verify_flag=no_user_verify_flag,
    )

    log_f, log_path = open_audit_log()
    if log_path:
        console.print(f"[dim]Audit log: {log_path}[/dim]")
        log_line(
            log_f,
            f"CONFIG mode={resolved_mode} skip_pdf_preview={no_user_verify} dry_run={dry_run}",
        )
    else:
        console.print("[yellow]Could not open audit log file.[/yellow]")

    auth, client = await connect_fulltext_client(
        base_url,
        username,
        password,
        TARGET_VERSIONS,
        strict_versions=strict_versions,
        courtesy_delay=courtesy_delay,
    )

    pdf_format_id = await client.resolve_pdf_format_id()
    if pdf_format_id is None:
        console.print("[red]Could not resolve PDF format id from server registry.[/red]")
        await _close_auth_session(auth)
        raise SystemExit(1)

    # Resolve which metadata field holds the DOI (version-aware default, overridable).
    server_version = client.last_detected_server_version or await client.detect_dspace_version()
    default_doi_field = default_doi_field_for_version(server_version)
    if cli_doi_field is not None:
        doi_field = cli_doi_field.strip() or default_doi_field
        console.print(f"[dim]Using DOI field from --doi-field: {doi_field}[/dim]")
    else:
        doi_field = _resolve_doi_field_interactive(default_doi_field, server_version)
    console.print(f"[dim]Reading DOI from metadata field: {doi_field}[/dim]")
    log_line(log_f, f"CONFIG doi_field={doi_field} server_version={server_version or 'unknown'}")
    if discovery_query == DEFAULT_DISCOVERY_QUERY:
        discovery_query = f"{doi_field}:*"

    timeout = httpx.Timeout(ext_cfg.timeout_seconds)
    limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)

    try:
        async with httpx.AsyncClient(timeout=timeout, limits=limits) as ext_http:
            if resolved_mode == "item":
                uid = (item_uuid_arg or "").strip()
                if not uid:
                    console.print("[red]Item mode requires a UUID.[/red]")
                    return
                try:
                    full = await client.get_item(uid)
                except DSpaceAPIError as e:
                    console.print(f"[red]get_item failed: {e}[/red]")
                    return
                metadata = full.get("metadata") or {}
                doi = extract_doi_from_metadata(metadata, doi_field)
                title = first_metadata_value(metadata, "dc.title") or "(no title)"
                if not doi:
                    console.print(
                        f"[red]Item has no usable DOI in metadata field '{doi_field}'.[/red]"
                    )
                    log_line(log_f, f"SKIP item={uid} reason=no_doi doi_field={doi_field}")
                    return
                has_pdf = await item_has_pdf_in_original(client, uid, pdf_format_id)
                if has_pdf:
                    console.print(
                        "[yellow]Item already has a PDF in ORIGINAL; nothing to do.[/yellow]"
                    )
                    log_line(log_f, f"SKIP item={uid} reason=already_has_pdf")
                    return
                outcome = await process_item(
                    client=client,
                    ext_http=ext_http,
                    cfg=ext_cfg,
                    item_uuid=uid,
                    doi=doi,
                    title=title,
                    mode_label="item",
                    log_file=log_f,
                    dry_run=dry_run,
                    no_user_verify=no_user_verify,
                    skip_open=skip_open,
                    admin_email=username,
                )
                if outcome == "quit_requested":
                    return
                return

            single = resolved_mode == "single"
            stats = {"uploaded": 0, "skipped": 0, "failed": 0, "dry_run": 0}

            n = 0
            async for uid, doi, full in find_eligible_items(
                client,
                pdf_format_id,
                query=discovery_query,
                max_items=max_items,
                single=single,
                doi_field=doi_field,
            ):
                n += 1
                metadata = full.get("metadata") or {}
                title = first_metadata_value(metadata, "dc.title") or "(no title)"
                console.print(
                    Panel(f"{title}\n[dim]{doi}[/dim]\n[dim]{uid}[/dim]", title="Candidate")
                )
                outcome = await process_item(
                    client=client,
                    ext_http=ext_http,
                    cfg=ext_cfg,
                    item_uuid=uid,
                    doi=doi,
                    title=title,
                    mode_label=resolved_mode,
                    log_file=log_f,
                    dry_run=dry_run,
                    no_user_verify=no_user_verify,
                    skip_open=skip_open,
                    admin_email=username,
                )
                if outcome == "quit_requested":
                    break
                if outcome == "uploaded":
                    stats["uploaded"] += 1
                elif outcome == "skipped":
                    stats["skipped"] += 1
                elif outcome == "failed":
                    stats["failed"] += 1
                elif outcome == "dry_run":
                    stats["dry_run"] += 1

            if n == 0:
                console.print(
                    f"[yellow]No eligible items found (DOI in '{doi_field}', no PDF in ORIGINAL).[/yellow]"
                )

            console.print(
                f"[bold]Done.[/bold] uploaded={stats['uploaded']} skipped={stats['skipped']} "
                f"failed={stats['failed']} dry_run={stats['dry_run']}"
            )
    finally:
        if log_f:
            log_line(log_f, "END session")
            with contextlib.suppress(Exception):
                log_f.close()
        await _close_auth_session(auth)


@app.command()
def main(
    item_uuid: str | None = typer.Argument(None, help="Item UUID (implies item mode when set)"),
    mode: str | None = typer.Option(
        None,
        "--mode",
        "-m",
        help="single | bulk | item (default: interactive prompt unless UUID is set)",
    ),
    discovery_query: str = typer.Option(
        DEFAULT_DISCOVERY_QUERY,
        "--discovery-query",
        "-q",
        help="Discovery Lucene query (default: items with a DOI field)",
    ),
    doi_field: str | None = typer.Option(
        None,
        "--doi-field",
        help=(
            "Metadata field holding the DOI (default: interactive prompt; "
            "dc.identifier.doi for DSpace <=9, dc.relation.hasversion for DSpace 10+)"
        ),
    ),
    max_items: int | None = typer.Option(
        None,
        "--max-items",
        help="Max eligible items to process in bulk mode",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Resolve URLs only; do not upload"),
    no_user_verify: bool = typer.Option(
        False,
        "--no-user-verify",
        help="Skip preview: upload each PDF without opening it (Enter in UI defaults to preview first)",
    ),
    skip_open: bool = typer.Option(
        False,
        "--skip-open",
        help="With user verify: do not open OS viewer (still prompt)",
    ),
    courtesy_delay: float = typer.Option(
        1.0,
        "--courtesy-delay",
        help="Seconds between DSpace API calls",
    ),
    strict_versions: bool = typer.Option(
        False,
        "--strict-versions",
        help="Run verify_server_version after login",
    ),
) -> None:
    """CLI entry (attribution, URL, and mode prompts run inside run_async in order)."""
    asyncio.run(
        run_async(
            cli_mode=mode,
            cli_item_uuid=item_uuid,
            discovery_query=discovery_query,
            max_items=max_items,
            dry_run=dry_run,
            no_user_verify_flag=no_user_verify,
            skip_open=skip_open,
            courtesy_delay=courtesy_delay,
            strict_versions=strict_versions,
            cli_doi_field=doi_field,
        )
    )


if __name__ == "__main__":
    app()
