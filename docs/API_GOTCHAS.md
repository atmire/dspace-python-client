# DSpace API Gotchas and Compatibility Notes

This document tracks important differences and compatibility issues between DSpace versions.

## DSpace 10

`demo.dspace.org` runs DSpace 10 (10.1 at time of writing), so it is the default reference server for examples once they declare `10.0` in their `TARGET_VERSIONS`.

The DSpace 9 -> 10 REST contract change is **additive only** for everything these examples use. Comparing `docs/dspace-rest-api/9.0/` (the 9.0 snapshot) against `docs/dspace-rest-api/10.0/` (`dspace-10_x`):

- **New endpoints** (not used by the bundled examples): `auditevents`, `edititems`, `edititemmodes`, `securitysettings`.
- **New search methods** (additive): `findEditAuthorized` / `findAddAuthorized` on communities/collections/items, and `findByCustomURL` on items.
- **Cosmetic only**: formatting/typo fixes in `bitstreams.md`, `identifiers.md`, `collections.md`.
- **Unchanged**: authentication/CSRF, communities/collections/items CRUD, bundles, bitstreams, EPersons, groups, vocabularies, discovery search, the submitter endpoint, and statistics view events.

Because there were no breaking changes, declaring `10.0` is purely additive: a script keeps its existing `TARGET_VERSIONS` and simply gains `"10.0"`. Note that the connection gate rejects **major** version mismatches, so a script that only declares `["9.0"]` will be refused by a DSpace 10 server with `ServerVersionMismatchError` until `"10.0"` is added.

To refresh the contract docs: `dspace-docs fetch 10.0` (writes to `docs/dspace-rest-api/10.0/`).

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

The `target_versions` parameter in `DSpaceClient` restricts which DSpace servers you can connect to based on version compatibility rules.

**Version Compatibility Rules:**

1. **Exact version match** (e.g., target `9.0` → server `9.0`) → ✅ Allowed, no warning
2. **Minor version difference** (e.g., target `9.0` → server `9.1`, same major version) → ⚠️ Warning but allowed
3. **Major version mismatch** (e.g., target `8.0` → server `7.6`, different major version) → ❌ Connection rejected with `ServerVersionMismatchError`

4. **Multiple target versions** - When you specify multiple versions (e.g., `["8.0", "9.0"]`), the server must match **at least one** of them (or be a minor variant of one).

5. **Operation validation** - Additionally, the client validates that all operations you call are supported in your target version(s). If an operation doesn't exist in all target versions, the client raises a `VersionIncompatibilityError` before making the request.

**Developer Workflow:**

As a developer, you declare which DSpace versions your script supports when writing it. At runtime, the user provides the URL, and the system validates the server version against your declared compatibility.

**Recommended Usage - Use Helper Function:**

```python
# DEVELOPER DECLARES: This script is compatible with DSpace 8.0 and 9.0
TARGET_VERSIONS = ["8.0", "9.0"]

from dspace_client import create_validated_client, ServerVersionMismatchError

# ... later, at runtime, user provides URL ...
base_url = input("DSpace base URL: ")
username = input("Username: ")
password = input("Password: ")

try:
    # Automatically authenticates, creates client, and validates server version
    # Server version is checked against TARGET_VERSIONS
    auth, client = await create_validated_client(
        base_url=base_url,  # User provides at runtime
        username=username,
        password=password,
        target_versions=TARGET_VERSIONS  # Developer-declared versions
    )
    # Version validation happened automatically
    await client.create_community("My Community")
except ServerVersionMismatchError as e:
    print(f"Cannot connect: {e}")
    print(f"This script only works with DSpace versions: {', '.join(TARGET_VERSIONS)}")
```

**Manual Usage:**

If you create the client manually, call `verify_server_version()` after initialization:

```python
# DEVELOPER DECLARES: This script is compatible with DSpace 8.0 and 9.0
TARGET_VERSIONS = ["8.0", "9.0"]

from dspace_client import DSpaceAuthClient, DSpaceClient, ServerVersionMismatchError

auth = DSpaceAuthClient(base_url)  # User provides base_url at runtime
jwt, status = await auth.authenticate(username, password)

client = DSpaceClient(
    base_url=base_url,
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

**Real-world scenario:**
```python
# DEVELOPER DECLARES: This application works with DSpace 8.0 and 9.0
TARGET_VERSIONS = ["8.0", "9.0"]

