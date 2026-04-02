"""Verify a full-text URL (redirect chain + MIME) — port of FullTextURLValidator.gs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

import httpx

VALID_MIME_PREFIXES = (
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.oasis.opendocument.text",
    "application/rtf",
    "application/octet-stream",
    "application/x-pdf",
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class UrlValidationResult:
    is_valid: bool
    http_code: int | None
    content_type: str | None
    final_url: str
    notes: str
    redirect_chain: list[dict[str, Any]]


def _mime_ok(content_type: str) -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    return any(ct.startswith(p) for p in VALID_MIME_PREFIXES)


async def verify_full_text_url(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout_s: float,
) -> UrlValidationResult:
    """HEAD/GET with Range bytes=0-0, manual redirects, max 5 hops."""
    max_redirects = 5
    seen: set[str] = set()
    chain: list[dict[str, Any]] = []
    current = url

    headers_base = {
        "User-Agent": USER_AGENT,
        "Range": "bytes=0-0",
        "Accept": "*/*",
    }

    try:
        for _hop in range(max_redirects + 1):
            if current in seen:
                return UrlValidationResult(
                    False,
                    None,
                    None,
                    current,
                    "Redirect loop detected",
                    chain,
                )
            seen.add(current)

            try:
                r = await client.get(
                    current,
                    headers=headers_base,
                    follow_redirects=False,
                    timeout=timeout_s,
                )
            except Exception as e:
                return UrlValidationResult(
                    False,
                    None,
                    None,
                    current,
                    f"Exception during fetch: {e!s}",
                    chain,
                )

            code = r.status_code
            ct = (r.headers.get("content-type") or "").strip()
            chain.append({"url": current, "status": code})

            if 300 <= code < 400:
                loc = r.headers.get("location")
                if not loc:
                    return UrlValidationResult(
                        False,
                        code,
                        ct,
                        current,
                        f"Redirect (HTTP {code}) without Location header",
                        chain,
                    )
                current = urljoin(current, loc)
                continue

            if code == 416:
                r2 = await client.get(
                    current,
                    headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
                    follow_redirects=False,
                    timeout=timeout_s,
                )
                code = r2.status_code
                ct = (r2.headers.get("content-type") or "").strip()
                chain.append({"url": current, "status": code, "retry": "no-range"})

            ok_status = code in (200, 206)
            ok_mime = _mime_ok(ct)
            is_valid = ok_status and ok_mime
            notes = (
                "Valid document link."
                if is_valid
                else f"Invalid content type ({ct!r}) or status ({code})"
            )
            return UrlValidationResult(is_valid, code, ct, current, notes, chain)

        return UrlValidationResult(
            False,
            None,
            None,
            current,
            f"Too many redirects (> {max_redirects})",
            chain,
        )
    except Exception as e:
        return UrlValidationResult(
            False,
            None,
            None,
            current,
            f"Exception: {e!s}",
            chain,
        )


__all__ = ["UrlValidationResult", "verify_full_text_url"]
