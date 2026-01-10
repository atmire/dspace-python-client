"""Bulk import example with batch operations and concurrency control."""

import asyncio
import getpass
from rich.console import Console
from dspace_client import create_validated_client, BatchItemCreator, ConcurrencyConfig, ServerVersionMismatchError

# DEVELOPER DECLARES: This script is compatible with DSpace 7.6, 8.0, and 9.0
# Users can only run this script against DSpace servers running these versions
TARGET_VERSIONS = ["7.6", "8.0", "9.0"]

console = Console()


async def main():
    """Demonstrate bulk import with adaptive concurrency."""
    
    # Print script information
    console.print("\n[bold cyan]Bulk Import Example with Adaptive Concurrency[/bold cyan]")
    console.print("[dim]━" * 50 + "[/dim]")
    console.print("[yellow]⚠️  WARNING: This script WILL CREATE a large amount of content in your DSpace repository[/yellow]")
    console.print("[yellow]   - Creates a new community and collection[/yellow]")
    console.print("[yellow]   - Creates 100 items with metadata and bitstreams[/yellow]")
    console.print("[yellow]   - Uses adaptive concurrency control for optimal performance[/yellow]")
    console.print("")
    console.print("[bold]Required Access:[/bold] Admin access is required to create communities, collections, and items")
    console.print("[bold]Supported Versions:[/bold] " + ", ".join(TARGET_VERSIONS))
    console.print("[dim]━" * 50 + "[/dim]\n")
    
    # Ask for confirmation before proceeding
    proceed = console.input("[bold yellow]Do you want to continue? (yes/no):[/bold yellow] ").strip().lower()
    if proceed not in ("yes", "y"):
        console.print("[dim]Script cancelled by user.[/dim]")
        return
    
    # Show what versions this script supports
    supported_str = ", ".join(TARGET_VERSIONS)
    
    # Prompt user for DSpace server URL
    base_url = console.input(
        f"[bold cyan]DSpace base URL[/bold cyan] [dim](this script supports versions: {supported_str}, press Enter for https://demo.dspace.org):[/dim] "
    ).strip()
    
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
        auth, client = await create_validated_client(
            base_url=base_url,
            username=username,
            password=password,
            target_versions=TARGET_VERSIONS,  # Uses developer-declared versions
        )
        # Version validation happens automatically
    except ServerVersionMismatchError as e:
        console.print(f"[red]Version mismatch:[/red] {e}")
        console.print(f"[yellow]This script only works with DSpace versions: {supported_str}[/yellow]")
        return
    
    # Create a community and collection for bulk import
    community = await client.create_community("Bulk Import Community")
    collection = await client.create_collection(
        name="Bulk Import Collection",
        parent_community_uuid=community["uuid"]
    )
    
    print(f"Created collection: {collection['uuid']}")
    
    # Configure concurrency for optimal performance
    config = ConcurrencyConfig(
        initial=8,  # Start with 8 concurrent operations
        max_concurrency=32,  # Allow up to 32 concurrent operations
        min_concurrency=2,   # Never go below 2
    )
    
    # Create batch item creator
    batch_creator = BatchItemCreator(client, config)
    
    # Generate sample item data
    item_data = []
    for i in range(100):  # Create 100 items
        item_data.append({
            "title": f"Bulk Item {i+1}",
            "metadata": {
                "dc.title": [{"value": f"Bulk Item {i+1}", "language": None, "authority": None, "confidence": -1}],
                "dc.description": [{"value": f"Description for bulk item {i+1}", "language": None, "authority": None, "confidence": -1}],
                "dc.subject": [{"value": f"Subject {i+1}", "language": None, "authority": None, "confidence": -1}]
            },
            "content": f"Sample content for item {i+1}".encode(),
            "filename": f"item_{i+1}.txt"
        })
    
    # Create items in batches with adaptive concurrency
    print(f"Creating {len(item_data)} items with adaptive concurrency...")
    
    items, bundles, bitstreams = await batch_creator.create_items_batch(
        collection_uuids=[collection["uuid"]],
        item_data=item_data
    )
    
    # Show final metrics
    metrics = await batch_creator.get_final_metrics()
    counts = batch_creator.get_created_counts()
    
    print(f"\nBulk import complete!")
    print(f"Items created: {counts['items']}")
    print(f"Bundles created: {counts['bundles']}")
    print(f"Bitstreams created: {counts['bitstreams']}")
    print(f"Final concurrency: {metrics.current_concurrency}")
    print(f"Average throughput: {metrics.throughput:.2f} items/second")
    print(f"P95 latency: {metrics.p95_latency:.3f} seconds")
    
    await auth.close()


if __name__ == "__main__":
    asyncio.run(main())
