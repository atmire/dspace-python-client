# DSpace API Gotchas and Compatibility Notes

This document tracks important differences and compatibility issues between DSpace versions.

## Submitter Information

### DSpace 9+

Submitter information for items can be retrieved in multiple ways:

1. **Direct endpoint**: `GET /api/core/items/<uuid>/submitter`
2. **Embed parameter**: `GET /api/core/items/<uuid>?embed=submitter`

Example:
```python
# Direct endpoint
submitter = await client.get_item_submitter(item_uuid)

# Or via embed
response = await client.client.get(
    f"{client.base_url}/server/api/core/items/{item_uuid}?embed=submitter",
    headers={"Authorization": f"Bearer {client.jwt_token}"}
)
item_data = response.json()
submitter = item_data.get("_embedded", {}).get("submitter")
```

### DSpace 7

**The submitter endpoint does NOT exist in DSpace 7.**

The items API in DSpace 7 does not expose submitter information through:
- The `/submitter` endpoint (doesn't exist)
- The `?embed=submitter` parameter (submitter is not an exposed link)

**Workarounds for DSpace 7:**

1. Search workspaceitems with the submitter filter:
   ```
   GET /api/submission/workspaceitems/search/findBySubmitter?uuid=<submitter-uuid>
   ```

2. Access submission metadata during workflow stages (before items are archived)

3. Store submitter information in item metadata during submission

**Note:** The `get_item_submitter()` method in `dspace_client.core.DSpaceClient` will return `None` for DSpace 7 instances. This is expected behavior.

## Version Detection and Compatibility

### Understanding `target_versions`

The `target_versions` parameter in `DSpaceClient` specifies which DSpace versions you want to ensure your code is compatible with. **It does NOT restrict which DSpace server you can connect to.**

**Important clarifications:**

1. **You can connect to any DSpace server** - The client will work against any DSpace instance, regardless of the `target_versions` you specify.

2. **Validation is about code compatibility** - The `target_versions` parameter tells the client to validate that all operations you call are supported in the specified version(s). This helps you write code that works across multiple DSpace versions.

3. **Multiple versions = strictest validation** - When you specify multiple versions, the client ensures that every operation you call works in **ALL** of those versions. If an operation doesn't exist in one of the target versions, the client will raise a `VersionIncompatibilityError` before making the request.

4. **Pre-execution validation** - Validation happens **before** each API call, preventing runtime failures from version incompatibilities.

**Examples:**

```python
# Compatible with DSpace 9 only - will validate operations against 9.0
# But you can still connect to any DSpace server (7.x, 8.x, 9.x, etc.)
client = DSpaceClient(base_url, jwt, csrf_token, http_client, target_versions="9.0")

# Compatible with DSpace 7.x - will validate operations against 7.6
client = DSpaceClient(base_url, jwt, csrf_token, http_client, target_versions="7.6")

# Compatible with multiple versions - operations must work in ALL three versions
# This is the strictest validation mode
client = DSpaceClient(base_url, jwt, csrf_token, http_client, target_versions=["7.6", "8.0", "9.0"])
```

**Real-world scenario:**
```python
# You're building an application that needs to work with DSpace 7.6, 8.0, and 9.0
client = DSpaceClient(
    base_url="https://production-dspace.org",  # This could be any version
    jwt_token=jwt,
    csrf_token=csrf,
    http_client=http_client,
    target_versions=["7.6", "8.0", "9.0"]  # Ensure code works in all these versions
)

# This will work - create_community exists in all three versions
await client.create_community("My Community")

# This will raise VersionIncompatibilityError BEFORE making the request
# because get_item_submitter only exists in 9.0+, not in 7.6 or 8.0
try:
    await client.get_item_submitter(item_uuid)
except VersionIncompatibilityError as e:
    print(f"Operation not supported in all target versions: {e}")
    # You would need to handle this differently for DSpace 7.6/8.0
```

### Automatic Version Detection

You can also detect the DSpace version automatically at runtime:

```python
# Detect DSpace version by testing API capabilities
detected_version = await client.detect_dspace_version()

if detected_version:
    print(f"Detected DSpace version: {detected_version}")
    if detected_version.startswith("7."):
        print("DSpace 7 - some features may not be available")
    elif detected_version.startswith("9."):
        print("DSpace 9 - full feature support")
```

This is useful for scripts that need to conditionally enable features based on the DSpace version.

## Other Known Differences

### Embed Parameter Support

The `?embed=` parameter is supported in both DSpace 7 and 9, but the available embeds differ:

- **DSpace 7**: bundles, owningCollection, mappedCollections, templateItemOf, relationships, thumbnail
- **DSpace 9**: Same as DSpace 7, plus submitter

### Projections

Both DSpace 7 and 9 support projections:
- `?projection=full` - includes all linked subresources
- Default projection - excludes all subresource embeds

### Authentication

Authentication works identically across DSpace 7, 8, and 9:
- JWT tokens via `/api/authn/login`
- CSRF tokens via `DSPACE-XSRF-COOKIE` cookie
- Bearer token authentication for API calls

## References

- [DSpace 7 RestContract](https://github.com/DSpace/RestContract/tree/dspace-7_x)
- [DSpace 9 RestContract](https://github.com/DSpace/RestContract/tree/main)
- Local documentation: `docs/dspace-rest-api/7.6/` and `docs/dspace-rest-api/9.0/`
