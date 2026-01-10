"""Recent items with submitters report."""

import asyncio
import getpass
import csv
import io
import sys
from datetime import datetime
from typing import List, Dict, Optional
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from dspace_client import DSpaceAuthClient, DSpaceClient

# DEVELOPER DECLARES: This script is compatible with DSpace 9.0 only
# (Submitter information endpoint only exists in DSpace 9+)
# Users can only run this script against DSpace servers running version 9.0 or later
TARGET_VERSIONS = ["9.0"]

console = Console()




def get_metadata_value(metadata: dict, key: str) -> str:
    """Extract metadata value, joining multiple values with ||."""
    values = metadata.get(key, [])
    if not values:
        return ""
    return " || ".join(v.get("value", "") for v in values)


async def get_item_submitter_with_cache(
    item_uuid: str,
    client: DSpaceClient,
    submitter_cache: dict,
) -> str:
    """
    Get submitter email with intelligent caching.
    
    Note: This works for DSpace 9+ using the embed parameter or direct submitter endpoint.
    For DSpace 7, submitter information is not available via the items API.
    
    Args:
        item_uuid: UUID of the item
        client: DSpace client instance
        submitter_cache: Dict to cache submitter_uuid -> submitter_email mappings
    
    Returns:
        Submitter email address
    """
    # Try to get the item with embedded submitter information (DSpace 9+)
    console.print(f"[dim]Fetching item with submitter for {item_uuid}...[/dim]")
    try:
        # Use the embed parameter to get submitter information in one call
        response = await client.client.get(
            f"{client.base_url}/server/api/core/items/{item_uuid}?embed=submitter",
            headers={"Authorization": f"Bearer {client.jwt_token}"}
        )
        
        if response.status_code == 200:
            item_data = response.json()
            submitter = item_data.get("_embedded", {}).get("submitter")
            
            if submitter:
                submitter_uuid = submitter.get("uuid", "")
                submitter_email = submitter.get("email", "")
                
                # Check if we've seen this submitter before
                if submitter_uuid in submitter_cache:
                    console.print(f"[green]✓ Known submitter: {submitter_email}[/green]")
                else:
                    console.print(f"[yellow]🔍 New submitter found: {submitter_email}[/yellow]")
                    submitter_cache[submitter_uuid] = submitter_email
                
                return submitter_email
            else:
                # Try using the dedicated submitter endpoint as fallback (DSpace 9+)
                submitter_data = await client.get_item_submitter(item_uuid)
                if submitter_data:
                    submitter_uuid = submitter_data.get("uuid", "")
                    submitter_email = submitter_data.get("email", "")
                    
                    if submitter_uuid in submitter_cache:
                        console.print(f"[green]✓ Known submitter: {submitter_email}[/green]")
                    else:
                        console.print(f"[yellow]🔍 New submitter found: {submitter_email}[/yellow]")
                        submitter_cache[submitter_uuid] = submitter_email
                    
                    return submitter_email
                
                console.print(f"[dim]⚠️  No submitter found for {item_uuid}[/dim]")
                return ""
        else:
            console.print(f"[dim]⚠️  Failed to get item {item_uuid}: {response.status_code}[/dim]")
            return ""
            
    except Exception as e:
        console.print(f"[dim]⚠️  Error getting submitter for {item_uuid}: {e}[/dim]")
        return ""


def generate_csv_data(items_data: List[Dict]) -> str:
    """Generate CSV string from items data."""
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow(["id", "title", "date issued", "date available", "submitter"])
    
    # Rows
    for item in items_data:
        writer.writerow([
            item["uuid"],
            item["title"],
            item["date_issued"],
            item["date_accessioned"],
            item["submitter_email"],
        ])
    
    return output.getvalue()


