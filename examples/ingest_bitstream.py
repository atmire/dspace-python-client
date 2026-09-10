"""Upload a local file as a bitstream on an item's ORIGINAL bundle.

Creates the ORIGINAL bundle if the item does not have one yet. Does not set
authorization policies on the bitstream.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import unicodedata
import uuid as uuid_mod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console

from dspace_client import (
    ServerVersionMismatchError,
    create_validated_client,
    show_script_attribution,
)
from dspace_client.exceptions import DSpaceAPIError

TARGET_VERSIONS = ["7.6", "8.0", "9.0", "10.0"]
SCRIPT_AUTHORS = "Bram Luyten (Atmire)"

console = Console()

WARN_IN_MEMORY_BYTES = 100 * 1024 * 1024
MAX_BASENAME_LEN = 240
_KIBIBYTE = 1024
HTTP_NOT_FOUND = 404
_FIRST_PRINTABLE_ASCII = 32
_MIN_LEN_FOR_QUOTE_PAIR = 2


def _strip_wrapping_quotes(s: str) -> str:
    """
    Remove one layer of surrounding quotes from copy-paste (e.g. '/path/file' or "path").

    Handles ASCII ' " and common Unicode curly quotes.
    """
    t = s.strip()
    if len(t) < _MIN_LEN_FOR_QUOTE_PAIR:
        return t
    if t[0] == t[-1] and t[0] in "'\"":
        return t[1:-1].strip()
    if t[0] == "\u2018" and t[-1] == "\u2019":
        return t[1:-1].strip()
    if t[0] == "\u201c" and t[-1] == "\u201d":
        return t[1:-1].strip()
    return t


@dataclass(frozen=True)
class _UploadContext:
    item_uuid: str
    resolved_path: Path
    upload_name: str
    file_size: int


def _extract_dc_title(item: dict) -> str | None:
    meta = item.get("metadata") or {}
    for row in meta.get("dc.title") or []:
        v = (row.get("value") or "").strip()
        if v:
            return v
    return None


def format_size(num_bytes: int) -> str:
    if num_bytes < _KIBIBYTE:
        return f"{num_bytes} B"
    if num_bytes < _KIBIBYTE**2:
        return f"{num_bytes / _KIBIBYTE:.1f} KiB"
    if num_bytes < _KIBIBYTE**3:
        return f"{num_bytes / (_KIBIBYTE**2):.1f} MiB"
    return f"{num_bytes / (_KIBIBYTE**3):.1f} GiB"


def _validate_upload_basename(name: str, resolved_name: str) -> None:
    msg = "Invalid filename (empty or '.' / '..')."
    if not name or name in (".", ".."):
        raise ValueError(msg)

    if name != resolved_name and console.is_terminal:
        console.print(f"[dim]Filename normalized to NFC for upload: {name!r}[/dim]")

    msg = "Filename must not contain path separators or null bytes."
    if any(c in name for c in ("/", "\\")) or "\0" in name:
        raise ValueError(msg)

    msg = "Filename must not contain ASCII control characters."
    if any(ord(c) < _FIRST_PRINTABLE_ASCII for c in name):
        raise ValueError(msg)

    msg = f"Filename is too long ({len(name)} chars); max {MAX_BASENAME_LEN} for this script."
    if len(name) > MAX_BASENAME_LEN:
        raise ValueError(msg)

    msg = "Filename has leading or trailing whitespace."
    if name != name.strip():
        raise ValueError(msg)

    msg = "Filename must not end with a dot."
    if name.rstrip(".") != name:
        raise ValueError(msg)


def validate_local_file(path: Path) -> tuple[Path, str, int]:
    """
    Resolve path, ensure it is a readable non-empty file, return (resolved, basename, size).

    basename is NFC-normalized for upload.
    """
    resolved = path.expanduser().resolve(strict=False)
    msg = f"Path does not exist: {path}"
    if not resolved.exists():
        raise ValueError(msg)

    msg = f"Path is a directory, not a file: {resolved}"
    if resolved.is_dir():
        raise ValueError(msg)

    msg = f"Path is not a regular file: {resolved}"
    if not resolved.is_file():
        raise ValueError(msg)

    st = resolved.stat()
    msg = "File has empty size (must be non-zero)."
    if st.st_size <= 0:
        raise ValueError(msg)

    msg = f"File is not readable: {resolved}"
    if not os.access(resolved, os.R_OK):
        raise ValueError(msg)

    name = unicodedata.normalize("NFC", resolved.name)
    _validate_upload_basename(name, resolved.name)

    return resolved, name, st.st_size


async def _get_json_paged(
    client,
    endpoint_base: str,
    list_key_embed: tuple[str, str],
) -> list:
    out: list = []
    page = 0
    size = 100
    embed_key, alt_key = list_key_embed
    while True:
        resp = await client._request(
            "GET",
            endpoint_base,
            params={"page": page, "size": size},
        )
        data = resp.json()
        batch = data.get(alt_key) or data.get("_embedded", {}).get(embed_key, [])
        out.extend(batch)
        if len(batch) < size:
            break
        page += 1
    return out


async def ensure_original_bundle_uuid(client, item_uuid: str) -> str:
    """Return UUID of the item's ORIGINAL bundle, creating it if missing."""
    bundle_path = f"core/items/{item_uuid}/bundles"
    bundles = await _get_json_paged(client, bundle_path, ("bundles", "bundles"))
    for b in bundles:
        if (b.get("name") or "").upper() == "ORIGINAL":
            uid = b.get("uuid")
            if uid:
                return str(uid)

    console.print("[yellow]No ORIGINAL bundle on this item; creating one…[/yellow]")
    created = await client.create_bundle(item_uuid, "ORIGINAL")
    uid = created.get("uuid")
    if not uid:
        msg = "create_bundle did not return a uuid"
        raise DSpaceAPIError(msg, status_code=None)
    return str(uid)


