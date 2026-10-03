"""
Where detection results go: a CSV file and/or a metadata field on each item.

Metadata values carry a marker, ``[a11y:<bitstream-uuid>]``, at the start. A re-run replaces
only the values whose marker matches a bitstream it just checked; values for other
bitstreams, and every other field, are left alone.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

CSV_COLUMNS = (
    "item_uuid",
    "item_title",
    "bitstream_uuid",
    "bitstream_filename",
    "verdict",
    "errors",
    "warnings",
    "pages",
    "analysis",
)

MARKER_RE = re.compile(r"^\[a11y:([0-9a-fA-F-]{36})\]")
FIELD_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)?$")


def marker(bitstream_uuid: str) -> str:
    return f"[a11y:{bitstream_uuid}]"


def metadata_text(bitstream_uuid: str, bitstream_name: str, report: str) -> str:
    return f"{marker(bitstream_uuid)} {bitstream_name}\n{report}"


def split_field(name: str) -> tuple[str, str, str | None]:
    """'local.accessibility.report' -> ('local', 'accessibility', 'report')."""
    if not FIELD_RE.match(name):
        raise ValueError(
            f"{name!r} is not a metadata field name like schema.element or schema.element.qualifier"
        )
    parts = name.split(".")
    return parts[0], parts[1], parts[2] if len(parts) == 3 else None


def plan_metadata_patch(
    item_metadata: dict[str, list[dict[str, Any]]],
    field: str,
    new_values: dict[str, str],
) -> list[dict[str, Any]]:
    """
    JSON Patch operations that put ``new_values`` (bitstream uuid -> text) on the item.

    Existing values with a matching marker are replaced in place; values that are already
    identical produce no operation; new ones are appended.
    """
    existing = item_metadata.get(field) or []
    ops: list[dict[str, Any]] = []
    done: set[str] = set()
    for index, value in enumerate(existing):
        match = MARKER_RE.match(value.get("value") or "")
        if not match:
            continue
        uuid = match.group(1)
        if uuid in new_values and uuid not in done:
            done.add(uuid)
            if value.get("value") != new_values[uuid]:
                ops.append(
                    {
                        "op": "replace",
                        "path": f"/metadata/{field}/{index}",
                        "value": {"value": new_values[uuid], "language": None},
                    }
                )
    # Always append one value per operation with "/-". It works whether or not the field
    # already has values, whereas "add /metadata/<field>" with a list stores only the first
    # value (seen on DSpace 11; see docs/API_GOTCHAS.md).
    for uuid, text in new_values.items():
        if uuid not in done:
            ops.append(
                {
                    "op": "add",
                    "path": f"/metadata/{field}/-",
                    "value": {"value": text, "language": None},
                }
            )
    return ops


def stale_markers(
    item_metadata: dict[str, list[dict[str, Any]]], field: str, current_bitstreams: set[str]
) -> list[str]:
    """Bitstream UUIDs that have a report value but are no longer in the item."""
    stale = []
    for value in item_metadata.get(field) or []:
        match = MARKER_RE.match(value.get("value") or "")
        if match and match.group(1) not in current_bitstreams:
            stale.append(match.group(1))
    return stale


class CsvSink:
    """One row per bitstream (or per item without a PDF), written as results come in."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=CSV_COLUMNS)
        self._writer.writeheader()
        self.rows = 0

    def write(self, row: dict[str, Any]) -> None:
        self._writer.writerow({k: row.get(k, "") for k in CSV_COLUMNS})
        self._fh.flush()
        self.rows += 1

    def close(self) -> None:
        self._fh.close()
