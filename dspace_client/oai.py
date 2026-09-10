"""OAI-PMH 2.0 client for DSpace repositories.

The OAI endpoint is at {base_url}/server/oai/request. No authentication is required.
"""

import csv
import hashlib
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self, TypedDict

import httpx
from defusedxml.ElementTree import fromstring as safe_fromstring

from .exceptions import OAIError

# OAI-PMH and Dublin Core namespaces (common in OAI responses)
NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

OAI_PATH = "/server/oai/request"


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def _text(el: ET.Element | None, default: str = "") -> str:
    if el is None:
        return default
    return (el.text or "").strip()


def _find_text(parent: ET.Element | None, path: str, default: str = "") -> str:
    if parent is None:
        return default
    child = parent.find(path, NS)
    return _text(child, default)


@dataclass
class ResumptionToken:
    """Resumption token for paginated ListRecords/ListIdentifiers."""

    value: str
    complete_list_size: int | None = None
    cursor: int | None = None
    expiration_date: str | None = None


@dataclass
class OAIRecord:
    """Single record from ListRecords."""

    identifier: str
    datestamp: str
    status: str | None = None  # "deleted" or None
    metadata: ET.Element | None = None  # raw metadata element for parsing by caller


@dataclass
class ListRecordsResult:
    """One page of ListRecords response."""

    records: list[OAIRecord]
    resumption_token: ResumptionToken | None = None


@dataclass
class IdentifyResult:
    """Response from Identify verb."""

    repository_name: str = ""
    base_url: str = ""
    protocol_version: str = ""
    admin_emails: list[str] = field(default_factory=list)
    earliest_datestamp: str = ""
    deleted_record: str = ""  # no, persistent, transient
    granularity: str = ""  # YYYY-MM-DD or YYYY-MM-DDThh:mm:ssZ
    raw: dict[str, Any] = field(default_factory=dict)


def _check_error(root: ET.Element) -> None:
    """Raise OAIError if response contains an error element."""
    err = root.find("oai:error", NS)
    if err is not None:
        code = err.get("code", "unknown")
        msg = _text(err)
        raise OAIError(code=code, message=msg or code)


def _parse_identify(root: ET.Element) -> IdentifyResult:
    _check_error(root)
    ident = root.find("oai:Identify", NS)
    if ident is None:
        return IdentifyResult()
    emails = [e.text.strip() for e in ident.findall("oai:adminEmail", NS) if e.text]
    return IdentifyResult(
        repository_name=_find_text(ident, "oai:repositoryName"),
        base_url=_find_text(ident, "oai:baseURL"),
        protocol_version=_find_text(ident, "oai:protocolVersion"),
        admin_emails=emails,
        earliest_datestamp=_find_text(ident, "oai:earliestDatestamp"),
        deleted_record=_find_text(ident, "oai:deletedRecord"),
        granularity=_find_text(ident, "oai:granularity"),
    )


def _parse_list_metadata_formats(root: ET.Element) -> list[dict[str, str]]:
    _check_error(root)
    fmt_list = root.find("oai:ListMetadataFormats", NS)
    if fmt_list is None:
        return []
    result = []
    for fmt in fmt_list.findall("oai:metadataFormat", NS):
        result.append(
            {
                "metadataPrefix": _find_text(fmt, "oai:metadataPrefix"),
                "schema": _find_text(fmt, "oai:schema"),
                "metadataNamespace": _find_text(fmt, "oai:metadataNamespace"),
            }
        )
    return result


def _parse_resumption_token(el: ET.Element | None) -> ResumptionToken | None:
    if el is None or not (el.text and el.text.strip()):
        return None
    try:
        complete_list_size = int(el.get("completeListSize", 0))
    except (TypeError, ValueError):
        complete_list_size = None
    try:
        cursor = int(el.get("cursor", 0))
    except (TypeError, ValueError):
        cursor = None
    return ResumptionToken(
        value=el.text.strip(),
        complete_list_size=complete_list_size,
        cursor=cursor,
        expiration_date=el.get("expirationDate"),
    )


def _parse_list_records(root: ET.Element) -> ListRecordsResult:
    _check_error(root)
    list_records = root.find("oai:ListRecords", NS)
    if list_records is None:
        return ListRecordsResult(records=[])
    records = []
    for rec_el in list_records.findall("oai:record", NS):
        header = rec_el.find("oai:header", NS)
        if header is None:
            continue
        status = header.get("status")  # "deleted" or None
        identifier = _find_text(header, "oai:identifier")
        datestamp = _find_text(header, "oai:datestamp")
        metadata_el = rec_el.find("oai:metadata", NS)
        records.append(
            OAIRecord(
                identifier=identifier,
                datestamp=datestamp,
                status=status or None,
                metadata=metadata_el,
            )
        )
    res_el = list_records.find("oai:resumptionToken", NS)
    res_token = _parse_resumption_token(res_el)
    return ListRecordsResult(records=records, resumption_token=res_token)