async def bundle_has_bitstream_name(client, bundle_uuid: str, filename: str) -> bool:
    """True if any bitstream in the bundle has the same name (exact string)."""
    bs_path = f"core/bundles/{bundle_uuid}/bitstreams"
    bitstreams = await _get_json_paged(client, bs_path, ("bitstreams", "bitstreams"))
    return any((bs.get("name") or "") == filename for bs in bitstreams)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Upload a local file to a DSpace item's ORIGINAL bundle (creates the bundle if absent)."
        ),
        epilog=(
            "Interactive order: repository URL, credentials, then (if not passed as "
            "arguments) item UUID and local file path."
        ),
    )
    p.add_argument(
        "item_uuid",
        nargs="?",
        default=None,
        help="UUID of an existing item (omit to be prompted)",
    )
    p.add_argument(
        "file_path",
        nargs="?",
        default=None,
        help="Path to a local file to upload (omit to be prompted)",
    )
    return p.parse_args()


async def _run_upload_after_auth(client, ctx: _UploadContext) -> None:
    console.print(f"\n[bold]Loading item[/bold] [cyan]{ctx.item_uuid}[/cyan] …")
    try:
        item = await client.get_item(ctx.item_uuid)
    except DSpaceAPIError as e:
        if e.status_code == HTTP_NOT_FOUND:
            console.print("[red]Item not found (404).[/red]")
        else:
            console.print(f"[red]Failed to load item:[/red] {e}")
        return

    title = _extract_dc_title(item)
    handle = item.get("handle") or "(none)"
    console.print(f"[dim]dc.title:[/dim] {title or '(none)'}")
    console.print(f"[dim]handle:[/dim] {handle}")

    console.print("\n[bold]Ensuring ORIGINAL bundle…[/bold]")
    try:
        bundle_uuid = await ensure_original_bundle_uuid(client, ctx.item_uuid)
    except DSpaceAPIError as e:
        console.print(f"[red]Could not resolve or create ORIGINAL bundle:[/red] {e}")
        return
    console.print(f"  [green]ORIGINAL bundle UUID:[/green] [cyan]{bundle_uuid}[/cyan]")

    if await bundle_has_bitstream_name(client, bundle_uuid, ctx.upload_name):
        console.print(
            f"[yellow]Warning:[/yellow] A bitstream named [bold]{ctx.upload_name!r}[/bold] "
            "already exists in ORIGINAL; upload will add another with the same name."
        )

    console.print("\n[bold]Ready to upload[/bold]")
    console.print(f"  [dim]Local file:[/dim] {ctx.resolved_path}")
    console.print(f"  [dim]Upload name:[/dim] {ctx.upload_name}")
    sz_line = (
        f"  [dim]Size (added storage):[/dim] {format_size(ctx.file_size)} ({ctx.file_size} bytes)"
    )
    console.print(sz_line)

    prompt = "\n[bold yellow]Upload this file? (yes/no):[/bold yellow] "
    proceed = console.input(prompt).strip().lower()
    if proceed not in ("yes", "y"):
        console.print("[dim]Cancelled.[/dim]")
        return

    try:
        content = ctx.resolved_path.read_bytes()
    except OSError as e:
        console.print(f"[red]Could not read file:[/red] {e}")
        return

    console.print("\n[bold]Uploading…[/bold]")
    try:
        bitstream = await client.upload_bitstream(
            bundle_uuid=bundle_uuid,
            filename=ctx.upload_name,
            content=content,
        )
    except DSpaceAPIError as e:
        console.print(f"[red]Upload failed:[/red] {e}")
        return

    bs_uuid = bitstream.get("uuid", "")
    line = f"[green]Uploaded bitstream[/green] [cyan]{bs_uuid}[/cyan] name={ctx.upload_name!r}"
    console.print(line)


