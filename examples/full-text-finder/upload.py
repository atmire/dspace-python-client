"""Upload a bitstream to an item's ORIGINAL bundle."""

from __future__ import annotations

from dspace_client import DSpaceClient
from dspace_client.exceptions import DSpaceAPIError


async def ensure_original_bundle_uuid(client: DSpaceClient, item_uuid: str) -> str:
    """Return UUID of ORIGINAL bundle, creating it if missing."""
    data = await client.get_item_bundles(item_uuid)
    bundles = data.get("bundles", [])
    if not bundles:
        bundles = data.get("_embedded", {}).get("bundles", [])
    for b in bundles:
        if (b.get("name") or "").upper() == "ORIGINAL":
            uid = b.get("uuid")
            if uid:
                return uid
    created = await client.create_bundle(item_uuid, "ORIGINAL")
    uid = created.get("uuid")
    if not uid:
        raise DSpaceAPIError("create_bundle did not return uuid", status_code=None)
    return uid


async def upload_pdf_bitstream(
    client: DSpaceClient,
    item_uuid: str,
    filename: str,
    content: bytes,
) -> dict:
    bundle_uuid = await ensure_original_bundle_uuid(client, item_uuid)
    return await client.upload_bitstream(
        bundle_uuid,
        filename,
        content,
        metadata={},
    )


__all__ = ["ensure_original_bundle_uuid", "upload_pdf_bitstream"]
