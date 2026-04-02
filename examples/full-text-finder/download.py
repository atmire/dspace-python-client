"""Download PDF bytes from a resolved URL."""

from __future__ import annotations

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36"
)


async def download_full_text(
    http: httpx.AsyncClient,
    url: str,
    *,
    timeout_s: float,
) -> tuple[bytes, str]:
    """
    GET the resource; follow redirects (httpx default).

    Returns (content, suggested_filename) where filename may be ``file.pdf`` default.
    """
    r = await http.get(
        url,
        follow_redirects=True,
        timeout=timeout_s,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    r.raise_for_status()
    cd = r.headers.get("content-disposition") or ""
    name = "fulltext.pdf"
    if "filename=" in cd:
        part = cd.split("filename=", 1)[1].strip().strip('"').split(";")[0]
        if part:
            name = part
    return r.content, name


__all__ = ["download_full_text"]