class OAIClient:
    """Client for OAI-PMH 2.0 requests to a DSpace server.

    The OAI endpoint is at {base_url}/server/oai/request. No authentication required.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 60.0,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.base_url = _normalize_base_url(base_url)
        self.oai_url = f"{self.base_url}{OAI_PATH}"
        self.timeout = timeout
        self._client = http_client
        self._owns_client = http_client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    def _build_url(
        self,
        verb: str,
        metadata_prefix: str | None = None,
        identifier: str | None = None,
        from_: str | None = None,
        until: str | None = None,
        set_spec: str | None = None,
        resumption_token: str | None = None,
    ) -> str:
        params: list[tuple[str, str]] = [("verb", verb)]
        if resumption_token:
            params.append(("resumptionToken", resumption_token))
        else:
            if metadata_prefix:
                params.append(("metadataPrefix", metadata_prefix))
            if identifier:
                params.append(("identifier", identifier))
            if from_:
                params.append(("from", from_))
            if until:
                params.append(("until", until))
            if set_spec:
                params.append(("set", set_spec))
        qs = urllib.parse.urlencode(params)
        return f"{self.oai_url}?{qs}"

    async def _request(self, url: str) -> ET.Element:
        client = await self._get_client()
        response = await client.get(url)
        response.raise_for_status()
        return safe_fromstring(response.text)

    async def identify(self) -> IdentifyResult:
        """Request Identify verb; returns repository info."""
        url = self._build_url("Identify")
        root = await self._request(url)
        return _parse_identify(root)

    async def list_metadata_formats(
        self,
        identifier: str | None = None,
    ) -> list[dict[str, str]]:
        """Request ListMetadataFormats; optional identifier for one item."""
        url = self._build_url(
            "ListMetadataFormats",
            identifier=identifier,
        )
        root = await self._request(url)
        return _parse_list_metadata_formats(root)

    async def list_records_page(
        self,
        metadata_prefix: str,
        from_: str | None = None,
        until: str | None = None,
        set_spec: str | None = None,
        resumption_token: str | None = None,
    ) -> ListRecordsResult:
        """Fetch one page of ListRecords. Call repeatedly with resumption_token until none."""
        if resumption_token:
            url = self._build_url("ListRecords", resumption_token=resumption_token)
        else:
            url = self._build_url(
                "ListRecords",
                metadata_prefix=metadata_prefix,
                from_=from_,
                until=until,
                set_spec=set_spec,
            )
        root = await self._request(url)
        return _parse_list_records(root)

    async def list_records(
        self,
        metadata_prefix: str = "oai_dc",
        from_: str | None = None,
        until: str | None = None,
        set_spec: str | None = None,
    ) -> AsyncIterator[OAIRecord]:
        """Iterate over all records (all pages via resumptionToken)."""
        token: str | None = None
        while True:
            page = await self.list_records_page(
                metadata_prefix=metadata_prefix,
                from_=from_,
                until=until,
                set_spec=set_spec,
                resumption_token=token,
            )
            for rec in page.records:
                yield rec
            if page.resumption_token is None or not page.resumption_token.value:
                break
            token = page.resumption_token.value


# --- PDF detection from oai_dc dc:format ---

PDF_MIME = "application/pdf"


def get_dc_formats(metadata_el: ET.Element | None) -> list[str]:
    """Extract all dc:format values from an oai_dc metadata element."""
    if metadata_el is None:
        return []
    # Metadata contains one child: oai_dc:dc in typical OAI responses
    dc_container = metadata_el.find("oai_dc:dc", NS)
    if dc_container is None:
        for child in metadata_el:
            if child.tag.endswith("}dc") or "dc" in child.tag:
                dc_container = child
                break
        if dc_container is None:
            dc_container = metadata_el
    formats: list[str] = []
    for fmt_el in dc_container.findall(".//dc:format", NS):
        val = _text(fmt_el)
        if val:
            formats.append(val)
    if not formats:
        for fmt_el in dc_container.iter():
            if fmt_el.tag.endswith("}format") or fmt_el.tag == "format":
                val = _text(fmt_el)
                if val:
                    formats.append(val)
    return list(dict.fromkeys(formats))  # preserve order, dedupe


def record_has_pdf(record: OAIRecord) -> bool:
    """Return True if the record has at least one dc:format equal to application/pdf."""
    if record.status == "deleted":
        return False
    formats = get_dc_formats(record.metadata)
    return any(f.strip().lower() == PDF_MIME for f in formats if f)


class OAIRecordParsed(TypedDict):
    """Parsed record for caching: identifier, datestamp, has_pdf."""

    identifier: str
    datestamp: str
    has_pdf: bool


async def iterate_oai_dc_records(
    client: OAIClient,
    from_: str | None = None,
    until: str | None = None,
    set_spec: str | None = None,
) -> AsyncIterator[OAIRecordParsed]:
    """Async iterator over ListRecords (oai_dc) yielding parsed { identifier, datestamp, has_pdf }."""
    async for record in client.list_records(
        metadata_prefix="oai_dc",
        from_=from_,
        until=until,
        set_spec=set_spec,
    ):
        if record.status == "deleted":
            continue
        yield {
            "identifier": record.identifier,
            "datestamp": record.datestamp,
            "has_pdf": record_has_pdf(record),
        }


async def count_items_with_pdf_via_oai(
    client: OAIClient,
    from_: str | None = None,
    until: str | None = None,
    set_spec: str | None = None,
) -> dict[str, int]:
    """Harvest all oai_dc records and return total_items and with_pdf counts."""
    total = 0
    with_pdf = 0
    async for parsed in iterate_oai_dc_records(client, from_=from_, until=until, set_spec=set_spec):
        total += 1
        if parsed["has_pdf"]:
            with_pdf += 1
    return {"total_items": total, "with_pdf": with_pdf}


# --- Persistent CSV cache for PDF counts ---


def _repository_cache_id(base_url: str) -> str:
    """Return a safe filename fragment for the repository (stable per base_url)."""
    normalized = _normalize_base_url(base_url).lower()
    parsed = urllib.parse.urlparse(normalized)
    netloc = parsed.netloc or parsed.path or normalized
    safe = re.sub(r"[^a-z0-9.-]", "_", netloc)
    if parsed.path and parsed.path != "/":
        # Not a security primitive: this only disambiguates cache filenames per
        # repository path, so an explicitly non-security digest is correct here.
        path_hash = hashlib.md5(parsed.path.encode(), usedforsecurity=False).hexdigest()[:8]
        safe = f"{safe}_{path_hash}"
    return safe or "default"


class OAIPDFCountCache:
    """Persistent cache of OAI item identifier -> datestamp, has_pdf for PDF counting.

    One CSV per repository; optional JSON sidecar for last_until (incremental harvest).
    """

    CACHE_FILENAME_PREFIX = "oai_pdf_cache_"
    LAST_UNTIL_FILENAME = "last_until.json"

    def __init__(
        self,
        base_url: str,
        cache_dir: Path | None = None,
    ):
        self.base_url = _normalize_base_url(base_url)
        self._repo_id = _repository_cache_id(self.base_url)
        self._cache_dir = (
            Path(cache_dir) if cache_dir else Path.home() / ".cache" / "dspace-oai-pdf"
        )
        self._cache_path = self._cache_dir / f"{self.CACHE_FILENAME_PREFIX}{self._repo_id}.csv"
        self._last_until_path = (
            self._cache_dir
            / f"{self.CACHE_FILENAME_PREFIX}{self._repo_id}_{self.LAST_UNTIL_FILENAME}"
        )
        self._data: dict[str, dict[str, Any]] = {}  # identifier -> { datestamp, has_pdf }
        self._last_until: str | None = None

    def _ensure_dir(self) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def load(self) -> None:
        """Load cache from CSV and last_until from JSON sidecar if present."""
        self._data = {}
        self._last_until = None
        if self._cache_path.exists():
            try:
                with self._cache_path.open(newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        ident = row.get("identifier", "").strip()
                        if not ident:
                            continue
                        has_pdf = row.get("has_pdf", "0").strip().lower() in ("1", "true", "yes")
                        self._data[ident] = {
                            "datestamp": row.get("datestamp", "").strip(),
                            "has_pdf": has_pdf,
                        }
            except (csv.Error, OSError):
                pass
        if self._last_until_path.exists():
            try:
                with self._last_until_path.open(encoding="utf-8") as f:
                    obj = json.load(f)
                self._last_until = obj.get("last_until")
            except (json.JSONDecodeError, OSError):
                pass

    def get(self, identifier: str) -> dict[str, Any] | None:
        """Return cached { datestamp, has_pdf } for identifier, or None."""
        return self._data.get(identifier)

    def update(self, identifier: str, datestamp: str, has_pdf: bool) -> None:
        """Update or insert cache entry for identifier."""
        self._data[identifier] = {"datestamp": datestamp, "has_pdf": has_pdf}

    def save(self, last_until: str | None = None) -> None:
        """Write cache CSV and optionally update last_until in JSON sidecar atomically."""
        self._ensure_dir()
        if last_until is not None:
            self._last_until = last_until
        tmp_csv = self._cache_path.with_suffix(".csv.tmp")
        with tmp_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["identifier", "datestamp", "has_pdf"])
            writer.writeheader()
            for ident, entry in self._data.items():
                writer.writerow(
                    {
                        "identifier": ident,
                        "datestamp": entry["datestamp"],
                        "has_pdf": "1" if entry["has_pdf"] else "0",
                    }
                )
        tmp_csv.replace(self._cache_path)
        if self._last_until is not None:
            tmp_json = self._last_until_path.with_suffix(".json.tmp")
            with tmp_json.open("w", encoding="utf-8") as f:
                json.dump({"last_until": self._last_until}, f, indent=0)
            tmp_json.replace(self._last_until_path)

    def totals(self) -> tuple[int, int]:
        """Return (total_count, with_pdf_count) from current in-memory cache."""
        total = len(self._data)
        with_pdf = sum(1 for e in self._data.values() if e.get("has_pdf"))
        return total, with_pdf

    @property
    def last_until(self) -> str | None:
        return self._last_until

    @last_until.setter
    def last_until(self, value: str | None) -> None:
        self._last_until = value

    @property
    def cache_path(self) -> Path:
        return self._cache_path