# ... user provides URL at runtime ...
base_url = "https://production-dspace.org"

auth, client = await create_validated_client(
    base_url=base_url,
    username="admin",
    password="pass",
    target_versions=TARGET_VERSIONS  # Server must match one of these
)

# Validation results:
# If server is 7.6 → ServerVersionMismatchError is raised (major mismatch)
# If server is 8.0 → ✅ Allowed (exact match)
# If server is 8.1 → ⚠️ Warning but allowed (minor version difference, same major)
# If server is 9.0 → ✅ Allowed (exact match)
# If server is 9.1 → ⚠️ Warning but allowed (minor version difference, same major)

# Operations are also validated
await client.create_community("My Community")  # Works - exists in 8.0 and 9.0

# This will raise VersionIncompatibilityError BEFORE making the request
# because get_item_submitter only exists in 9.0+, not in 8.0
try:
    await client.get_item_submitter(item_uuid)
except VersionIncompatibilityError as e:
    print(f"Operation not supported in all target versions: {e}")
```

### Automatic Version Detection

The client detects the server version by trying, in order:

1. **GET /api/config/properties/dspace.version** (single-property endpoint; the main **GET /api/config/properties** is not implemented and returns 405 per the REST contract).
2. **GET /server/api** (root HAL document), parsing a version field if present.
3. **GET /actuator/info** (admin-only on some setups), parsing version from the response.

```python
# Version detection happens automatically in verify_server_version()
# But you can also call it manually:
detected_version = await client.detect_dspace_version()

if detected_version:
    print(f"Detected DSpace version: {detected_version}")
    if detected_version.startswith("7."):
        print("DSpace 7 - some features may not be available")
    elif detected_version.startswith("9."):
        print("DSpace 9 - full feature support")
```

If all strategies fail (e.g. property not whitelisted, root/actuator not exposing version), a warning is shown but the connection proceeds.

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

**Long-running jobs and session refresh:** `DSpaceAuthClient.ensure_session()` extends sessions by calling `POST /api/authn/login` with the existing `Authorization: Bearer` and `X-XSRF-TOKEN` when possible, so routine refresh does not hit `GET /api/security/csrf` (which some reverse proxies mishandle). If that refresh fails, the client falls back to a full CSRF + password login.

**Auth diagnostics (failure-only):** The `dspace_client.auth` logger emits **WARNING** (and **DEBUG** for extra detail) only when CSRF fetch, JWT refresh, login, or verification fails—successful auth is silent. To capture verbose traces for support, enable DEBUG for that logger only, for example:

```python
import logging
logging.getLogger("dspace_client.auth").setLevel(logging.DEBUG)
```

### Adding several metadata values in one PATCH

`{"op": "add", "path": "/metadata/<field>", "value": [{...}, {...}]}` on an item that has no
value in that field yet stores **only the first value** of the list. The response is still
200, so nothing signals the loss. Append each value with its own operation instead:

```json
[{"op": "add", "path": "/metadata/dc.subject/-", "value": {"value": "one"}},
 {"op": "add", "path": "/metadata/dc.subject/-", "value": {"value": "two"}}]
```

`/-` works whether or not the field already has values. Observed on DSpace 11.0-SNAPSHOT
(2026-10); not yet checked on 9 and 10.

### Writing metadata on in-progress items

A site administrator can `PATCH /api/core/items/{uuid}` on the item of a workspace item
(an in-progress submission), not only on archived items. Observed on DSpace 11.0-SNAPSHOT
(2026-10); not yet checked on 9 and 10.

## References

- [DSpace 7 RestContract](https://github.com/DSpace/RestContract/tree/dspace-7_x)
- [DSpace 9 RestContract](https://github.com/DSpace/RestContract/tree/main)
- Local documentation: `docs/dspace-rest-api/7.6/` and `docs/dspace-rest-api/9.0/`
