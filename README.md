# DSpace Python Client

A comprehensive Python client for the DSpace REST API with version-aware compatibility checking and automatic documentation management.

## Key Features

- **Version-first initialization** with automatic documentation fetching
- **Pre-execution validation** for all API operations
- **Multi-version compatibility** support (DSpace 7.x, 8.x, 9.x)
- **Git-based documentation** management with auto-updates
- **Rich console output** for beautiful user experience
- **Batch operations** with adaptive concurrency control
- **Comprehensive error handling** with actionable messages

## Installation

```bash
pip install dspace-client
```

## Quick Start

```python
import asyncio
from dspace_client import DSpaceAuthClient, DSpaceClient

async def main():
    # Authenticate
    auth = DSpaceAuthClient("https://demo.dspace.org")
    jwt, status = await auth.authenticate("user", "pass")
    
    # Create client with version specification
    client = DSpaceClient(
        base_url="https://demo.dspace.org",
        jwt_token=jwt,
        csrf_token=auth.csrf_token,
        http_client=auth.client,
        target_versions="bleeding-edge",  # or ["7.6", "8.0", "9.0"]
    )
    
    # Create a community (validated against target versions)
    community = await client.create_community("My Community")
    print(f"Created: {community['uuid']}")
    
    await auth.close()

if __name__ == "__main__":
    asyncio.run(main())
```

## Version-First Architecture

The client requires you to specify target DSpace version(s) at initialization:

```python
# Single version
client = DSpaceClient(..., target_versions="8.0")

# Multiple versions (strictest validation)
client = DSpaceClient(..., target_versions=["7.6", "8.0", "9.0"])

# Latest development (default)
client = DSpaceClient(..., target_versions="bleeding-edge")
```

### What `target_versions` Means

**Important:** The `target_versions` parameter does **NOT** restrict which DSpace server you can connect to. Instead, it specifies which DSpace versions you want to ensure your code is compatible with.

**Key points:**

1. **You can connect to any DSpace server** - The client will work against any DSpace instance, regardless of the `target_versions` you specify.

2. **Validation is about compatibility** - The `target_versions` parameter tells the client to validate that all operations you call are supported in the specified version(s). This helps you write code that works across multiple DSpace versions.

3. **Multiple versions = strictest validation** - When you specify multiple versions (e.g., `["7.6", "8.0", "9.0"]`), the client ensures that every operation you call works in **ALL** of those versions. This is useful when building applications that need to work across different DSpace installations.

4. **Pre-execution validation** - Before each API call, the client checks if the operation is compatible with your target versions. If not, it raises a `VersionIncompatibilityError` **before** making the request, preventing runtime failures.

**Example:**
```python
# This client will validate operations against DSpace 7.6, 8.0, and 9.0
# But you can still connect to any DSpace server (7.x, 8.x, 9.x, or even bleeding-edge)
client = DSpaceClient(
    base_url="https://any-dspace-server.org",  # Can be any DSpace instance
    jwt_token=jwt,
    csrf_token=csrf,
    http_client=http_client,
    target_versions=["7.6", "8.0", "9.0"]  # Ensures code works in all these versions
)

# This will work if create_community exists in 7.6, 8.0, AND 9.0
await client.create_community("My Community")

# This will raise VersionIncompatibilityError if get_item_submitter 
# doesn't exist in ALL three versions (it only exists in 9.0+)
await client.get_item_submitter(item_uuid)  # Will fail validation
```

This ensures:
- **Compatibility validation** before every API call
- **Automatic documentation fetching** for target versions
- **Clear error messages** for version incompatibilities
- **Cross-version compatibility** when targeting multiple versions

## Documentation Management

The client automatically manages DSpace REST API documentation:

- **Git-based storage** in `docs/dspace-rest-api/{version}/`
- **Automatic updates** if docs are older than 24 hours
- **Version-specific branches** from DSpace/RestContract repository
- **CLI tools** for manual management

**Note:** The `dspace-docs` command is available after installing the package. If using a virtual environment, activate it first (`source venv/bin/activate`).

```bash
# Update all documentation
dspace-docs update

# Update specific version
dspace-docs update 9.0

# List available versions
dspace-docs list
```

## Examples

See the `examples/` directory for comprehensive examples:

- **`basic_usage.py`** - Simple CRUD operations
- **`bulk_import.py`** - Batch operations with concurrency control
- **`advanced_auth.py`** - Session management and error handling

## API Reference

### Core Classes

- **`DSpaceAuthClient`** - Authentication and session management
- **`DSpaceClient`** - Main API client with version validation
- **`BatchItemCreator`** - High-performance bulk operations
- **`ConcurrencyController`** - Adaptive concurrency control

### Key Methods

```python
# Communities
await client.create_community(name, metadata=None, parent_uuid=None)
await client.delete_community(uuid)

# Collections
await client.create_collection(name, parent_community_uuid, metadata=None)
await client.delete_collection(uuid)

# Items
await client.create_item(name, owning_collection_uuid, metadata=None)
await client.delete_item(uuid)

# Bitstreams
await client.upload_bitstream(bundle_uuid, filename, content, metadata=None)
await client.delete_bitstream(uuid)

# EPeople
await client.create_eperson(email, first_name, last_name)
await client.add_eperson_to_group(group_uuid, eperson_uuid)

# Groups
await client.create_group(name, description=None)
await client.add_subgroup_to_group(parent_group_uuid, subgroup_uuid)
```

## Error Handling

The client provides comprehensive error handling:

```python
from dspace_client import (
    DSpaceClientError,
    AuthenticationError,
    DSpaceAPIError,
    VersionIncompatibilityError
)

try:
    await client.create_community("My Community")
except VersionIncompatibilityError as e:
    print(f"Operation not supported: {e}")
    print(f"Supported versions: {e.supported_versions}")
except DSpaceAPIError as e:
    print(f"API error: {e}")
except AuthenticationError as e:
    print(f"Authentication failed: {e}")
```

## Configuration

### Concurrency Control

```python
from dspace_client import ConcurrencyConfig

config = ConcurrencyConfig(
    initial=8,           # Starting concurrency
    max_concurrency=32,  # Maximum concurrent operations
    min_concurrency=2,   # Minimum concurrent operations
)

batch_creator = BatchItemCreator(client, config)
```

### Version Compatibility

```python
# Check compatibility report
report = client.validator.get_compatibility_report()
print(f"create_community supported in: {report['create_community']}")

# Check incompatible operations
incompatible = client.validator.get_incompatible_operations()
if incompatible:
    print(f"Incompatible operations: {incompatible}")
```

## Development

### Installation from Source

```bash
git clone https://github.com/yourusername/dspace-python-client.git
cd dspace-python-client

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
pip install -e .
```

### Running Examples

When running examples, make sure to use the venv's Python:

```bash
# Option 1: Activate venv first
source venv/bin/activate
python examples/recent_items_with_submitters.py

# Option 2: Use venv Python directly
./venv/bin/python examples/recent_items_with_submitters.py
```

### Running Tests

```bash
pytest tests/
```

### Code Quality

```bash
ruff check .
mypy dspace_client/
```

## License

GPL-3.0-or-later - See LICENSE file for details.

## Author

**Bram Luyten** - bram@atmire.com

## Links

- [DSpace REST Contract](https://github.com/DSpace/RestContract)
- [DSpace Documentation](https://wiki.duraspace.org/display/DSPACE/DSpace+Documentation)
- [DSpace REST API Guide](https://wiki.duraspace.org/display/DSPACE/REST+API)
