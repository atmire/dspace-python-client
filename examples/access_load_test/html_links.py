"""Extract same-site links and page assets from server-side rendered HTML (stdlib only)."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse

_SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:")


@dataclass
class PageLinks:
    links: list[str] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)
    title: str = ""


class _Extractor(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.hrefs: list[str] = []
        self.assets: list[str] = []
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "a" and a.get("href"):
            self.hrefs.append(a["href"] or "")
        elif tag == "link" and a.get("href"):
            rel = (a.get("rel") or "").lower()
            if "stylesheet" in rel or "icon" in rel or "preload" in rel:
                self.assets.append(a["href"] or "")
        elif (tag == "script" and a.get("src")) or (tag == "img" and a.get("src")):
            self.assets.append(a["src"] or "")
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def extract_links(html: str, page_url: str, allowed_hosts: set[str]) -> PageLinks:
    """Absolute, de-fragmented, same-site links; downloads split out from navigation links."""
    parser = _Extractor(page_url)
    with contextlib.suppress(Exception):  # malformed HTML must never kill a bot
        parser.feed(html)
    out = PageLinks(title=parser.title.strip())
    seen: set[str] = set()
    for href in parser.hrefs:
        h = href.strip()
        if not h or h.startswith(_SKIP_SCHEMES) or h.startswith("#"):
            continue
        absolute, _ = urldefrag(urljoin(page_url, h))
        host = urlparse(absolute).hostname or ""
        if host not in allowed_hosts or absolute in seen:
            continue
        seen.add(absolute)
        path = urlparse(absolute).path.lower()
        if (path.startswith("/bitstreams/") and path.endswith("/download")) or (
            "/api/core/bitstreams/" in path and path.endswith("/content")
        ):
            out.downloads.append(absolute)
        else:
            out.links.append(absolute)
    for src in parser.assets:
        s = src.strip()
        if not s or s.startswith(_SKIP_SCHEMES):
            continue
        absolute, _ = urldefrag(urljoin(page_url, s))
        if (urlparse(absolute).hostname or "") in allowed_hosts and absolute not in seen:
            seen.add(absolute)
            out.assets.append(absolute)
    return out


__all__ = ["PageLinks", "extract_links"]
