# DSpace Python Client

A comprehensive Python client for the DSpace REST API with version-aware compatibility checking and automatic documentation management.

## Table of Contents

- [Key Features](#key-features)
- [Important Safety Notice](#important-safety-notice)
- [Installation](#installation)
- [Running the Examples](#running-the-examples)
  - [General Tutorials](#general-tutorials)
  - [Seed Scenarios](#seed-scenarios)
  - [Reporting Scripts](#reporting-scripts)
- [Building with the Library](#building-with-the-library)
  - [Getting Started](#getting-started)
  - [Version-First Architecture](#version-first-architecture)
    - [What `target_versions` Means](#what-target_versions-means)
    - [Manual Version Validation](#manual-version-validation)
  - [Documentation Management](#documentation-management)
  - [API Reference](#api-reference)
    - [Core Classes](#core-classes)
    - [Key Methods](#key-methods)
  - [Recipes](#recipes)
    - [Counting items with PDF bitstreams (REST)](#counting-items-with-pdf-bitstreams-rest-includes-non-public-items)
    - [Counting items with PDF via OAI-PMH](#counting-items-with-pdf-via-oai-pmh-no-auth-cacheable)
  - [Configuration](#configuration)
    - [Concurrency Control](#concurrency-control)
    - [Version Compatibility](#version-compatibility)
  - [Error Handling](#error-handling)
- [Contributing](#contributing)
  - [Installation from Source](#installation-from-source)
  - [Running Examples from Source](#running-examples-from-source)
  - [Running Tests](#running-tests)
  - [Code Quality](#code-quality)
- [Atmire Promotional Messages (Optional)](#atmire-promotional-messages-optional)
- [License](#license)
- [Author](#author)
- [Links](#links)

> This README is organised in two parts. If you just want to **run the bundled examples** against a DSpace server, start with [Running the Examples](#running-the-examples) — many users never need to go further. If you want to **write your own scripts** with the library, continue to [Building with the Library](#building-with-the-library).

## Key Features

- **Version-first initialization** with automatic documentation fetching
- **Pre-execution validation** for all API operations
- **Multi-version compatibility** support (DSpace 7.6, 8.x, 9.x; 7.6 REST contract in `docs/dspace-rest-api/7.6/`)
- **Git-based documentation** management with auto-updates
- **Rich console output** for beautiful user experience
- **Batch operations** with adaptive concurrency control
- **Comprehensive error handling** with actionable messages

## Important Safety Notice

Please read this section before running anything — examples or your own scripts — against a real DSpace instance.

> [!WARNING]
> **Always run against a test or staging server first.**
> This client can create, modify, and delete communities, collections, items, bitstreams, EPeople, and groups — often in bulk. Batch operations and cleanup flags are irreversible at scale. Verify behaviour on a non-production instance (e.g. [demo.dspace.org](https://demo.dspace.org) or your own staging environment) before pointing any script at a live repository.

> [!WARNING]
> **If you use AI to generate or modify scripts, you must understand every line before running it.**
> Large language models readily produce plausible-looking code that deletes the wrong things, silently skips validation, or hits the API in ways that look fine in isolation but misbehave against real data. You are responsible for the effects of any code you run against a DSpace repository. Do not run AI-assisted code — even a small edit to an existing example — unless you can explain what each operation does, have read the DSpace REST contract for the endpoints it touches, and have tested it on a throwaway instance first.

These two rules apply equally to the bundled examples, your own scripts, and anything copied out of the [Recipes](#recipes) section.

## Installation

```bash
pip install dspace-client
```

For full installation details, including source checkouts and virtual environments, see [Contributing → Installation from Source](#installation-from-source).

## Running the Examples

The fastest way to get a feel for what the client can do is to run the scripts in the `examples/` directory against a test DSpace instance. Each example is self-contained and prints what it creates/reads/deletes.

**Install optional deps for seed scenarios** (PyYAML for `examples/seed/`):

```bash
pip install -e ".[examples]"
```

All examples follow the same pattern: they prompt for a base URL, username, and password at runtime, and declare which DSpace versions they support. They will refuse to run against a server that does not match.

> [!WARNING]
> Re-read the [Important Safety Notice](#important-safety-notice) before pointing any example at a live repository. The seed scenarios in particular create and optionally delete substantial amounts of content.

### General Tutorials

- **`basic_usage.py`** — Short generic CRUD: community, collection, item, bitstream
- **`bulk_import.py`** — Batch item creation with adaptive concurrency (`BatchItemCreator`)
- **`advanced_auth.py`** — Session management and error handling

### Seed Scenarios

Located under `examples/seed/`. Inspired by the **dspace-seed** workflow (bundled YAML, no `dspace_seed` package at runtime—only `dspace_client`):

- **`seed/minispace.py`** — One community → collection → item → bitstream; declares **DSpace 9.0**; **`verify_server_version`** after login **by default** (use **`--skip-version-check`** to skip); optional cascade delete.
- **`seed/megaspace.py`** — Larger scenario: groups, EPeople, collection READ groups, **mega-metadata** and **mega-bitstreams** stress items, adaptive **`BatchItemCreator`** batch import (the library supports an optional **`on_metrics_sample`** callback on **`create_items_batch`** for time-series metrics), view events, optional cleanup. Requires **at least `--collections 2`** (validated at startup). **MegaSpace** also supports courtesy pacing, slow-request reporting, and optional **JSON/Markdown** diagnostics export — see **`examples/seed/README.md`**.

The large file **`examples/seed/seedpacks/default.yml`** is copied from dspace-seed; sync it manually if the upstream pack changes.

### Reporting Scripts

Read-only scripts for inspecting a repository:

- **`examples/count_items_with_pdf_bitstream.py`** — Counts items with at least one PDF bitstream via the authenticated REST API (includes non-public items). Supports resumable caching.
- **`examples/count_items_with_pdf_bitstream_oai.py`** — Same count via unauthenticated OAI-PMH harvesting, suitable for very large or slow repositories. Cached in CSV so runs can resume.

The underlying library calls used by these scripts are documented in [Recipes](#recipes).

## Building with the Library

This section is for developers writing their own scripts against the library. If you only want to run the bundled examples, you can skip it.

### Getting Started

For a minimal, runnable walkthrough — declaring target versions, authenticating, and creating your first community/collection/item — see **[QUICKSTART.md](QUICKSTART.md)**. The rest of this section covers the architecture, API surface, and library conventions that QUICKSTART references.

### Version-First Architecture

The client requires you to specify target DSpace version(s) at initialization:

```python
# Single version
client = DSpaceClient(..., target_versions="8.0")

# Multiple versions (strictest validation)
client = DSpaceClient(..., target_versions=["7.6", "8.0", "9.0"])

# Latest development (default)
client = DSpaceClient(..., target_versions="bleeding-edge")
```

#### What `target_versions` Means

**Important:** The `target_versions` parameter restricts which DSpace servers you can connect to based on version compatibility rules.

**Key points:**

1. **Version restrictions** - When you specify `target_versions`, you declare which DSpace versions your code is compatible with. The client will validate the server version and:
   - **Allow** connections to servers with exact version matches (e.g., target `9.0` → server `9.0`)
   - **Warn but allow** connections to servers with minor version differences (e.g., target `9.0` → server `9.1`, same major version)
   - **Reject** connections to servers with major version mismatches (e.g., target `8.0` → server `7.6`, different major version)

2. **Multiple versions** - When you specify multiple versions (e.g., `["8.0", "9.0"]`), the server must match **at least one** of the target versions. This allows your code to work with multiple DSpace installations.

3. **Pre-execution validation** - Before each API call, the client also validates that the operation is supported in your target version(s). If not, it raises a `VersionIncompatibilityError` **before** making the request.

**Developer Workflow:**

As a developer, you declare which DSpace versions your script supports when you write it:

```python
# DEVELOPER DECLARES: This script is compatible with DSpace 8.0 and 9.0
TARGET_VERSIONS = ["8.0", "9.0"]

# ... later in your script, when user provides URL ...

from dspace_client import create_validated_client, ServerVersionMismatchError

try:
    # Authenticates, creates client, and validates server version automatically
    auth, client = await create_validated_client(
        base_url=base_url,  # User provides this at runtime
        username=username,
        password=password,
        target_versions=TARGET_VERSIONS  # Developer-declared versions
    )
    # Version validation happens automatically - server version is checked against TARGET_VERSIONS
    # If major mismatch, ServerVersionMismatchError is raised
    await client.create_community("My Community")
except ServerVersionMismatchError as e:
    print(f"Cannot connect: Server version doesn't match declared compatibility")
    print(f"This script only works with DSpace versions: {', '.join(TARGET_VERSIONS)}")
```

#### Manual Version Validation

If you create the client manually, call `verify_server_version()` after initialization:

```python
# DEVELOPER DECLARES: This script is compatible with DSpace 8.0 and 9.0
TARGET_VERSIONS = ["8.0", "9.0"]

from dspace_client import DSpaceAuthClient, DSpaceClient, ServerVersionMismatchError

auth = DSpaceAuthClient("https://demo.dspace.org")
jwt, status = await auth.authenticate("user", "pass")

client = DSpaceClient(
    base_url="https://demo.dspace.org",
    jwt_token=jwt,
    csrf_token=auth.csrf_token,
    http_client=auth.client,
    target_versions=TARGET_VERSIONS  # Developer-declared versions
)

# Validate server version (raises ServerVersionMismatchError on major mismatch)
try:
    await client.verify_server_version(raise_on_mismatch=True)
except ServerVersionMismatchError as e:
    print(f"Version mismatch: {e}")
    print(f"This script only works with DSpace versions: {', '.join(TARGET_VERSIONS)}")
    # Handle error...
```

This ensures:
- **Server version validation** prevents connecting to incompatible servers
- **Operation compatibility validation** before every API call
- **Automatic documentation fetching** for target versions
- **Clear error messages** for version incompatibilities

### Documentation Management

The client automatically manages DSpace REST API documentation:

- **Git-based storage** in `docs/dspace-rest-api/{version}/`
- **Automatic updates** if docs are older than 24 hours
- **Version-specific branches** from DSpace/RestContract repository
- **CLI tools** for manual management

**Note:** The `dspace-docs` command is available after installing the package. If using a virtual environment, activate it first (`source venv/bin/activate`). Run these from the **project root** so documentation is stored under `docs/dspace-rest-api/{version}/` (paths are relative to the current working directory).

```bash
# First-time (or missing cache): clone the RestContract docs for a given API version
dspace-docs fetch 9.0
# Other examples: 7.6, 8.0, bleeding-edge — see `dspace-docs list` for supported labels

# List supported versions and whether each is already present locally
dspace-docs list

# Refresh every version that is already cached (does not fetch versions you never downloaded)
dspace-docs update

# Per-version git details and last update time
dspace-docs status
```

If a version was fetched recently, `dspace-docs fetch <version>` may reuse the cache until it is older than 24 hours (same logic as the client’s automatic fetch).

### API Reference

#### Core Classes

- **`DSpaceAuthClient`** - Authentication and session management
- **`DSpaceClient`** - Main API client with version validation
- **`BatchItemCreator`** - High-performance bulk operations; **`create_items_batch`** accepts an optional **`on_metrics_sample`** callback (invoked with **`PerformanceMetrics`** when progress is logged, every 50 items and at completion) for benchmarks and degradation analysis
- **`ConcurrencyController`** - Adaptive concurrency control

#### Key Methods

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
await client.get_item_bundles(item_uuid)
await client.get_bundle_bitstreams(bundle_uuid, embed_format=True)
await client.get_bitstream_format(bitstream_uuid)
await client.get_bitstream_formats(page=0, size=100)

# Reporting: count items with at least one PDF bitstream
result = await client.count_items_with_pdf_bitstream(
    pdf_format_id=3,  # optional; resolved from registry if omitted
    page_size=100,
    delay_between_pages=1.0,
)
# result["count"], result["total_items_processed"], result["pdf_format_id"]

# EPeople
await client.create_eperson(email, first_name, last_name)
await client.add_eperson_to_group(group_uuid, eperson_uuid)

# Groups
await client.create_group(name, description=None)
await client.add_subgroup_to_group(parent_group_uuid, subgroup_uuid)
```

### Recipes

#### Counting items with PDF bitstreams (REST, includes non-public items)

To report how many items have at least one bitstream in PDF format (equivalent to a DB count over items with PDF bitstreams), use the **REST API** with authentication so that **all items** (including non-public) are considered. The client pages through discovery (item UUIDs only), then for each item fetches only bundles and bitstreams (with format).

**Caching and resuming:** Use `RestPDFCountCache` so that already-known items are skipped on subsequent runs (items are assumed immutable). Use `force_rerun=True` to re-check everything.

**Slow-request logging:** To identify which endpoints are slow, set `slow_request_threshold_seconds` and `slow_request_callback` on the client; requests exceeding the threshold are also logged at WARNING and can be collected for analysis.

```python
from dspace_client import create_validated_client, RestPDFCountCache

auth, client = await create_validated_client(base_url=..., username=..., password=...)

cache = RestPDFCountCache(base_url=base_url)  # default dir: ~/.cache/dspace-rest-pdf
cache.load()

result = await client.count_items_with_pdf_bitstream(
    page_size=100,
    delay_between_pages=1.0,
    cache=cache,
    force_rerun=False,  # use cache; set True to re-check all
)
cache.save()
print(f"Items with ≥1 PDF: {result['count']} (of {result['total_items_processed']} processed)")
```

To log and inspect slow requests, pass `slow_request_threshold_seconds` and `slow_request_callback` into `create_validated_client(..., **client_kwargs)`; the example script does this and prints a table of slow requests at the end.

Example script: `examples/count_items_with_pdf_bitstream.py`. Set `DSPACE_REST_PDF_CACHE_DIR` to override the cache directory.

#### Counting items with PDF via OAI-PMH (no auth, cacheable)

For large or slow repositories, you can count items with PDF using the **OAI-PMH** endpoint at `{base_url}/server/oai/request`. No authentication is required. The client harvests `ListRecords` with `metadataPrefix=oai_dc` and infers PDF from `<dc:format>application/pdf</dc:format>`. Results can be stored in a **persistent CSV cache** so resumed runs skip already-seen items; incremental harvest (from `last_until`) is supported.

```python
from dspace_client.oai import OAIClient, OAIPDFCountCache, iterate_oai_dc_records

base_url = "https://your-dspace.edu"
cache = OAIPDFCountCache(base_url=base_url)  # default: ~/.cache/dspace-oai-pdf
cache.load()

async with OAIClient(base_url=base_url) as client:
    async for parsed in iterate_oai_dc_records(client, from_=cache.last_until):
        cache.update(parsed["identifier"], parsed["datestamp"], parsed["has_pdf"])
cache.save(last_until=max_datestamp)
total, with_pdf = cache.totals()
```

Example script: `examples/count_items_with_pdf_bitstream_oai.py`. Set `DSPACE_OAI_CACHE_DIR` to override the cache directory.

### Configuration

#### Concurrency Control

```python
from dspace_client import ConcurrencyConfig

config = ConcurrencyConfig(
    initial=8,           # Starting concurrency
    max_concurrency=32,  # Maximum concurrent operations
    min_concurrency=2,   # Minimum concurrent operations
)

batch_creator = BatchItemCreator(client, config)
```

#### Version Compatibility

```python
# Check compatibility report
report = client.validator.get_compatibility_report()
print(f"create_community supported in: {report['create_community']}")

# Check incompatible operations
incompatible = client.validator.get_incompatible_operations()
if incompatible:
    print(f"Incompatible operations: {incompatible}")
```

### Error Handling

The client provides comprehensive error handling:

```python
from dspace_client import (
    DSpaceClientError,
    AuthenticationError,
    DSpaceAPIError,
    VersionIncompatibilityError,
    ServerVersionMismatchError
)

try:
    auth, client = await create_validated_client(
        base_url="https://demo.dspace.org",
        username="user",
        password="pass",
        target_versions=["8.0", "9.0"]
    )
    await client.create_community("My Community")
except ServerVersionMismatchError as e:
    print(f"Server version mismatch: {e}")
    print(f"Server version: {e.server_version}")
    print(f"Target versions: {e.target_versions}")
except VersionIncompatibilityError as e:
    print(f"Operation not supported: {e}")
    print(f"Supported versions: {e.supported_versions}")
except DSpaceAPIError as e:
    print(f"API error: {e}")
except AuthenticationError as e:
    print(f"Authentication failed: {e}")
```

## Contributing

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

### Running Examples from Source

When running examples from a source checkout, make sure to use the venv's Python:

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

## Atmire Promotional Messages (Optional)

The client can show a **single non-blocking** **Rich** panel when you **[`await auth.close()`](dspace_client/auth.py)** on a session that had an open HTTP client: a thank-you line, a rotating **Did you know** fact, and **https://www.atmire.com/** (where the terminal supports Rich links). There is **no** session-start promotional UI and **no** browser prompt.

[`create_validated_client`](dspace_client/__init__.py) does not print Atmire messaging at connect time. The **[`examples/seed/seed_client.connect_seed_client`](examples/seed/seed_client.py)** helper still calls **`show_atmire_promo_start`** for API compatibility; that call is a no-op.

To **disable** all promotional output, set **`DSPACE_CLIENT_DISABLE_ATMIRE_PROMO=1`** (or `true` / `yes`).

You can call **`show_atmire_promo_end`** from **`dspace_client`** manually if you use a custom auth flow without integrated **`close()`** messaging.

## License

GPL-3.0-or-later - See LICENSE file for details.

## Author

**Bram Luyten** - bram@atmire.com

## Links

- [DSpace REST Contract](https://github.com/DSpace/RestContract)
- [DSpace Documentation](https://wiki.duraspace.org/display/DSPACE/DSpace+Documentation)
- [DSpace REST API Guide](https://wiki.duraspace.org/display/DSPACE/REST+API)
