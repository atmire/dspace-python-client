"""ORCID normalization, extraction, and vocabulary resolution."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from session import _throttled_call, console

from dspace_client import DSpaceAuthClient, DSpaceClient
from dspace_client.throttle import ThrottleController

_ORCID_HYPHENATED = re.compile(
    r"(\d{4}-\d{4}-\d{4}-\d{3}[\dXx])",
    re.IGNORECASE,
)
_ORCID_SEGMENT = re.compile(
    r"^\d{4}-\d{4}-\d{4}-\d{3}[\dXx]$",
    re.IGNORECASE,
)


async def fetch_entry_detail(
    client: DSpaceClient, vocabulary_name: str, authority_uuid: str
) -> dict | None:
    """Optionally fetch vocabulary entry detail for ORCID/display. Returns None on any error."""
    return await client.get_vocabulary_entry_detail(vocabulary_name, authority_uuid)


def _orcid_candidate_from_plain_or_url(v: str) -> str | None:
    """Normalize a metadata value to an https://orcid.org/... URL when it is an ORCID."""
    v = (v or "").strip()
    if not v:
        return None
    low = v.lower()
    if "orcid.org" in low:
        if v.startswith("http"):
            return v
        return f"https://{v.lstrip('/')}" if not v.startswith("//") else f"https:{v}"
    if _ORCID_SEGMENT.match(v) or _ORCID_HYPHENATED.search(v):
        if not v.startswith("http"):
            return f"https://orcid.org/{v}"
        return v
    # Plain compact 16-char (digits + optional trailing X), e.g. person.identifier.orcid
    compact = "".join(ch.upper() for ch in v if ch.isdigit() or ch in "Xx")
    if _valid_orcid_compact(compact):
        hid = f"{compact[0:4]}-{compact[4:8]}-{compact[8:12]}-{compact[12:16]}"
        return f"https://orcid.org/{hid}"
    return None


def extract_orcid_from_entry(entry: dict, detail: dict | None) -> str | None:
    """Get ORCID URL from vocabulary entry or its detail if available."""
    meta = entry.get("metadata") or {}
    for key in (
        "dc.identifier.orcid",
        "orcid",
        "person.identifier.orcid",
    ):
        for lst in meta.get(key) or []:
            if isinstance(lst, dict) and lst.get("value"):
                out = _orcid_candidate_from_plain_or_url(lst["value"])
                if out:
                    return out
    for key in ("dc.identifier.uri",):
        for lst in meta.get(key) or []:
            if isinstance(lst, dict) and lst.get("value"):
                out = _orcid_candidate_from_plain_or_url(lst["value"])
                if out:
                    return out

    if detail and isinstance(detail.get("otherInformation"), dict):
        oi = detail["otherInformation"]
        for key in ("orcid", "dc.identifier.orcid", "person.identifier.orcid"):
            if oi.get(key):
                out = _orcid_candidate_from_plain_or_url(str(oi[key]))
                if out:
                    return out
        for key in ("dc.identifier.uri",):
            if oi.get(key):
                out = _orcid_candidate_from_plain_or_url(str(oi[key]))
                if out:
                    return out
    return None


def _valid_orcid_compact(compact: str) -> bool:
    """ORCID is 16 chars: 15 digits plus a final digit or checksum X."""
    if len(compact) != 16:
        return False
    return all(c.isdigit() for c in compact[:15]) and (compact[15].isdigit() or compact[15] == "X")


def _compact_from_hyphenated(h: str) -> str | None:
    h = (h or "").strip()
    if not _ORCID_SEGMENT.match(h):
        return None
    c = h.upper().replace("-", "")
    return c if _valid_orcid_compact(c) else None


def normalize_orcid_identifier(raw: str) -> str | None:
    """
    Parse user ORCID input into canonical 16-character form (digits + optional trailing X).

    Accepts hyphenated ids, 16-char compact form, and common profile URLs including
    https://www.orcid.org/... (checksum letter X is valid per ORCID spec).
    """
    s = (raw or "").strip()
    if not s:
        return None

    m = _ORCID_HYPHENATED.search(s)
    if m:
        c = _compact_from_hyphenated(m.group(1))
        if c:
            return c

    url_candidate = s
    if not re.match(r"^[a-z][a-z0-9+.-]*://", s, re.IGNORECASE):
        low = s.lower()
        if low.startswith(("www.orcid.org/", "orcid.org/")):
            url_candidate = "https://" + s

    if "://" in url_candidate:
        try:
            pu = urlparse(url_candidate)
            for seg in pu.path.split("/"):
                seg = seg.split("?")[0].strip()
                if not seg:
                    continue
                c = _compact_from_hyphenated(seg)
                if c:
                    return c
        except Exception:
            pass

    lower = s.lower()
    for prefix in (
        "https://www.orcid.org/",
        "http://www.orcid.org/",
        "https://orcid.org/",
        "http://orcid.org/",
        "www.orcid.org/",
        "orcid.org/",
    ):
        if lower.startswith(prefix):
            rest = s[len(prefix) :].strip()
            first = rest.split("/")[0].split("?")[0].strip()
            if first:
                c = _compact_from_hyphenated(first)
                if c:
                    return c
                m2 = _ORCID_HYPHENATED.search(first)
                if m2:
                    c = _compact_from_hyphenated(m2.group(1))
                    if c:
                        return c
            break

    compact = "".join(ch.upper() for ch in s if ch.isdigit() or ch in "Xx")
    if _valid_orcid_compact(compact):
        return compact

    return None


