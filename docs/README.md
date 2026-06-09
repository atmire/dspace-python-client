# DSpace Client Documentation

This directory contains documentation for the DSpace Python client.

## REST API Documentation

The `dspace-rest-api/` subdirectory contains automatically fetched DSpace REST API documentation from the [DSpace/RestContract](https://github.com/DSpace/RestContract) repository. This documentation is:

- **Automatically fetched** when you initialize a `DSpaceClient` with specific target versions
- **Git-based** for easy updates and version tracking
- **Version-specific** with separate directories for each DSpace version
- **Excluded from git** (see `.gitignore`) to avoid repository bloat

### Directory Structure

```
docs/
├── README.md                    # This file
├── API_GOTCHAS.md              # Critical DSpace quirks and gotchas
└── dspace-rest-api/            # Fetched docs (gitignored)
    ├── bleeding-edge/          # Latest development branch (main)
    ├── 7.6/                    # DSpace 7.6 REST contract docs (dspace-7_x)
    ├── 8.0/                    # DSpace 8.0 REST contract docs
    ├── 9.0/                    # DSpace 9.0 REST contract docs (dspace-9_x)
    └── 10.0/                   # DSpace 10.0 REST contract docs (dspace-10_x)
```

### Usage

The documentation is automatically managed by the client:

```python
from dspace_client import DSpaceClient

# This will automatically fetch docs for bleeding-edge if not cached
client = DSpaceClient(
    base_url="https://demo.dspace.org",
    jwt_token=jwt,
    csrf_token=csrf,
    http_client=client,
    target_versions="bleeding-edge"  # or ["7.6", "8.0", "9.0", "10.0"]
)
```

### Manual Documentation Management

You can also manage documentation manually using the CLI:

**Note:** The `dspace-docs` command is available after installing the package. If using a virtual environment, activate it first (`source .venv/bin/activate` on macOS/Linux, `.venv\Scripts\activate` on Windows). Run from the **project root** so docs land under `docs/dspace-rest-api/{version}/`.

```bash
# First-time (or missing cache): clone RestContract for a version
dspace-docs fetch 9.0

# List supported versions and local cache status
dspace-docs list

# Refresh all versions that are already cached
dspace-docs update

# Show git status for all versions
dspace-docs status
```

### API_GOTCHAS.md

This file contains critical information about DSpace REST API quirks and gotchas that every developer should know. It covers:

- CSRF token handling
- Cookie management
- Authentication flow
- Common pitfalls
- Debugging tips

**Read this file before using the client!**

## Links

- [DSpace REST Contract Repository](https://github.com/DSpace/RestContract)
- [DSpace Documentation](https://wiki.duraspace.org/display/DSPACE/DSpace+Documentation)
- [DSpace REST API Guide](https://wiki.duraspace.org/display/DSPACE/REST+API)
