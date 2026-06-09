# Quick Start Guide

Get up and running with the DSpace Python client in 5 minutes.

## Installation

> [!WARNING]
> This client is **not yet published to PyPI**. The name `dspace-client` on PyPI currently belongs to an unrelated project, so `pip install dspace-client` will install the wrong package. Install from source instead.

**Prerequisites:** Python 3.11+, Git (also used at runtime to fetch the DSpace REST API docs, so it must be on your system PATH), and pip 21.3 or newer (required by `pip install -e .`).

Get the code either by cloning:

```bash
git clone https://git.atmire.com/scripts/dspace-python-client.git
cd dspace-python-client
```

or by unpacking a zip archive of the project and `cd`-ing into the unpacked folder. Then, from inside the project folder, create a virtual environment and install (see the [Python packaging guide](https://packaging.python.org/en/latest/guides/installing-using-pip-and-virtual-environments/#create-and-use-virtual-environments) for details):

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
which python   # should end in .venv/bin/python
python3 -m pip install --upgrade pip
pip install -e .
```

**Windows**

```bat
py -m venv .venv
.venv\Scripts\activate
where python   # should end in .venv\Scripts\python
py -m pip install --upgrade pip
pip install -e .
```

If `pip install -e .` fails with a confusing error, the most common cause is an old pip; re-run `pip install --upgrade pip` inside the activated venv and try again.

## Basic Usage

### 1. Declare Version Compatibility (Developer)

As a developer, declare which DSpace versions your script supports at the top of your script:

```python
# DEVELOPER DECLARES: This script is compatible with DSpace 8.0 and 9.0
TARGET_VERSIONS = ["8.0", "9.0"]
```

### 2. Authenticate and Create Client with Version Validation (Runtime)

At runtime, collect the URL from the user and validate against declared versions:

```python
import asyncio
from dspace_client import create_validated_client, ServerVersionMismatchError

async def main():
    # User provides URL at runtime
    base_url = input("DSpace base URL: ")
    username = input("Username: ")
    password = input("Password: ")
    
    try:
        # Authenticate and create client with automatic version validation
        # Server version will be checked against TARGET_VERSIONS
        auth, client = await create_validated_client(
            base_url=base_url,
            username=username,
            password=password,
            target_versions=TARGET_VERSIONS  # Developer-declared versions
        )
        # Version validation happens automatically
        # If major version mismatch, ServerVersionMismatchError is raised
    except ServerVersionMismatchError as e:
        print(f"Cannot connect: {e}")
        print(f"This script only works with DSpace versions: {', '.join(TARGET_VERSIONS)}")
        return
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
from dspace_client import create_validated_client, ServerVersionMismatchError

# DEVELOPER DECLARES: This script is compatible with DSpace 8.0 and 9.0
TARGET_VERSIONS = ["8.0", "9.0"]

async def main():
    # User provides URL at runtime
    base_url = input("DSpace base URL: ")
    username = input("Username: ")
    password = input("Password: ")
    
    try:
        # Authenticate and create client with automatic version validation
        auth, client = await create_validated_client(
            base_url=base_url,
            username=username,
            password=password,
            target_versions=TARGET_VERSIONS  # Developer-declared versions
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
    except ServerVersionMismatchError as e:
        print(f"Cannot connect: {e}")
        print(f"This script only works with DSpace versions: {', '.join(TARGET_VERSIONS)}")

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

**Important:** The `target_versions` parameter restricts which DSpace servers you can connect to:
- **Exact version match** (e.g., target `9.0` → server `9.0`) → ✅ Allowed
- **Minor version difference** (e.g., target `9.0` → server `9.1`, same major) → ⚠️ Warning but allowed
- **Major version mismatch** (e.g., target `8.0` → server `7.6`, different major) → ❌ Connection rejected

When you specify multiple versions (e.g., `["8.0", "9.0"]`), the server must match **at least one** of the target versions. Use `create_validated_client()` helper function for automatic version validation.

## What Happens on First Run

1. **Documentation Fetching**: Client automatically clones DSpace/RestContract repository
2. **Version Validation**: All API calls are validated against target versions
3. **Caching**: Documentation is cached locally for future use
4. **Updates**: Documentation is automatically updated if older than 24 hours

## Next Steps

- **Examples**: See `examples/` directory for more complex scenarios
- **Seed examples** (`examples/seed/`): dspace-seed–style **MiniSpace** and **MegaSpace** scenarios (install optional deps: `pip install -e ".[examples]"`). They declare **`TARGET_VERSIONS = ["9.0"]`** and run **`verify_server_version`** by default (use **`--skip-version-check`** to skip). **MegaSpace** requires **`--collections` ≥ 2** (fail-fast at startup). Details: `examples/seed/README.md`.
- **Atmire messaging**: When **`auth.close()`** runs, the library may show a short thank-you panel; **`DSPACE_CLIENT_DISABLE_ATMIRE_PROMO=1`** turns it off.
- **Batch Operations**: Use `BatchItemCreator` for high-performance bulk imports (optional **`on_metrics_sample`** on **`create_items_batch`** for timing samples)
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

**Note:** The `dspace-docs` command is available after installing the package. If using a virtual environment, activate it first (`source .venv/bin/activate` on macOS/Linux, `.venv\Scripts\activate` on Windows).

```bash
# First-time: download docs for a version (e.g. from project root)
dspace-docs fetch 9.0

# Update all already-cached versions
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
