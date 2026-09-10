"""Basic DSpace client usage example.

For a MiniSpace-style walkthrough (Rich preview, seed-pack titles, optional cleanup), see
``examples/seed/minispace.py`` (requires ``pip install -e ".[examples]"`` for PyYAML).
"""

import asyncio
import getpass

from rich.console import Console

from dspace_client import ServerVersionMismatchError, managed_client, show_script_attribution

# DEVELOPER DECLARES: This script is compatible with DSpace 8.0, 9.0 and 10.0
# Users can only run this script against DSpace servers running these versions
TARGET_VERSIONS = ["8.0", "9.0", "10.0"]
SCRIPT_AUTHORS = "Bram Luyten (Atmire)"

console = Console()


async def main():
    """Demonstrate basic DSpace client usage."""
    show_script_attribution(SCRIPT_AUTHORS, console=console)

    # Print script information
    console.print("\n[bold cyan]Basic DSpace Client Usage Example[/bold cyan]")
    console.print("[dim]━" * 50 + "[/dim]")
    console.print(
        "[yellow]⚠️  WARNING: This script WILL CREATE new content in your DSpace repository[/yellow]"
    )
    console.print("[yellow]   - Creates a new community[/yellow]")
    console.print("[yellow]   - Creates a new collection[/yellow]")
    console.print("[yellow]   - Creates a new item with metadata[/yellow]")
    console.print("[yellow]   - Creates a bundle and uploads a bitstream[/yellow]")
    console.print("")
    console.print(
        "[bold]Required Access:[/bold] Admin access is required to create communities, collections, and items"
    )
    console.print("[bold]Supported Versions:[/bold] " + ", ".join(TARGET_VERSIONS))
    console.print("[dim]━" * 50 + "[/dim]\n")

    # Ask for confirmation before proceeding
    proceed = (
        console.input("[bold yellow]Do you want to continue? (yes/no):[/bold yellow] ")
        .strip()
        .lower()
    )
    if proceed not in ("yes", "y"):
        console.print("[dim]Script cancelled by user.[/dim]")
        return

    # Show what versions this script supports
    supported_str = ", ".join(TARGET_VERSIONS)

    # Prompt user for DSpace server URL
    base_url = console.input(
        f"[bold cyan]DSpace base URL[/bold cyan] [dim](this script supports versions: {supported_str}, press Enter for https://demo.dspace.org):[/dim] "
    ).strip()

    # Default to demo.dspace.org if user just pressed Enter
    if not base_url:
        base_url = "https://demo.dspace.org"
        console.print("[dim]→ Using default: https://demo.dspace.org[/dim]")

    # Auto-detect demo.dspace.org credentials
    base_url_normalized = base_url.rstrip("/").lower()
    is_demo = "demo.dspace.org" in base_url_normalized

    if is_demo:
        console.print("[dim]ℹ️  Using demo credentials: dspacedemo+admin@gmail.com[/dim]")
        username = "dspacedemo+admin@gmail.com"
        password = "dspace"
    else:
        username = console.input("[bold cyan]Admin username:[/bold cyan] ").strip()
        password = getpass.getpass("Admin password: ")

    # Authenticate and create client with automatic version validation
    # The server version will be checked against TARGET_VERSIONS declared above
    try:
        async with managed_client(
            base_url=base_url,
            username=username,
            password=password,
            target_versions=TARGET_VERSIONS,
            show_atmire_promo=True,
        ) as (_auth, client):
            community = await client.create_community("My Community")
            print(f"Created: {community['uuid']}")

            collection = await client.create_collection(
                name="My Collection", parent_community_uuid=community["uuid"]
            )
            print(f"Created collection: {collection['uuid']}")

            item = await client.create_item(
                name="My Item",
                owning_collection_uuid=collection["uuid"],
                metadata={
                    "dc.title": [
                        {"value": "My Item", "language": None, "authority": None, "confidence": -1}
                    ],
                    "dc.description": [
                        {
                            "value": "A sample item",
                            "language": None,
                            "authority": None,
                            "confidence": -1,
                        }
                    ],
                },
            )
            print(f"Created item: {item['uuid']}")

            bundle = await client.create_bundle(item["uuid"], "ORIGINAL")
            print(f"Created bundle: {bundle['uuid']}")

            sample_content = b"This is sample content for the bitstream."
            bitstream = await client.upload_bitstream(
                bundle_uuid=bundle["uuid"],
                filename="sample.txt",
                content=sample_content,
                metadata={
                    "dc.title": [
                        {
                            "value": "Sample File",
                            "language": None,
                            "authority": None,
                            "confidence": -1,
                        }
                    ]
                },
            )
            print(f"Uploaded bitstream: {bitstream['uuid']}")
    except ServerVersionMismatchError as e:
        console.print(f"[red]Version mismatch:[/red] {e}")
        console.print(
            f"[yellow]This script only works with DSpace versions: {supported_str}[/yellow]"
        )
        return


if __name__ == "__main__":
    asyncio.run(main())
