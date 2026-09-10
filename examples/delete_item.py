"""Delete a single DSpace item with pre-inventory and post-delete verification."""

import asyncio
import getpass
import uuid as uuid_mod

from rich.console import Console

from dspace_client import (
    ServerVersionMismatchError,
    create_validated_client,
    show_script_attribution,
)
from dspace_client.exceptions import DSpaceAPIError

# DEVELOPER DECLARES: DSpace 7.6 and 10.0 (delete endpoint is unchanged across versions; verified against demo.dspace.org on DSpace 10)
TARGET_VERSIONS = ["7.6", "10.0"]
SCRIPT_AUTHORS = "Bram Luyten (Atmire)"

console = Console()

VERIFY_WAIT_SECONDS = 10


def _extract_dc_title(item: dict) -> str | None:
    """First dc.title value, or None if absent/empty."""
    meta = item.get("metadata") or {}
    for row in meta.get("dc.title") or []:
        v = (row.get("value") or "").strip()
        if v:
            return v
    return None


def _confirmation_phrase(item: dict) -> tuple[str, str]:
    """
    Returns (phrase_user_must_type, human_description_for_banner).

    If dc.title exists, the user must re-type that exact string.
    Otherwise they must type DELETE (uppercase).
    """
    title = _extract_dc_title(item)
    if title is not None:
        return title, "the exact dc.title shown above (copy carefully)"
    name = (item.get("name") or "").strip()
    if name:
        return "DELETE", f'no dc.title; type DELETE to confirm deletion of name "{name}"'
    return "DELETE", "no dc.title or name; type DELETE to confirm"


async def _get_json_paged(
    client,
    endpoint_base: str,
    list_key_embed: tuple[str, str],
) -> list:
    """GET hal+json pages until a short page; endpoint_base without trailing slash."""
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


async def collect_inventory(client, item_uuid: str) -> tuple[list[dict], list[dict]]:
    """
    Returns (bundles_records, bitstream_records) where each record has uuid, name;
    bitstream rows include bundle_uuid.
    """
    bundle_path = f"core/items/{item_uuid}/bundles"
    bundles = await _get_json_paged(client, bundle_path, ("bundles", "bundles"))
    bundle_rows: list[dict] = []
    bitstream_rows: list[dict] = []

    for b in bundles:
        bu = b.get("uuid")
        if not bu:
            continue
        bname = b.get("name") or ""
        bundle_rows.append({"uuid": bu, "name": bname})
        console.print(f"  [dim]Bundle[/dim] [cyan]{bu}[/cyan] [bold]{bname}[/bold]")

        bs_path = f"core/bundles/{bu}/bitstreams"
        bitstreams = await _get_json_paged(client, bs_path, ("bitstreams", "bitstreams"))
        for bs in bitstreams:
            bsu = bs.get("uuid")
            if not bsu:
                continue
            bsname = bs.get("name") or ""
            bitstream_rows.append({"uuid": bsu, "name": bsname, "bundle_uuid": bu})
            console.print(f"    [dim]Bitstream[/dim] [cyan]{bsu}[/cyan] [bold]{bsname}[/bold]")

    return bundle_rows, bitstream_rows


async def http_get_status(client, api_path: str) -> int:
    """GET without using DSpaceClient._request (avoids error console spam on 404)."""
    url = f"{client.base_url}/server/api/{api_path.lstrip('/')}"
    resp = await client.client.get(
        url,
        headers={"Authorization": f"Bearer {client.jwt_token}"},
    )
    return resp.status_code


async def verify_gone(
    client,
    item_uuid: str,
    bundles: list[dict],
    bitstreams: list[dict],
) -> bool:
    """Return True if every stored object returns 404."""
    ok = True

    console.print("\n[bold]Verifying bitstreams are gone[/bold]")
    for row in bitstreams:
        uid = row["uuid"]
        code = await http_get_status(client, f"core/bitstreams/{uid}")
        if code == 404:
            console.print(f"  [green]OK[/green]  bitstream {uid} → 404")
        else:
            console.print(f"  [red]FAIL[/red] bitstream {uid} → HTTP {code} (expected 404)")
            ok = False

    console.print("\n[bold]Verifying bundles are gone[/bold]")
    for row in bundles:
        uid = row["uuid"]
        code = await http_get_status(client, f"core/bundles/{uid}")
        if code == 404:
            console.print(f"  [green]OK[/green]  bundle {uid} → 404")
        else:
            console.print(f"  [red]FAIL[/red] bundle {uid} → HTTP {code} (expected 404)")
            ok = False

    console.print("\n[bold]Verifying item is gone[/bold]")
    icode = await http_get_status(client, f"core/items/{item_uuid}")
    if icode == 404:
        console.print(f"  [green]OK[/green]  item {item_uuid} → 404")
    else:
        console.print(f"  [red]FAIL[/red] item {item_uuid} → HTTP {icode} (expected 404)")
        ok = False

    return ok


