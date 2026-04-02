"""Full-text URL discovery (Unpaywall, OpenAlex, OpenAIRE, CORE) — GAS port."""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, unquote, urlparse

import httpx

from dspace_candidates import normalize_doi_string
from rate_limit import HostThrottle
from url_validator import UrlValidationResult, verify_full_text_url

# Throttles per API (seconds between calls)
THROTTLE_UNPAYWALL = HostThrottle(0.25)
THROTTLE_OPENALEX = HostThrottle(0.25)
THROTTLE_OPENAIRE = HostThrottle(0.25)
THROTTLE_CORE = HostThrottle(6.0)
THROTTLE_OPENAIRE_AUTH = HostThrottle(0.25)

_openaire_access_token: str = ""
_openaire_access_exp: float = 0.0


def _local_tag(tag: str) -> str:
    if not tag:
        return ""
    return tag.split("}")[-1]


def is_likely_pdf_url(u: str) -> bool:
    lower = u.lower()
    if lower.endswith(".pdf"):
        return True
    if "blobtype=pdf" in lower:
        return True
    if "=application/pdf" in lower:
        return True
    try:
        p = urlparse(u)
        path = p.path.lower()
        if path.endswith("/pdf"):
            return True
        if path.endswith(".fcgi") and (
            "pdf" in p.query.lower() or "blobtype=pdf" in p.query.lower()
        ):
            return True
        if any(seg == "pdf" for seg in path.split("/")):
            return True
    except Exception:
        pass
    if "/pdf" in lower:
        return True
    if "pdf=" in lower:
        return True
    return False


@dataclass
class ResolveContext:
    """Mutable state passed through the source chain (OpenAlex closed-access hint)."""

    oa_closed: bool = False
    openalex_summary: str = ""


@dataclass(frozen=True)
class SourceHit:
    url: str
    provenance: str


async def _validate(
    http: httpx.AsyncClient,
    url: str,
    timeout_s: float,
) -> UrlValidationResult:
    return await verify_full_text_url(http, url, timeout_s=timeout_s)


async def try_unpaywall(
    http: httpx.AsyncClient,
    doi: str,
    email: str,
    timeout_s: float,
) -> Optional[SourceHit]:
    await THROTTLE_UNPAYWALL.wait()
    api_url = f"https://api.unpaywall.org/v2/{quote(doi, safe='')}?email={quote(email, safe='')}"
    r = await http.get(api_url, follow_redirects=True, timeout=timeout_s)
    if r.status_code >= 400 and r.status_code != 404:
        return None
    try:
        data = r.json()
    except Exception:
        return None

    if data.get("oa_status") == "closed":
        return None

    tried: set[str] = set()

    async def try_location(loc: dict) -> Optional[SourceHit]:
        url = (loc.get("url_for_pdf") or loc.get("url") or "").strip()
        if not url or url in tried:
            return None
        tried.add(url)
        check = await _validate(http, url, timeout_s)
        if not check.is_valid:
            return None
        inst = loc.get("repository_institution") or ""
        prov = f"Unpaywall: {inst}" if inst else "Unpaywall"
        resolved = check.final_url or url
        return SourceHit(url=resolved, provenance=prov)

    loc = data.get("best_oa_location")
    if loc:
        hit = await try_location(loc)
        if hit:
            return hit
    loc = data.get("first_oa_location")
    if loc:
        hit = await try_location(loc)
        if hit:
            return hit
    for loc in data.get("oa_locations") or []:
        hit = await try_location(loc)
        if hit:
            return hit
    return None


async def try_openalex(
    http: httpx.AsyncClient,
    doi: str,
    email: str,
    timeout_s: float,
    ctx: ResolveContext,
) -> Optional[SourceHit]:
    await THROTTLE_OPENALEX.wait()
    doi_url = doi if doi.startswith("https://doi.org/") else f"https://doi.org/{doi}"
    api_url = f"https://api.openalex.org/works/{quote(doi_url, safe='')}?mailto={quote(email, safe='')}"
    r = await http.get(api_url, follow_redirects=True, timeout=timeout_s)
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None

    oa = data.get("open_access") or {}
    oa_status = (oa.get("oa_status") or "unknown") or "unknown"
    ctx.openalex_summary = (
        f"OpenAlex – OA status: {oa_status} · is_oa: {oa.get('is_oa')} · "
        f"repo_fulltext: {oa.get('any_repository_has_fulltext')}"
    )
    if oa_status == "closed":
        ctx.oa_closed = True

    tried: set[str] = set()

    async def try_location(loc: Optional[dict]) -> Optional[SourceHit]:
        if not loc:
            return None
        url = (loc.get("pdf_url") or loc.get("landing_page_url") or "").strip()
        if not url or url in tried:
            return None
        tried.add(url)
        check = await _validate(http, url, timeout_s)
        if not check.is_valid:
            return None
        src = loc.get("source") or {}
        name = src.get("display_name") or ""
        prov = f"OpenAlex: {name}" if name else "OpenAlex"
        resolved = check.final_url or url
        return SourceHit(url=resolved, provenance=prov)

    loc = data.get("best_oa_location")
    if loc:
        hit = await try_location(loc)
        if hit:
            return hit
    for loc in data.get("locations") or []:
        hit = await try_location(loc)
        if hit:
            return hit
    return None


