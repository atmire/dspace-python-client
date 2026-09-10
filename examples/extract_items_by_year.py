"""Extract all items from a specific year with pagination support."""

import asyncio
import csv
import getpass
import io
from datetime import datetime

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

from dspace_client import DSpaceAuthClient, DSpaceClient, show_script_attribution

TARGET_VERSIONS = ["7.0", "8.0", "9.0", "10.0"]
SCRIPT_AUTHORS = "Bram Luyten (Atmire)"

console = Console()


def get_metadata_value(metadata: dict, key: str) -> str:
    """Extract metadata value, joining multiple values with ||."""
    values = metadata.get(key, [])
    if not values:
        return ""
    return " || ".join(v.get("value", "") for v in values)


def generate_csv_data(items_data: list[dict]) -> str:
    """Generate CSV string from items data."""
    output = io.StringIO()
    writer = csv.writer(output)

    # Header
    writer.writerow(["dc.title", "dc.date.issued", "dc.identifier.pure", "dc.identifier.uri"])

    # Rows
    for item in items_data:
        writer.writerow([
            item["title"],
            item["date_issued"],
            item["identifier_pure"],
            item["identifier_uri"],
        ])

    return output.getvalue()


async def main():
    """Extract all items from a specific year."""
    show_script_attribution(SCRIPT_AUTHORS, console=console)

    # Interactive prompt for base URL
    base_url = console.input(
        "[bold cyan]DSpace base URL[/bold cyan] [dim](press Enter for https://demo.dspace.org):[/dim] "
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

    # Prompt for year
    current_year = datetime.now().strftime("%Y")
    year_input = console.input(
        f"[bold cyan]Year to extract (YYYY)[/bold cyan] [dim](press Enter for {current_year}):[/dim] "
    ).strip()

    if not year_input:
        year_input = current_year
        console.print(f"[dim]→ Using current year: {current_year}[/dim]")

    # Validate year format
    try:
        year = int(year_input)
        if year < 1000 or year > 9999:
            raise ValueError("Year must be 4 digits")
    except ValueError:
        console.print("[red]Error: Invalid year format. Please use YYYY (e.g., 2023)[/red]")
        return

    # Authenticate
    auth = DSpaceAuthClient(base_url)
    auth.show_atmire_promo = True
    jwt, _status = await auth.authenticate(username, password)

    # Ask for throttle delay
    throttle_input = console.input(
        "[bold cyan]Throttle delay between API calls (in seconds)[/bold cyan] [dim](press Enter for 1.0, 0 for no throttle):[/dim] "
    ).strip()

    # Parse and validate the throttle value
    if not throttle_input:
        courtesy_delay = 1.0
        console.print("[dim]→ Using default throttle: 1.0 second between API calls[/dim]")
    else:
        try:
            courtesy_delay = float(throttle_input)

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

    # Create client with version specification
    client = DSpaceClient(
        base_url=base_url,
        jwt_token=jwt,
        csrf_token=auth.csrf_token,
        http_client=auth.client,
        target_versions=TARGET_VERSIONS,
        courtesy_delay=courtesy_delay,
    )

    console.print(f"[dim]Searching for items with dc.date.issued in year {year}...[/dim]")

    # Search for items using wildcard query to match both:
    # - Full dates: "2026-01-15", "2026-12-31"
    # - Year-only values: "2026"
    # Format: dc.date.issued:YYYY*
    # This will match any date string starting with the year
    date_query = f"dc.date.issued:{year}*"

    console.print(f"[dim]Using query: {date_query}[/dim]")

    # Search with pagination to retrieve all items
    all_items = []
    page = 0
    page_size = 100  # Start with maximum allowed

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console,
    ) as progress:

        search_task = progress.add_task("Searching items...", total=None)

        while True:
            try:
                # Try to get metadata directly in search results by using embed parameter
                # Make direct request to discovery endpoint with embed=metadata if supported
                url = f"{client.base_url}/server/api/discover/search/objects"
                params = {
                    "dsoType": "item",
                    "sort": "dc.date.issued,desc",
                    "page": page,
                    "size": min(page_size, 100),
                    "query": date_query,
                }

                # Try to embed metadata in search results (may not be supported by all DSpace versions)
                # If this fails, we'll fall back to extracting from indexableObject
                headers = client._get_headers(include_csrf=False)
                response = await client.client.get(url, headers=headers, params=params)

                if response.status_code >= 400:
                    # If embed fails, use standard search_items method
                    results = await client.search_items(
                        query=date_query,
                        sort="dc.date.issued,desc",
                        page=page,
                        size=page_size,
                    )
                else:
                    results = response.json()

                items = results.get("_embedded", {}).get("searchResult", {}).get("_embedded", {}).get("objects", [])

                if not items:
                    break  # No more items available

                all_items.extend(items)
                progress.update(search_task, description=f"Found {len(all_items)} items so far...")

                # If we got fewer items than requested, we've reached the end
                if len(items) < page_size:
                    break

                page += 1

            except Exception as e:
                # If error and page_size > 10, try reducing it
                if page_size > 10:
                    page_size = max(10, page_size // 2)
                    console.print(f"[yellow]⚠️  Error occurred, reducing page size to {page_size}: {e}[/yellow]")
                    # Don't increment page, retry with smaller size
                    continue
                console.print(f"[red]Error searching items: {e}[/red]")
                await auth.close()
                return

        progress.update(search_task, completed=True, description=f"Found {len(all_items)} items")

    console.print(f"[green]✓[/green] Found {len(all_items)} items")

    if not all_items:
        console.print(f"[yellow]No items found for year {year}.[/yellow]")
        await auth.close()
        return

    # Process items to extract metadata
    # Note: DSpace search results typically don't include full metadata in indexableObject
    # We need to fetch items individually, but we'll try to extract what we can from search results first
    items_data = []
    items_needing_fetch = []  # Track items that need full fetch

    # First pass: try to extract from search results
    for item in all_items:
        indexable_object = item.get("_embedded", {}).get("indexableObject", {})
        item_uuid = indexable_object.get("uuid", "")

        # Check if metadata is available in search results
        metadata = indexable_object.get("metadata", {})
        title = get_metadata_value(metadata, "dc.title")
        date_issued = get_metadata_value(metadata, "dc.date.issued")
        identifier_pure = get_metadata_value(metadata, "dc.identifier.pure")
        identifier_uri = get_metadata_value(metadata, "dc.identifier.uri")

        # Also check if 'name' field exists (sometimes used as title in search results)
        if not title:
            title = indexable_object.get("name", "")

        # Check if we have all required fields from search results
        # We need at least title and date_issued, but identifier fields are optional
        has_required_fields = title and date_issued

        if has_required_fields:
            # We have the required fields, use what we have (even if identifiers are empty)
            items_data.append({
                "title": title,
                "date_issued": date_issued,
                "identifier_pure": identifier_pure,
                "identifier_uri": identifier_uri,
            })
        else:
            # Need to fetch full item details
            items_needing_fetch.append((item_uuid, len(items_data)))
            # Add placeholder that we'll update later
            items_data.append({
                "title": title or "",
                "date_issued": date_issued or "",
                "identifier_pure": identifier_pure or "",
                "identifier_uri": identifier_uri or "",
            })

    # Second pass: fetch items that need full metadata (if any)
    if items_needing_fetch:
        console.print(f"[dim]Fetching full metadata for {len(items_needing_fetch)} items...[/dim]")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=console,
        ) as progress:

            fetch_task = progress.add_task("Fetching item metadata...", total=len(items_needing_fetch))

            for item_uuid, data_index in items_needing_fetch:
                try:
                    full_item = await client.get_item(item_uuid)
                    metadata = full_item.get("metadata", {})
                    title = get_metadata_value(metadata, "dc.title")
                    date_issued = get_metadata_value(metadata, "dc.date.issued")
                    identifier_pure = get_metadata_value(metadata, "dc.identifier.pure")
                    identifier_uri = get_metadata_value(metadata, "dc.identifier.uri")

                    # Update the placeholder
                    items_data[data_index] = {
                        "title": title,
                        "date_issued": date_issued,
                        "identifier_pure": identifier_pure,
                        "identifier_uri": identifier_uri,
                    }

                except Exception as e:
                    console.print(f"[red]Error fetching item {item_uuid}: {e}[/red]")

                progress.update(fetch_task, advance=1)

    # Generate output
    csv_data = generate_csv_data(items_data)

    # Save to file with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"items_{year}_{timestamp}.csv"

    with open(filename, "w", encoding="utf-8") as f:
        f.write(csv_data)

    console.print(f"[green]✓[/green] Saved {len(items_data)} items to {filename}")

    # For small datasets, also print to console
    if len(items_data) <= 20:
        console.print("\n[bold]Extracted Items:[/bold]")
        console.print(csv_data)

    await auth.close()


if __name__ == "__main__":
    asyncio.run(main())