def _print_ingest_banner() -> None:
    show_script_attribution(SCRIPT_AUTHORS, console=console)
    console.print("\n[bold cyan]Ingest bitstream (ORIGINAL bundle)[/bold cyan]")
    console.print("[dim]" + "━" * 50 + "[/dim]")
    console.print(
        "[dim]Uploads one file to the item's ORIGINAL bundle (creates the bundle if needed). "
        "Does not set bitstream authorization policies.[/dim]"
    )
    console.print(f"[bold]Supported versions:[/bold] {', '.join(TARGET_VERSIONS)}")
    console.print("[dim]" + "━" * 50 + "[/dim]\n")


async def _prompt_url_credentials_and_connect() -> tuple[Any, Any] | None:
    supported_str = ", ".join(TARGET_VERSIONS)
    url_prompt = (
        "[bold cyan]DSpace base URL[/bold cyan] [dim](Enter for https://demo.dspace.org; "
        f"targets {supported_str}):[/dim] "
    )
    base_url = console.input(url_prompt).strip()
    if not base_url:
        base_url = "https://demo.dspace.org"
        console.print("[dim]→ Using default: https://demo.dspace.org[/dim]")

    base_url_normalized = base_url.rstrip("/").lower()
    is_demo = "demo.dspace.org" in base_url_normalized
    if is_demo:
        console.print("[dim]Using demo credentials: dspacedemo+admin@gmail.com[/dim]")
        username = "dspacedemo+admin@gmail.com"
        password = "dspace"
    else:
        username = console.input("[bold cyan]Username:[/bold cyan] ").strip()
        password = getpass.getpass("Password: ")

    try:
        return await create_validated_client(
            base_url=base_url,
            username=username,
            password=password,
            target_versions=TARGET_VERSIONS,
            show_atmire_promo=True,
        )
    except ServerVersionMismatchError as e:
        console.print(f"[red]Version mismatch:[/red] {e}")
        console.print(f"[yellow]This script targets: {supported_str}[/yellow]")
        return None


def _gather_upload_targets(args: argparse.Namespace) -> _UploadContext | None:
    raw_uuid = _strip_wrapping_quotes(args.item_uuid or "") or None
    raw_path = _strip_wrapping_quotes(args.file_path or "") or None

    if not raw_uuid:
        raw_uuid = (
            _strip_wrapping_quotes(console.input("[bold cyan]Item UUID:[/bold cyan] ")) or None
        )
    if not raw_path:
        raw_path = (
            _strip_wrapping_quotes(console.input("[bold cyan]Path to local file:[/bold cyan] "))
            or None
        )

    if not raw_uuid:
        console.print("[red]Item UUID is required.[/red]")
        return None
    if not raw_path:
        console.print("[red]File path is required.[/red]")
        return None

    try:
        item_uuid = str(uuid_mod.UUID(raw_uuid))
    except ValueError:
        console.print("[red]Invalid item UUID syntax.[/red]")
        return None

    try:
        resolved_path, upload_name, file_size = validate_local_file(Path(raw_path))
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return None

    if file_size >= WARN_IN_MEMORY_BYTES:
        console.print(
            f"[yellow]Note:[/yellow] File is {format_size(file_size)}; "
            "the client loads the full file into memory for upload."
        )

    return _UploadContext(
        item_uuid=item_uuid,
        resolved_path=resolved_path,
        upload_name=upload_name,
        file_size=file_size,
    )


async def main() -> None:
    args = parse_args()
    _print_ingest_banner()

    connected = await _prompt_url_credentials_and_connect()
    if connected is None:
        return
    auth, client = connected

    try:
        ctx = _gather_upload_targets(args)
        if ctx is None:
            return
        await _run_upload_after_auth(client, ctx)
    finally:
        await auth.close()


if __name__ == "__main__":
    asyncio.run(main())
