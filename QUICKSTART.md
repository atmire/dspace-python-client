# Quick Start Guide

Get up and running with the DSpace Python client in 5 minutes.

## Installation

```bash
pip install dspace-client
```

## Basic Usage

### 1. Authenticate

```python
import asyncio
from dspace_client import DSpaceAuthClient, DSpaceClient

async def main():
    # Create auth client
    auth = DSpaceAuthClient("https://demo.dspace.org")
    
    # Authenticate (this handles CSRF → Login → JWT flow)
    jwt, status = await auth.authenticate("your_username", "your_password")
    
    print(f"✅ Authenticated as: {status.get('eperson', {}).get('name', 'Unknown')}")
```

### 2. Create Client

```python
    # Create main client with version specification
    client = DSpaceClient(
        base_url="https://demo.dspace.org",
        jwt_token=jwt,
        csrf_token=auth.csrf_token,
        http_client=auth.client,
        target_versions="bleeding-edge",  # or "8.0", "9.0", ["7.6", "8.0"]
    )
    
    # On first run, this automatically fetches REST API docs
    # Subsequent runs use cached docs
```

### 3. Create Your First Community

```python
    # Create a community
    community = await client.create_community("My First Community")
    print(f"✅ Created community: {community['uuid']}")
    
    # Create a collection in the community
    collection = await client.create_collection(
        name="My First Collection",
        parent_community_uuid=community["uuid"]
    )
    print(f"✅ Created collection: {collection['uuid']}")
    
    # Create an item in the collection
    item = await client.create_item(
        name="My First Item",
        owning_collection_uuid=collection["uuid"]
    )
    print(f"✅ Created item: {item['uuid']}")
```

### 4. Clean Up

```python
    # Close the auth client
    await auth.close()
    
    print("🎉 You're done! Check your DSpace instance to see the created objects.")
```

## Complete Example

```python
import asyncio
from dspace_client import DSpaceAuthClient, DSpaceClient

async def main():
    # Authenticate
    auth = DSpaceAuthClient("https://demo.dspace.org")
    jwt, status = await auth.authenticate("your_username", "your_password")
    
    # Create client
    client = DSpaceClient(
        base_url="https://demo.dspace.org",
        jwt_token=jwt,
        csrf_token=auth.csrf_token,
        http_client=auth.client,
        target_versions="bleeding-edge",
    )
    
    # Create objects
    community = await client.create_community("My Community")
    collection = await client.create_collection(
        name="My Collection",
        parent_community_uuid=community["uuid"]
    )
    item = await client.create_item(
        name="My Item",
        owning_collection_uuid=collection["uuid"]
    )
    
    print(f"Created: {community['uuid']} → {collection['uuid']} → {item['uuid']}")
    
    await auth.close()

if __name__ == "__main__":
    asyncio.run(main())
```

## Version Options

Choose your target DSpace version(s):

```python
# Latest development (default)
target_versions="bleeding-edge"

# Specific stable version
target_versions="8.0"

# Multiple versions (strictest validation)
target_versions=["7.6", "8.0", "9.0"]
```

**Important:** The `target_versions` parameter does **NOT** restrict which DSpace server you can connect to. It specifies which DSpace versions you want to ensure your code is compatible with. The client will validate that all operations work in the specified version(s) before executing them, but you can still connect to any DSpace server regardless of its version.

When you specify multiple versions, operations must work in **ALL** of them. This helps you write code that works across different DSpace installations.

## What Happens on First Run

1. **Documentation Fetching**: Client automatically clones DSpace/RestContract repository
2. **Version Validation**: All API calls are validated against target versions
3. **Caching**: Documentation is cached locally for future use
4. **Updates**: Documentation is automatically updated if older than 24 hours

## Next Steps

- **Examples**: See `examples/` directory for more complex scenarios
- **Batch Operations**: Use `BatchItemCreator` for high-performance bulk imports
- **Error Handling**: Learn about comprehensive error handling
- **Documentation**: Read `docs/API_GOTCHAS.md` for critical DSpace quirks

## Troubleshooting

### Authentication Issues

```python
# Check if server is reachable
if not await auth.verify_server():
    print("❌ Server not reachable")

# Check session validity
if await auth.is_session_valid():
    print("✅ Session is valid")
```

### Version Compatibility

```python
# Check compatibility report
report = client.validator.get_compatibility_report()
print(f"Supported operations: {list(report.keys())}")

# Check for incompatible operations
incompatible = client.validator.get_incompatible_operations()
if incompatible:
    print(f"⚠️  Incompatible operations: {incompatible}")
```

### Documentation Management

**Note:** The `dspace-docs` command is available after installing the package. If using a virtual environment, activate it first (`source venv/bin/activate`).

```bash
# Update documentation manually
dspace-docs update

# Check documentation status
dspace-docs status

# List available versions
dspace-docs list
```

## Need Help?

- **Documentation**: See `docs/README.md`
- **Examples**: Check `examples/` directory
- **API Reference**: Read the main README.md
- **Issues**: Report on GitHub

Happy coding! 🚀