def orcid_hyphenated_from_compact(compact: str) -> str | None:
    """Build 0000-0000-0000-000X from canonical 16-char ORCID."""
    if not _valid_orcid_compact(compact):
        return None
    return f"{compact[0:4]}-{compact[4:8]}-{compact[8:12]}-{compact[12:16]}"


async def resolve_authority_by_orcid(
    client: DSpaceClient,
    vocabulary_name: str,
    orcid_input: str,
    auth: DSpaceAuthClient,
    username: str,
    password: str,
    throttle: ThrottleController,
    max_pages: int = 20,
) -> tuple[str, str] | None:
    """
    Resolve an ORCID id to a local authority (authority_uuid, display_name).

    Uses vocabulary `filter` only to obtain candidates (Solr behavior varies by site):
    tries entryID, hyphenated and compact filters, then first-4-digit pagination.
    Always confirms with a full normalized ORCID match on entry metadata/detail.
    """
    orcid_digits = normalize_orcid_identifier(orcid_input)
    if not orcid_digits:
        return None
    orcid_hyphenated = orcid_hyphenated_from_compact(orcid_digits)

    async def _try_match_in_entries(entries: list[dict]) -> tuple[str, str] | None:
        for e in entries:
            if not isinstance(e, dict) or not e.get("authority"):
                continue
            extracted = extract_orcid_from_entry(e, None)
            detail: dict | None = None
            if not extracted:
                detail = await fetch_entry_detail(client, vocabulary_name, e.get("authority", ""))
                extracted = extract_orcid_from_entry(e, detail)
            ex_norm = normalize_orcid_identifier(extracted) if extracted else None
            if ex_norm == orcid_digits:
                name = (e.get("display") or e.get("value") or "").strip()
                return (e["authority"], name)
        return None

    async def _fetch_entries_filtered(filter_term: str | None, page: int) -> list[dict]:
        resp = await _throttled_call(
            auth,
            client,
            username,
            password,
            throttle,
            lambda: client.get_vocabulary_entries(
                vocabulary_name,
                filter_term=filter_term,
                exact=False,
                page=page,
                size=50,
            ),
        )
        return (resp.get("_embedded") or {}).get("entries") or []

    async def _fetch_entries_by_entry_id(entry_id: str) -> list[dict]:
        resp = await _throttled_call(
            auth,
            client,
            username,
            password,
            throttle,
            lambda eid=entry_id: client.get_vocabulary_entries(
                vocabulary_name,
                filter_term=None,
                entry_id=eid,
                page=0,
                size=50,
            ),
        )
        return (resp.get("_embedded") or {}).get("entries") or []

    # 1) Direct entryID (some sites key authority by ORCID string)
    for eid in (x for x in (orcid_hyphenated, orcid_digits) if x):
        try:
            entries = await _fetch_entries_by_entry_id(eid)
            hit = await _try_match_in_entries(entries)
            if hit:
                return hit
        except Exception:
            pass

    # 2) Filter passes: hyphenated then compact (candidate generation only)
    for ft in (x for x in (orcid_hyphenated, orcid_digits) if x):
        try:
            entries = await _fetch_entries_filtered(ft, 0)
            hit = await _try_match_in_entries(entries)
            if hit:
                return hit
        except Exception:
            pass

    # 3) Broad filter: first 4 digits, paginate
    for page in range(max_pages):
        try:
            entries = await _fetch_entries_filtered(orcid_digits[:4], page)
        except Exception:
            break
        if not entries:
            break
        if page > 0:
            console.print(
                f"[dim]ORCID resolve: scanning vocabulary page {page + 1}/{max_pages} "
                f"(filter={orcid_digits[:4]!r})…[/dim]"
            )
        hit = await _try_match_in_entries(entries)
        if hit:
            return hit

    return None