async def main() -> None:
    show_script_attribution(SCRIPT_AUTHORS, console=console)
    console.print("\n[bold cyan]Delete single DSpace item (with verification)[/bold cyan]")
    console.print("[dim]" + "━" * 50 + "[/dim]")
    console.print(
        "[yellow]⚠️  This script PERMANENTLY deletes one item and checks that "
        "bundles/bitstreams disappear via the REST API.[/yellow]"
    )
    console.print(
        "[bold]Required access:[/bold] permission to delete the target item (often admin)."
    )
    console.print("[bold]Supported versions:[/bold] " + ", ".join(TARGET_VERSIONS))
    console.print(
        "[dim]Note: DELETE /api/core/items/{uuid} removes child bundles and bitstreams server-side; "
        "this script inventories them first, then confirms removal after a short wait.[/dim]"
    )
    console.print("[dim]" + "━" * 50 + "[/dim]\n")

    proceed = console.input("[bold yellow]Continue? (yes/no):[/bold yellow] ").strip().lower()
    if proceed not in ("yes", "y"):
        console.print("[dim]Cancelled.[/dim]")
        return

    supported_str = ", ".join(TARGET_VERSIONS)
    base_url = console.input(
        f"[bold cyan]DSpace base URL[/bold cyan] [dim](Enter for https://demo.dspace.org; targets DSpace {supported_str}):[/dim] "
    ).strip()
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
        auth, client = await create_validated_client(
            base_url=base_url,
            username=username,
            password=password,
            target_versions=TARGET_VERSIONS,
            show_atmire_promo=True,
        )
    except ServerVersionMismatchError as e:
        console.print(f"[red]Version mismatch:[/red] {e}")
        console.print(f"[yellow]This script targets: {supported_str}[/yellow]")
        return

    raw = console.input("[bold cyan]Item UUID to delete:[/bold cyan] ").strip()
    try:
        item_uuid = str(uuid_mod.UUID(raw))
    except ValueError:
        console.print("[red]Invalid UUID.[/red]")
        await auth.close()
        return

    console.print(f"\n[bold]Loading item[/bold] [cyan]{item_uuid}[/cyan] …")
    try:
        item = await client.get_item(item_uuid)
    except DSpaceAPIError as e:
        if e.status_code == 404:
            console.print("[red]Item not found (404).[/red]")
        else:
            console.print(f"[red]Failed to load item:[/red] {e}")
        await auth.close()
        return

    title = _extract_dc_title(item)
    handle = item.get("handle") or "(none)"
    name = item.get("name") or ""

    console.print("[bold]dc.title (confirmation):[/bold]")
    if title is not None:
        console.print(f"  [green]{title}[/green]")
    else:
        console.print("  [yellow](no dc.title metadata)[/yellow]")
    console.print(f"[dim]name:[/dim] {name or '(empty)'}")
    console.print(f"[dim]handle:[/dim] {handle}")

    phrase, desc = _confirmation_phrase(item)
    console.print(f"\n[yellow]To proceed, type {desc}.[/yellow]")
    typed = console.input("[bold]Confirmation:[/bold] ")
    if typed != phrase:
        console.print("[red]Confirmation does not match. Aborted.[/red]")
        await auth.close()
        return

    console.print("\n[bold]Inventory (stored for post-delete checks)[/bold]")
    bundles, bitstreams = await collect_inventory(client, item_uuid)
    console.print(
        f"\n[dim]Summary:[/dim] {len(bundles)} bundle(s), {len(bitstreams)} bitstream(s).\n"
    )

    console.print("[bold red]Deleting item…[/bold red]")
    try:
        await client.delete_item(item_uuid)
        console.print("[green]DELETE item returned success (204).[/green]")
    except DSpaceAPIError as e:
        console.print(f"[red]Delete failed:[/red] {e}")
        await auth.close()
        return

    console.print(
        f"\n[bold]Waiting {VERIFY_WAIT_SECONDS}s before verification[/bold] "
        "[dim](allows indexing / consistency)[/dim]"
    )
    for remaining in range(VERIFY_WAIT_SECONDS, 0, -1):
        console.print(f"  [dim]{remaining}s…[/dim]")
        await asyncio.sleep(1)

    all_gone = await verify_gone(client, item_uuid, bundles, bitstreams)
    if all_gone:
        console.print(
            "\n[bold green]Verification complete: stored objects are gone (404).[/bold green]"
        )
    else:
        console.print(
            "\n[bold red]Verification reported failures; inspect the server or retry.[/bold red]"
        )

    await auth.close()


if __name__ == "__main__":
    asyncio.run(main())