async def main():
    """Generate recent items report with submitter information."""
    
    # Print script information
    console.print("\n[bold cyan]Recent Items Report with Submitter Information[/bold cyan]")
    console.print("[dim]━" * 50 + "[/dim]")
    console.print("[green]✓ This script is READ-ONLY - no changes will be made to your DSpace repository[/green]")
    console.print("[green]   - Searches for recently added items[/green]")
    console.print("[green]   - Retrieves submitter information for each item[/green]")
    console.print("[green]   - Exports data to CSV format[/green]")
    console.print("")
    console.print("[bold]Required Access:[/bold] Admin access is recommended (some items may require admin to view submitter info)")
    console.print("[bold]Supported Versions:[/bold] " + ", ".join(TARGET_VERSIONS))
    console.print("[bold]Note:[/bold] Submitter information endpoint only exists in DSpace 9.0+")
    console.print("[dim]━" * 50 + "[/dim]\n")
    
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
    
    # Prompt for start month
    current_month = datetime.now().strftime("%Y-%m")
    start_month = console.input(
        f"[bold cyan]Search from month (YYYY-MM)[/bold cyan] [dim](press Enter for {current_month}):[/dim] "
    ).strip()
    
    if not start_month:
        start_month = current_month
        console.print(f"[dim]→ Using current month: {current_month}[/dim]")
    
    # Validate date format
    try:
        datetime.strptime(start_month, "%Y-%m")
    except ValueError:
        console.print("[red]Error: Invalid date format. Please use YYYY-MM (e.g., 2025-01)[/red]")
        return
    
    # Authenticate
    auth = DSpaceAuthClient(base_url)
    jwt, status = await auth.authenticate(username, password)
    
    # Ask for throttle delay
    throttle_input = console.input(
        "[bold cyan]Throttle delay between API calls (in seconds)[/bold cyan] [dim](press Enter for 1.0, 0 for no throttle):[/dim] "
    ).strip()
    
    # Parse and validate the throttle value
    if not throttle_input:
        # Default: 1 second
        courtesy_delay = 1.0
        console.print("[dim]→ Using default throttle: 1.0 second between API calls[/dim]")
    else:
        try:
            courtesy_delay = float(throttle_input)
            
            # Validate the input
            if courtesy_delay < 0:
                console.print("[yellow]⚠️  Invalid value (negative). Using default: 1.0 second[/yellow]")
                courtesy_delay = 1.0
            elif courtesy_delay == 0:
                console.print("[yellow]⚠️  No throttle - maximum speed mode enabled[/yellow]")
            elif courtesy_delay < 0.1:
                console.print(f"[yellow]⚠️  Very aggressive throttle: {courtesy_delay}s - use with caution[/yellow]")
            else:
                console.print(f"[dim]→ Using throttle: {courtesy_delay} second(s) between API calls[/dim]")
        except ValueError:
            console.print(f"[yellow]⚠️  Invalid input '{throttle_input}'. Using default: 1.0 second[/yellow]")
            courtesy_delay = 1.0
    
    
    # Ask for maximum number of items to retrieve
    max_items_input = console.input(
        "[bold cyan]Maximum items to retrieve[/bold cyan] [dim](press Enter for 1000):[/dim] "
    ).strip()
    
    try:
        max_items = int(max_items_input) if max_items_input else 1000
        if max_items <= 0:
            max_items = 1000
            console.print("[yellow]Invalid number, using default: 1000[/yellow]")
    except ValueError:
        max_items = 1000
        console.print("[yellow]Invalid number, using default: 1000[/yellow]")
    
    console.print(f"[dim]→ Will retrieve up to {max_items} items[/dim]")
    
    # Create client with developer-declared target versions
    client = DSpaceClient(
        base_url=base_url,
        jwt_token=jwt,
        csrf_token=auth.csrf_token,
        http_client=auth.client,
        target_versions=TARGET_VERSIONS,  # Uses developer-declared versions
        courtesy_delay=courtesy_delay,
    )
    
    # Verify server version matches developer-declared versions
    from dspace_client import ServerVersionMismatchError
    try:
        await client.verify_server_version(raise_on_mismatch=True)
    except ServerVersionMismatchError as e:
        console.print(f"[red]Version mismatch:[/red] {e}")
        console.print(f"[yellow]This script only works with DSpace versions: {supported_str}[/yellow]")
        await auth.close()
        return
    
    # Detect DSpace version to check if submitter information is available
    console.print("[dim]Detecting DSpace version...[/dim]")
    detected_version = await client.detect_dspace_version()
    
    if detected_version and detected_version.startswith("7."):
        console.print("[red]✗[/red] DSpace 7 detected - submitter information is not available via the items API")
        console.print("[yellow]This script requires DSpace 9+ to retrieve submitter information for archived items.[/yellow]")
        console.print("[dim]DSpace 7 does not expose submitter data through the items endpoint.[/dim]")
        await auth.close()
        return
    elif detected_version:
        console.print(f"[green]✓[/green] DSpace {detected_version} detected - submitter information available")
    else:
        console.print("[yellow]⚠[/yellow] Could not detect DSpace version - continuing anyway")
    
    # Parse start month to datetime object
    start_month_dt = datetime.strptime(start_month, "%Y-%m")
    console.print(f"[dim]Searching items from {start_month} onwards[/dim]")
    
    # Test basic search first
    console.print("[dim]Testing basic search endpoint...[/dim]")
    try:
        results = await client.search_items(
            query="*",  # Search for everything
            sort="dc.date.accessioned,desc",
            page=0,
            size=10,
        )
        console.print("[green]✓[/green] Basic search works!")
        console.print(f"Found {len(results.get('_embedded', {}).get('searchResult', {}).get('_embedded', {}).get('objects', []))} items")
    except Exception as e:
        console.print(f"[red]Basic search failed: {e}[/red]")
        await auth.close()
        return
    
    # Search for items using open-ended range query (works with DSpace Solr)
    console.print("[dim]Searching for items with date filtering...[/dim]")
    try:
        # Use open-ended range query that works with DSpace Solr
        # Format: dc.date.accessioned:[YYYY-MM-01 TO *]
        start_date_str = start_month_dt.strftime("%Y-%m-01")
        date_query = f"dc.date.accessioned:[{start_date_str} TO *]"
        
        console.print(f"[dim]Using query: {date_query}[/dim]")
        
        # Search with pagination to respect max_items limit
        all_items = []
        page = 0
        page_size = min(100, max_items)  # Don't fetch more than needed
        
        while len(all_items) < max_items:
            results = await client.search_items(
                query=date_query,
                sort="dc.date.accessioned,desc",
                page=page,
                size=page_size,
            )
            items = results["_embedded"]["searchResult"]["_embedded"]["objects"]
            
            if not items:
                break  # No more items available
            
            # Add items up to the limit
            remaining_slots = max_items - len(all_items)
            items_to_add = items[:remaining_slots]
            all_items.extend(items_to_add)
            
            # If we got fewer items than requested, we've reached the end
            if len(items) < page_size:
                break
                
            page += 1
        
        console.print(f"[green]✓[/green] Found {len(all_items)} items with date filtering")
        
        if len(all_items) >= max_items:
            console.print(f"[yellow]⚠️  Reached limit of {max_items} items[/yellow]")
            
    except Exception as e:
        console.print(f"[red]Search failed: {e}[/red]")
        await auth.close()
        return
    
    console.print(f"[green]✓[/green] Found {len(all_items)} items")
    
    if not all_items:
        console.print("[yellow]No items found in the specified date range.[/yellow]")
        await auth.close()
        return
    
    # Process items to get full details and submitter information
    items_data = []
    submitter_cache = {}  # Cache submitter_uuid -> submitter_email mappings
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console,
    ) as progress:
        
        process_task = progress.add_task("Processing items...", total=len(all_items))
        
        for item in all_items:
            try:
                # Get full item details
                item_uuid = item["_embedded"]["indexableObject"]["uuid"]
                full_item = await client.get_item(item_uuid)
                
                # Extract metadata
                metadata = full_item.get("metadata", {})
                title = get_metadata_value(metadata, "dc.title")
                date_issued = get_metadata_value(metadata, "dc.date.issued")
                date_accessioned = get_metadata_value(metadata, "dc.date.accessioned")
                
                # Get submitter information with caching
                submitter_email = await get_item_submitter_with_cache(
                    item_uuid,
                    client,
                    submitter_cache,
                )
                
                items_data.append({
                    "uuid": item_uuid,
                    "title": title,
                    "date_issued": date_issued,
                    "date_accessioned": date_accessioned,
                    "submitter_email": submitter_email,
                })
                
            except Exception as e:
                console.print(f"[red]Error processing item {item.get('_embedded', {}).get('indexableObject', {}).get('uuid', 'unknown')}: {e}[/red]")
            
            progress.update(process_task, advance=1)
    
    # Show cache statistics
    console.print("\n[bold cyan]Cache Statistics:[/bold cyan]")
    console.print(f"  Unique submitters found: {len(submitter_cache)}")
    console.print(f"  Total items processed: {len(items_data)}")
    
    # Calculate cache effectiveness
    unique_submitters = len(submitter_cache)
    total_items = len(items_data)
    if total_items > 0:
        cache_efficiency = 100 * (total_items - unique_submitters) / total_items
        console.print(f"  Cache efficiency: {total_items - unique_submitters}/{total_items} items ({cache_efficiency:.1f}%)")
    
    # Generate output
    csv_data = generate_csv_data(items_data)
    
    if len(items_data) <= 10:
        # Print to stdout for small datasets
        console.print("\n[bold]Recent Items Report:[/bold]")
        console.print(csv_data)
    else:
        # Offer to save to file for larger datasets
        console.print(f"\n[bold]Found {len(items_data)} items.[/bold]")
        save_choice = console.input(
            "[bold cyan]Save to file?[/bold cyan] [dim](y/n, press Enter for yes):[/dim] "
        ).strip().lower()
        
        if save_choice in ("", "y", "yes"):
            filename = f"recent_items_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            with open(filename, "w", encoding="utf-8") as f:
                f.write(csv_data)
            console.print(f"[green]✓[/green] Saved to {filename}")
        else:
            console.print("\n[bold]Recent Items Report:[/bold]")
            console.print(csv_data)
    
    await auth.close()


if __name__ == "__main__":
    asyncio.run(main())
