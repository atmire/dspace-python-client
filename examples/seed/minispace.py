"""
MiniSpace-style example: one community, collection, item, and PDF bitstream (dspace-seed).

Uses ``dspace_client`` only for the API. Themed titles/metadata come from
``examples/seed/seedpacks/default.yml`` via ``seed_data.py``.

Install deps: ``pip install -e ".[examples]"`` (PyYAML).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from dspace_client import AuthenticationError, ServerVersionMismatchError, show_script_attribution
from dspace_client.exceptions import DSpaceAPIError

# DEVELOPER DECLARES: compatible with DSpace 9.0 and 10.0
TARGET_VERSIONS = ["9.0", "10.0"]
SCRIPT_AUTHORS = "Bram Luyten (Atmire)"

_SEED_DIR = Path(__file__).resolve().parent
if str(_SEED_DIR) not in sys.path:
    sys.path.insert(0, str(_SEED_DIR))

from seed_client import connect_seed_client
from seed_data import DEFAULT_SEED_HTTP_TIMEOUT, DataFactory, load_seed_pack

console = Console()
DEFAULT_SEEDPACK = _SEED_DIR / "seedpacks" / "default.yml"


async def _ensure_session(
    auth,
    client,
    username: str,
) -> bool:
    """Re-authenticate if needed and sync tokens on the client."""
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


async def run_minispace(
    seed_pack_path: Path,
    seed: int,
    base_url: str,
    username: str,
    password: str,
    strict_versions: bool,
) -> bool:
    """Run the mini scenario."""
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

    try:
        auth, client = await connect_seed_client(
            base_url=base_url,
            username=username,
            password=password,
            target_versions=TARGET_VERSIONS,
            strict_versions=strict_versions,
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

    try:
        factory = DataFactory(seed_pack, seed=seed)
        discipline = factory.get_first_discipline()
        community_title = factory.get_discipline_title(discipline)
        collection_title = factory.get_collection_title(discipline, 0)
        item_title = factory.get_item_title(discipline, 0)

        table = Table(title="MiniSpace preview", show_header=True, header_style="bold cyan")
        table.add_column("Type", style="yellow")
        table.add_column("Name", style="white")
        table.add_row("Community", community_title)
        table.add_row("Collection", collection_title)
        table.add_row("Item", item_title)
        table.add_row("Bitstream", "sample.pdf (open access)")
        console.print()
        console.print(table)
        console.print()

        confirm = (
            console.input("[bold yellow]Proceed with creation? (yes/no):[/bold yellow] ")
            .strip()
            .lower()
        )
        if confirm not in ("yes", "y"):
            console.print("[yellow]Cancelled.[/yellow]")
            return True

        console.print("\n[bold cyan]Creating content…[/bold cyan]\n")

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
                        "value": f"A top-level community for {discipline.name} research.",
                        "language": "en",
                        "authority": None,
                        "confidence": -1,
                    }
                ],
            },
        )
        community_uuid = community["uuid"]
        console.print(f"  [green]✓[/green] Community: {community_uuid}")

        collection = await client.create_collection(
            name=collection_title,
            parent_community_uuid=community_uuid,
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
        collection_uuid = collection["uuid"]
        console.print(f"  [green]✓[/green] Collection: {collection_uuid}")

        item_metadata = factory.get_item_metadata(item_title, discipline, subfield_index=0)
        item = await client.create_item(
            name=item_title,
            owning_collection_uuid=collection_uuid,
            metadata=item_metadata,
        )
        item_uuid = item["uuid"]
        console.print(f"  [green]✓[/green] Item: {item_uuid}")

        bundle = await client.create_bundle(item_uuid, "ORIGINAL")
        pdf_content = factory.generate_sample_pdf_content(item_title)
        bitstream = await client.upload_bitstream(
            bundle_uuid=bundle["uuid"],
            filename="sample.pdf",
            content=pdf_content,
            metadata={
                "dc.title": [
                    {"value": "sample.pdf", "language": None, "authority": None, "confidence": -1}
                ],
                "dc.description": [
                    {
                        "value": "Sample PDF for demonstration",
                        "language": "en",
                        "authority": None,
                        "confidence": -1,
                    }
                ],
            },
        )
        console.print(f"  [green]✓[/green] Bitstream: {bitstream['uuid']}")

        console.print("\n[bold green]✓ MiniSpace content created.[/bold green]\n")

        summary = f"""[bold cyan]Created[/bold cyan]

[yellow]Community[/yellow]  {community_uuid}
  {base_url.rstrip("/")}/communities/{community_uuid}

[yellow]Collection[/yellow] {collection_uuid}
  {base_url.rstrip("/")}/collections/{collection_uuid}

[yellow]Item[/yellow]       {item_uuid}
  {base_url.rstrip("/")}/items/{item_uuid}

[yellow]Bitstream[/yellow]  {bitstream["uuid"]}
"""
        console.print(Panel(summary, title="Summary", border_style="green"))

        cleanup = (
            console.input(
                "\n[bold yellow]Delete the community (cascades to children)? (yes/no):[/bold yellow] "
            )
            .strip()
            .lower()
        )
        if cleanup not in ("yes", "y"):
            console.print("[cyan]Cleanup skipped.[/cyan]")
            return True

        if not await _ensure_session(auth, client, username):
            return False

        try:
            await client.delete_community(community_uuid)
            console.print("[green]✓ Community deleted (including children).[/green]")
        except DSpaceAPIError as e:
            console.print(f"[red]Cleanup failed: {e}[/red]")
            return False

        return True

    finally:
        await auth.close()


async def main_async(args: argparse.Namespace) -> None:
    show_script_attribution(SCRIPT_AUTHORS, console=console)
    seed_pack_path = Path(args.seedpack).resolve()
    supported = ", ".join(TARGET_VERSIONS)
    console.print(
        Panel.fit(
            "[bold cyan]MiniSpace[/bold cyan]\n"
            "1 Community → 1 Collection → 1 Item → 1 Bitstream\n"
            f"Target versions: {supported}",
            border_style="cyan",
        )
    )

    proceed = console.input("\n[bold yellow]Continue? (yes/no):[/bold yellow] ").strip().lower()
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

    ok = await run_minispace(
        seed_pack_path=seed_pack_path,
        seed=args.seed,
        base_url=base_url,
        username=username,
        password=password,
        strict_versions=not args.skip_version_check,
    )
    if not ok:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="MiniSpace seed example for dspace-python-client.")
    parser.add_argument(
        "--seedpack",
        type=Path,
        default=DEFAULT_SEEDPACK,
        help=f"Path to seed pack YAML (default: {DEFAULT_SEEDPACK})",
    )
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for factory (default: 42)")
    parser.add_argument(
        "--skip-version-check",
        action="store_true",
        help="Skip verify_server_version after login (faster; default is to probe and enforce 9.0).",
    )
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
