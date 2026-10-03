"""
Scope: which items and PDFs a detection run looks at.

Five modes, each yielding ScopedItem objects:

    bitstream   one bitstream UUID (checked even if it is not in the ORIGINAL bundle)
    item        one item UUID
    collection  every item Discovery finds in one collection
    workspace   in-progress submissions: the current user's, or everyone's
    workflow    items in workflow: all of them, or the current user's pool/claimed tasks

Then pdf_bitstreams() lists the PDFs in an item's ORIGINAL bundle.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from dspace_client import DSpaceClient

SCOPES = ("bitstream", "item", "collection", "workspace", "workflow")
PDF_MIME = "application/pdf"


@dataclass
class ScopedItem:
    item: dict[str, Any]
    state: str  # archived | withdrawn | workspace | workflow | in-progress
    workspaceitem_id: int | None = None
    workflowitem_id: int | None = None
    only_bitstream: dict[str, Any] | None = None  # bitstream mode: check just this one

    @property
    def uuid(self) -> str:
        return str(self.item["uuid"])

    @property
    def title(self) -> str:
        return first_value(self.item, "dc.title") or self.item.get("name") or "(untitled)"


@dataclass
class PdfTarget:
    bitstream: dict[str, Any]
    format_known: bool = True  # False when the format registry did not say PDF; verify bytes
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def uuid(self) -> str:
        return str(self.bitstream["uuid"])

    @property
    def name(self) -> str:
        return str(self.bitstream.get("name") or "")

    @property
    def checksum(self) -> str | None:
        checksum = self.bitstream.get("checkSum") or {}
        value = checksum.get("value")
        return f"{checksum.get('checkSumAlgorithm', 'MD5').lower()}:{value}" if value else None

    @property
    def size(self) -> int | None:
        size = self.bitstream.get("sizeBytes")
        return int(size) if size is not None else None


def first_value(dso: dict[str, Any], field_name: str) -> str | None:
    values = (dso.get("metadata") or {}).get(field_name) or []
    return values[0].get("value") if values else None


def item_state(item: dict[str, Any]) -> str:
    if item.get("withdrawn"):
        return "withdrawn"
    if item.get("inArchive"):
        return "archived"
    return "in-progress"


def _embedded(obj: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = (obj.get("_embedded") or {}).get(key)
    return value if isinstance(value, dict) else None


async def iter_scope(
    client: DSpaceClient,
    scope: str,
    *,
    uuid: str | None = None,
    mine: bool = False,
    eperson_uuid: str | None = None,
) -> AsyncIterator[ScopedItem]:
    """Yield the items in scope. ``mine`` selects the current user's workspace/tasks."""
    if scope == "bitstream":
        if not uuid:
            raise ValueError("bitstream scope needs a bitstream UUID")
        bitstream = await client.get_bitstream(uuid)
        item = await client.get_bitstream_item(uuid)
        yield ScopedItem(item=item, state=item_state(item), only_bitstream=bitstream)

    elif scope == "item":
        if not uuid:
            raise ValueError("item scope needs an item UUID")
        item = await client.get_item(uuid)
        yield ScopedItem(item=item, state=item_state(item))

    elif scope == "collection":
        if not uuid:
            raise ValueError("collection scope needs a collection UUID")
        page = 0
        while True:
            result = await client.search_items(scope=uuid, sort=None, page=page, size=100)
            search = (result.get("_embedded") or {}).get("searchResult") or {}
            objects = (search.get("_embedded") or {}).get("objects") or []
            for obj in objects:
                found = _embedded(obj, "indexableObject")
                if found:
                    yield ScopedItem(item=found, state=item_state(found))
            page += 1
            if page >= (search.get("page") or {}).get("totalPages", 0):
                break

    elif scope == "workspace":
        async for wsi in client.iter_workspace_items(eperson_uuid if mine else None):
            found = _embedded(wsi, "item")
            if found:
                yield ScopedItem(item=found, state="workspace", workspaceitem_id=wsi.get("id"))

    elif scope == "workflow":
        if mine:
            if not eperson_uuid:
                raise ValueError("'my tasks' needs the current user's UUID")
            seen: set[int] = set()
            for listing in (
                client.iter_claimed_tasks(eperson_uuid),
                client.iter_pool_tasks(eperson_uuid),
            ):
                async for task in listing:
                    wfi = _embedded(task, "workflowitem")
                    found = _embedded(wfi, "item") if wfi else None
                    if wfi and found and wfi.get("id") not in seen:
                        seen.add(wfi["id"])
                        yield ScopedItem(item=found, state="workflow", workflowitem_id=wfi["id"])
        else:
            async for wfi in client.iter_workflow_items():
                found = _embedded(wfi, "item")
                if found:
                    yield ScopedItem(item=found, state="workflow", workflowitem_id=wfi.get("id"))
    else:
        raise ValueError(f"unknown scope {scope!r}; expected one of {', '.join(SCOPES)}")


async def pdf_bitstreams(client: DSpaceClient, scoped: ScopedItem) -> list[PdfTarget]:
    """The PDFs to check for one scoped item (ORIGINAL bundle only, unless bitstream mode)."""
    if scoped.only_bitstream is not None:
        return [PdfTarget(bitstream=scoped.only_bitstream, format_known=False)]

    bundles_resp = await client.get_item_bundles(scoped.uuid)
    bundles = (
        (bundles_resp.get("_embedded") or {}).get("bundles") or bundles_resp.get("bundles") or []
    )
    targets: list[PdfTarget] = []
    for bundle in bundles:
        if bundle.get("name") != "ORIGINAL":
            continue
        # Paged: get_bundle_bitstreams() returns only the first page (20 by default).
        async for bitstream in client.iter_pages(
            f"core/bundles/{bundle['uuid']}/bitstreams",
            "bitstreams",
            {"embed": "format"},
            method_name="get_bundle_bitstreams",
        ):
            fmt = _embedded(bitstream, "format") or {}
            mimetype = (fmt.get("mimetype") or "").lower()
            name = str(bitstream.get("name") or "").lower()
            if mimetype == PDF_MIME:
                targets.append(PdfTarget(bitstream=bitstream))
            elif (not mimetype or fmt.get("supportLevel") == "UNKNOWN") and name.endswith(".pdf"):
                targets.append(PdfTarget(bitstream=bitstream, format_known=False))
    return targets
