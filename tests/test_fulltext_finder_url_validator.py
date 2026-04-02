"""Tests for examples/full-text-finder/url_validator.py (import via path)."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
import respx

_FFT = Path(__file__).resolve().parents[1] / "examples" / "full-text-finder"
if str(_FFT) not in sys.path:
    sys.path.insert(0, str(_FFT))

from dspace_candidates import extract_doi_from_metadata  # noqa: E402
from sources import try_unpaywall  # noqa: E402
from url_validator import verify_full_text_url  # noqa: E402

_MD = {"language": None, "authority": None, "confidence": -1}


def test_extract_doi_from_metadata() -> None:
    md = {"dc.identifier.doi": [{"value": "10.1234/zen", **_MD}]}
    assert extract_doi_from_metadata(md) == "10.1234/zen"
    uri_md = {
        "dc.identifier.uri": [
            {"value": "https://doi.org/10.9999/TEST", **_MD},
        ]
    }
    assert extract_doi_from_metadata(uri_md) == "10.9999/TEST"


@pytest.mark.asyncio
async def test_verify_direct_pdf_200() -> None:
    with respx.mock:
        respx.get("https://example.org/paper.pdf").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=b"%PDF-1.4 test",
            )
        )
        async with httpx.AsyncClient() as client:
            r = await verify_full_text_url(client, "https://example.org/paper.pdf", timeout_s=5.0)
    assert r.is_valid
    assert "application/pdf" in (r.content_type or "").lower()
    assert r.final_url.startswith("https://example.org/")


@pytest.mark.asyncio
async def test_verify_redirect_then_pdf() -> None:
    with respx.mock:
        respx.get("https://example.org/go").mock(
            return_value=httpx.Response(
                302,
                headers={"Location": "/paper.pdf"},
            )
        )
        respx.get("https://example.org/paper.pdf").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/pdf; charset=binary"},
                content=b"%PDF",
            )
        )
        async with httpx.AsyncClient() as client:
            r = await verify_full_text_url(client, "https://example.org/go", timeout_s=5.0)
    assert r.is_valid
    assert r.final_url.endswith("/paper.pdf")


@pytest.mark.asyncio
async def test_verify_bad_mime() -> None:
    with respx.mock:
        respx.get("https://example.org/html").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"<html/>",
            )
        )
        async with httpx.AsyncClient() as client:
            r = await verify_full_text_url(client, "https://example.org/html", timeout_s=5.0)
    assert not r.is_valid


@pytest.mark.asyncio
async def test_try_unpaywall_returns_hit() -> None:
    payload = {
        "oa_status": "gold",
        "best_oa_location": {
            "url_for_pdf": "https://example.org/paper.pdf",
            "repository_institution": "Test Repo",
        },
    }
    with respx.mock:
        respx.get("https://api.unpaywall.org/v2/10.1%2Ftest?email=test%40example.com").mock(
            return_value=httpx.Response(200, json=payload)
        )
        respx.get("https://example.org/paper.pdf").mock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=b"%PDF-1.4",
            )
        )
        async with httpx.AsyncClient() as client:
            hit = await try_unpaywall(client, "10.1/test", "test@example.com", 10.0)
    assert hit is not None
    assert hit.provenance.startswith("Unpaywall")
    assert "paper.pdf" in hit.url
