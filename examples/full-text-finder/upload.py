"""Upload a bitstream to an item's ORIGINAL bundle."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

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


def source_slug(source_label: str) -> str:
    """Reduce a source label (e.g. ``"Unpaywall: Univ"``) to a filename-safe slug."""
    base = (source_label or "").split(":", 1)[0]
    return re.sub(r"[^a-z0-9]+", "", base.lower())


def build_pdf_filename(source_label: str) -> str:
    """Descriptive bitstream filename, e.g. ``fulltext-unpaywall.pdf``."""
    slug = source_slug(source_label)
    return f"fulltext-{slug}.pdf" if slug else "fulltext.pdf"


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


@dataclass(frozen=True)
class ChecksumVerification:
    """Result of comparing DSpace's stored checksum against a locally computed one."""

    algorithm: str
    stored: str
    local: str
    comparable: bool
    matched: bool

    @property
    def checksum(self) -> str:
        """Checksum to record (DSpace's stored value when present, else the local one)."""
        return self.stored or self.local


def compute_local_checksum(content: bytes, algorithm: str | None) -> str:
    """
    Compute a hex digest of ``content`` using ``algorithm`` (DSpace naming, e.g. ``MD5``).

    Returns an empty string when the algorithm is not supported by ``hashlib``.
    """
    algo = (algorithm or "MD5").strip()
    normalized = algo.replace("-", "").replace("_", "").lower()
    try:
        hasher = hashlib.new(normalized)
    except (ValueError, TypeError):
        return ""
    hasher.update(content)
    return hasher.hexdigest()


def verify_bitstream_checksum(bitstream: dict, content: bytes) -> ChecksumVerification:
    """
    Compare the checksum DSpace stored against one computed locally over ``content``.

    DSpace computes and stores a checksum (default algorithm MD5) when a bitstream is
    created; it is returned in the upload response under ``checkSum`` and is what
    DSpace's checksum checker re-verifies. We always recompute the digest locally with
    the same algorithm so a mismatch (i.e. corruption between the bytes we sent and what
    DSpace stored) can be surfaced.

    ``comparable`` is True only when DSpace reported a checksum and the algorithm is
    supported locally; ``matched`` is True only when comparable and the values are equal.
    """
    check = bitstream.get("checkSum") or {}
    stored = (check.get("value") or "").strip()
    algorithm = (check.get("checkSumAlgorithm") or "").strip() or "MD5"
    local = compute_local_checksum(content, algorithm)
    comparable = bool(stored) and bool(local)
    matched = comparable and stored.lower() == local.lower()
    return ChecksumVerification(
        algorithm=algorithm,
        stored=stored,
        local=local,
        comparable=comparable,
        matched=matched,
    )


# DSpace filters metadata values without a language out of the item representation,
# so the provenance value MUST be written with a language code (e.g. "en") to be visible.
PROVENANCE_LANGUAGE = "en"


def build_provenance_statement(
    *,
    admin_email: str,
    source: str,
    url: str,
    inspected: bool,
    checksum: str | None = None,
    checksum_algorithm: str | None = None,
) -> str:
    """
    Build a statement to append to the item's ``dc.description.provenance``.

    Captures: timestamp (UTC), admin account email that added the file, source where
    the full text was found, whether the file was manually inspected, the original
    retrieval URL, and the file checksum (algorithm as reported by DSpace, default MD5).
    """
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "Full text retrieved by the DSpace Full Text Finder and added to the ORIGINAL bundle.",
        f"Timestamp: {timestamp}",
        f"Added by: {admin_email}",
        f"Source: {source}",
        f"Original URL: {url}",
        f"Manually inspected: {'Yes' if inspected else 'No'}",
    ]
    if checksum:
        algo = checksum_algorithm or "MD5"
        lines.append(f"Checksum ({algo}): {checksum}")
    return "\n".join(lines)


async def add_item_provenance(
    client: DSpaceClient,
    item_uuid: str,
    statement: str,
) -> dict:
    """
    Append a statement to the item's ``dc.description.provenance``.

    Uses a JSON Patch ``add`` with path ``/-`` so existing provenance values are
    preserved. The value is written with ``language: "en"`` because DSpace filters
    language-less values out of the item representation (they would otherwise appear
    to "vanish" even though the request returns HTTP 200).
    """
    return await client.patch_item(
        item_uuid,
        [
            {
                "op": "add",
                "path": "/metadata/dc.description.provenance/-",
                "value": {"value": statement, "language": PROVENANCE_LANGUAGE},
            }
        ],
    )


async def item_has_provenance_value(
    client: DSpaceClient,
    item_uuid: str,
    statement: str,
) -> bool:
    """Read the item back and confirm ``statement`` is present in dc.description.provenance."""
    item = await client.get_item(item_uuid)
    values = (item.get("metadata") or {}).get("dc.description.provenance", [])
    return any((v.get("value") or "") == statement for v in values)


__all__ = [
    "ChecksumVerification",
    "add_item_provenance",
    "build_pdf_filename",
    "build_provenance_statement",
    "compute_local_checksum",
    "ensure_original_bundle_uuid",
    "item_has_provenance_value",
    "source_slug",
    "upload_pdf_bitstream",
    "verify_bitstream_checksum",
]
