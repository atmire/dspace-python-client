"""Discovery pagination, DOI extraction, ORIGINAL-bundle PDF check."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

from dspace_client import DSpaceClient


def first_metadata_value(metadata: dict, key: str) -> str:
    vals = metadata.get(key) or []
    if not isinstance(vals, list) or not vals:
        return ""
    first = vals[0]
    if isinstance(first, dict):
        return str(first.get("value") or "").strip()
    return str(first).strip()


DEFAULT_DOI_FIELD = "dc.identifier.doi"


def _doi_from_value(value: str) -> str:
    """Normalize a raw metadata value to a bare DOI (handles doi.org URLs)."""
    if "doi.org/" in value.lower():
        m = re.search(r"doi\.org/(.+)", value, re.IGNORECASE)
        if m:
            return normalize_doi_string(m.group(1).strip())
    return normalize_doi_string(value)


def extract_doi_from_metadata(metadata: dict, doi_field: str = DEFAULT_DOI_FIELD) -> str:
    """
    Return a single DOI string from ``doi_field``, or empty if none.

    Falls back to a ``dc.identifier.uri`` value that points at doi.org when the
    chosen field is empty.
    """
    v = first_metadata_value(metadata, doi_field)
    if v:
        return _doi_from_value(v)
    uri = first_metadata_value(metadata, "dc.identifier.uri")
    if uri and "doi.org" in uri.lower():
        return _doi_from_value(uri)
    return ""


def normalize_doi_string(s: str) -> str:
    """Strip common prefixes; keep bare DOI for APIs."""
    t = s.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if t.lower().startswith(prefix.lower()):
            t = t[len(prefix) :].strip()
    return t


async def item_has_pdf_in_original(
    client: DSpaceClient,
    item_uuid: str,
    pdf_format_id: int,
) -> bool:
    """True if ORIGINAL bundle has at least one bitstream with PDF format id."""
    bundles_data = await client.get_item_bundles(item_uuid)
    bundles = bundles_data.get("bundles", [])
    if not bundles:
        bundles = bundles_data.get("_embedded", {}).get("bundles", [])
    for bundle in bundles:
        name = (bundle.get("name") or "").upper()
        if name != "ORIGINAL":
            continue
        bundle_uuid = bundle.get("uuid")
        if not bundle_uuid:
            continue
        try:
            bitstreams_data = await client.get_bundle_bitstreams(bundle_uuid, embed_format=True)
        except Exception:
            try:
                bitstreams_data = await client.get_bundle_bitstreams(
                    bundle_uuid, embed_format=False
                )
            except Exception:
                continue
        bitstreams = bitstreams_data.get("_embedded", {}).get("bitstreams", [])
        if not bitstreams:
            bitstreams = bitstreams_data.get("bitstreams", [])
        for bs in bitstreams:
            fmt = bs.get("_embedded", {}).get("format") or bs.get("format")
            if fmt is not None and fmt.get("id") == pdf_format_id:
                return True
        for bs in bitstreams:
            bs_uuid = bs.get("uuid")
            if not bs_uuid:
                continue
            try:
                fmt = await client.get_bitstream_format(bs_uuid)
                if fmt.get("id") == pdf_format_id:
                    return True
            except Exception:
                pass
    return False


def _objects_from_search(data: dict) -> list:
    """Embedded discovery objects list."""
    sr = data.get("_embedded", {}).get("searchResult", {})
    return sr.get("_embedded", {}).get("objects", [])


async def iter_discovery_item_uuids(
    client: DSpaceClient,
    *,
    query: str,
    sort: str = "dc.date.accessioned,desc",
    page_size: int = 100,
) -> AsyncIterator[str]:
    """Yield item UUIDs from discovery, newest first."""
    page = 0
    page_size = min(page_size, 100)
    while True:
        results = await client.search_items(
            query=query,
            sort=sort,
            page=page,
            size=page_size,
        )
        objects = _objects_from_search(results)
        if not objects:
            break
        for obj in objects:
            indexable = obj.get("_embedded", {}).get("indexableObject", {})
            u = indexable.get("uuid")
            if u:
                yield u
        if len(objects) < page_size:
            break
        page += 1


async def find_eligible_items(
    client: DSpaceClient,
    pdf_format_id: int,
    *,
    query: str,
    max_items: int | None,
    single: bool,
    doi_field: str = DEFAULT_DOI_FIELD,
) -> AsyncIterator[tuple[str, str, dict]]:
    """
    Yield (uuid, doi, full_item_dict) for items that have a DOI and no PDF in ORIGINAL.

    If single is True, stop after the first eligible item.
    max_items caps how many eligible items are yielded in bulk mode.
    doi_field selects which metadata field is read for the DOI.
    """
    eligible = 0
    async for item_uuid in iter_discovery_item_uuids(client, query=query):
        try:
            full = await client.get_item(item_uuid)
        except Exception:
            continue
        metadata = full.get("metadata") or {}
        doi = extract_doi_from_metadata(metadata, doi_field)
        if not doi:
            continue
        has_pdf = await item_has_pdf_in_original(client, item_uuid, pdf_format_id)
        if has_pdf:
            continue
        eligible += 1
        yield item_uuid, doi, full
        if single:
            break
        if max_items is not None and eligible >= max_items:
            break


__all__ = [
    "DEFAULT_DOI_FIELD",
    "extract_doi_from_metadata",
    "find_eligible_items",
    "first_metadata_value",
    "item_has_pdf_in_original",
    "iter_discovery_item_uuids",
    "normalize_doi_string",
]