async def fetch_openaire_access_token(
    http: httpx.AsyncClient,
    refresh_token: str,
    timeout_s: float,
) -> str:
    global _openaire_access_token, _openaire_access_exp
    await THROTTLE_OPENAIRE_AUTH.wait()
    url = (
        "https://services.openaire.eu/uoa-user-management/api/users/getAccessToken"
        f"?refreshToken={quote(refresh_token, safe='')}"
    )
    r = await http.get(url, timeout=timeout_s)
    if r.status_code != 200:
        return ""
    try:
        data = r.json()
    except Exception:
        return ""
    tok = data.get("access_token")
    if not tok:
        return ""
    _openaire_access_token = str(tok)
    _openaire_access_exp = time.time() + 55 * 60
    return _openaire_access_token


async def get_openaire_authorization(
    http: httpx.AsyncClient,
    pat: str,
    refresh_token: str,
    timeout_s: float,
) -> str:
    """Return value for Authorization header, or empty string."""
    if pat.strip():
        return f"Bearer {pat.strip()}"
    if not refresh_token.strip():
        return ""
    global _openaire_access_token, _openaire_access_exp
    if _openaire_access_token and time.time() < _openaire_access_exp:
        return f"Bearer {_openaire_access_token}"
    tok = await fetch_openaire_access_token(http, refresh_token.strip(), timeout_s)
    if not tok:
        return ""
    return f"Bearer {tok}"


def _openaire_candidate_urls(xml_text: str) -> list[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    urls: list[str] = []
    for el in root.iter():
        if _local_tag(el.tag) != "webresource":
            continue
        for child in el:
            if _local_tag(child.tag) == "url":
                u = (child.text or "").strip()
                if u and (is_likely_pdf_url(u) or u.lower().endswith((".pdf", ".doc"))):
                    urls.append(u)
    return urls


async def try_openaire(
    http: httpx.AsyncClient,
    doi: str,
    pat: str,
    refresh_token: str,
    timeout_s: float,
    ctx: ResolveContext,
) -> Optional[SourceHit]:
    if ctx.oa_closed:
        return None
    await THROTTLE_OPENAIRE.wait()
    auth = await get_openaire_authorization(http, pat, refresh_token, timeout_s)
    api_url = f"https://api.openaire.eu/search/publications?doi={quote(doi, safe='')}"
    headers = {}
    if auth:
        headers["Authorization"] = auth
    r = await http.get(api_url, headers=headers, timeout=timeout_s)
    if auth and r.status_code in (401, 403):
        await THROTTLE_OPENAIRE.wait()
        r = await http.get(api_url, timeout=timeout_s)
    if r.status_code != 200:
        return None
    for url in _openaire_candidate_urls(r.text):
        check = await _validate(http, url, timeout_s)
        if check.is_valid:
            resolved = check.final_url or url
            return SourceHit(url=resolved, provenance="OpenAIRE")
    return None


async def try_core(
    http: httpx.AsyncClient,
    doi: str,
    api_key: str,
    timeout_s: float,
    ctx: ResolveContext,
) -> Optional[SourceHit]:
    if ctx.oa_closed:
        return None
    if not api_key.strip():
        return None
    await THROTTLE_CORE.wait()
    r = await http.post(
        "https://api.core.ac.uk/v3/discover",
        headers={"Authorization": f"Bearer {api_key.strip()}"},
        json={"doi": normalize_doi_string(doi)},
        timeout=timeout_s,
    )
    if r.status_code == 429:
        return None
    if r.status_code >= 400:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    link = (data.get("fullTextLink") or "").strip()
    if not link:
        return None
    if link.startswith("https://core.ac.uk/redirect/"):
        link = unquote(link[len("https://core.ac.uk/redirect/") :])
    check = await _validate(http, link, timeout_s)
    if not check.is_valid:
        return None
    src = data.get("source") or ""
    prov = f"CORE: {src}" if src else "CORE"
    resolved = check.final_url or link
    return SourceHit(url=resolved, provenance=prov)


__all__ = [
    "ResolveContext",
    "SourceHit",
    "try_core",
    "try_openaire",
    "try_openalex",
    "try_unpaywall",
]
