"""Advanced authentication example with session management."""

import asyncio
import getpass
from rich.console import Console
from dspace_client import DSpaceAuthClient, DSpaceClient, ServerVersionMismatchError

# DEVELOPER DECLARES: This script is compatible with DSpace 8.0 and 9.0
# Users can only run this script against DSpace servers running these versions
TARGET_VERSIONS = ["8.0", "9.0"]

console = Console()


async def main():
    """Demonstrate advanced authentication and session management."""
    
    # Print script information
    console.print("\n[bold cyan]Advanced Authentication and Session Management Example[/bold cyan]")
    console.print("[dim]━" * 50 + "[/dim]")
    console.print("[yellow]⚠️  WARNING: This script WILL CREATE and then DELETE test content in your DSpace repository[/yellow]")
    console.print("[yellow]   - Creates a test community, collection, and item[/yellow]")
    console.print("[yellow]   - Demonstrates session validation and cleanup[/yellow]")
    console.print("[yellow]   - Automatically cleans up created test objects at the end[/yellow]")
    console.print("")
    console.print("[bold]Required Access:[/bold] Admin access is required to create and delete communities, collections, and items")
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
    
    # Create auth client
    auth = DSpaceAuthClient(base_url)
    
    # Check if server is reachable
    if not await auth.verify_server():
        print("❌ DSpace server is not reachable")
        return
    
    print("✅ DSpace server is reachable")
    
    # Authenticate
    try:
        jwt, status = await auth.authenticate(username, password)
        print(f"✅ Authentication successful")
        print(f"   JWT token: {jwt[:20]}...")
        print(f"   Authenticated: {status.get('authenticated', False)}")
        print(f"   User: {status.get('eperson', {}).get('name', 'Unknown')}")
    except Exception as e:
        print(f"❌ Authentication failed: {e}")
        return
    
    # Create client with developer-declared target versions
    client = DSpaceClient(
        base_url=base_url,
        jwt_token=jwt,
        csrf_token=auth.csrf_token,
        http_client=auth.client,
        target_versions=TARGET_VERSIONS,  # Uses developer-declared versions
    )
    
    # Verify server version matches developer-declared versions
    try:
        await client.verify_server_version(raise_on_mismatch=True)
    except ServerVersionMismatchError as e:
        console.print(f"[red]Version mismatch:[/red] {e}")
        console.print(f"[yellow]This script only works with DSpace versions: {supported_str}[/yellow]")
        return
    
    # Check if session is still valid
    if await auth.is_session_valid():
        print("✅ Session is valid")
    else:
        print("❌ Session is invalid")
        return
    
    # Perform some operations
    try:
        # Create a test community
        community = await client.create_community("Advanced Auth Test")
        print(f"✅ Created community: {community['uuid']}")
        
        # Create a test collection
        collection = await client.create_collection(
            name="Advanced Auth Collection",
            parent_community_uuid=community["uuid"]
        )
        print(f"✅ Created collection: {collection['uuid']}")
        
        # Create a test item
        item = await client.create_item(
            name="Advanced Auth Item",
            owning_collection_uuid=collection["uuid"],
            metadata={
                "dc.title": [{"value": "Advanced Auth Item", "language": None, "authority": None, "confidence": -1}],
                "dc.description": [{"value": "Created with advanced authentication", "language": None, "authority": None, "confidence": -1}]
            }
        )
        print(f"✅ Created item: {item['uuid']}")
        
        # Clean up
        await client.delete_item(item["uuid"])
        await client.delete_collection(collection["uuid"])
        await client.delete_community(community["uuid"])
        print("✅ Cleanup completed")
        
    except Exception as e:
        print(f"❌ Operation failed: {e}")
    
    # Check session validity again
    if await auth.is_session_valid():
        print("✅ Session is still valid after operations")
    else:
        print("❌ Session became invalid during operations")
    
    await auth.close()
    print("✅ Auth client closed")


if __name__ == "__main__":
    asyncio.run(main())
