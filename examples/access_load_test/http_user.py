"""
One httpx session per simulated user, recording every request into the metrics collector.

No retries, ever: a 503 or a timeout is a measurement. Bodies are read in full only up to
``max_body_bytes`` (HTML and JSON that a persona needs to parse); everything else goes
through the streaming sink in ``sink.py``.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from access_load_test.config import RUN_ID_HEADER, RunConfig
from access_load_test.metrics import MetricsCollector, RequestRecord, classify_url
from access_load_test.sink import DownloadResult, TrafficBudget, stream_and_discard

EDGE_HEADERS = ("cf-ray", "cf-cache-status", "x-sucuri-id", "x-akamai-request-id", "x-amz-cf-id")
EDGE_SERVERS = ("cloudflare", "akamaighost", "sucuri", "cloudfront", "varnish", "imperva")


def looks_like_edge_block(status: int | None, headers: dict[str, str]) -> bool:
    """A 403/429/503 that carries a CDN/WAF signature came from the edge, not from DSpace."""
    if status not in (403, 429, 503):
        return False
    if any(h in headers for h in EDGE_HEADERS):
        return True
    server = headers.get("server", "").lower()
    return any(s in server for s in EDGE_SERVERS)


@dataclass(slots=True)
class FetchResult:
    url: str
    final_url: str
    status: int | None
    text: str | None
    headers: dict[str, str] = field(default_factory=dict)
    duration_s: float = 0.0
    bytes_received: int = 0
    error: str | None = None
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and self.status < 400


class HttpUser:
    def __init__(
        self,
        user_id: str,
        persona: str,
        cfg: RunConfig,
        collector: MetricsCollector,
        *,
        user_agent: str,
        budget: TrafficBudget,
        inflight: asyncio.Semaphore,
        http2: bool = False,
        accept_language: str = "en-US,en;q=0.9",
        max_body_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self.user_id = user_id
        self.persona = persona
        self.cfg = cfg
        self.collector = collector
        self.budget = budget
        self.inflight = inflight
        self.max_body_bytes = max_body_bytes
        self.headers = {
            "User-Agent": user_agent,
            "Accept-Language": accept_language,
            RUN_ID_HEADER: cfg.run_id,
        }
        timeout = httpx.Timeout(cfg.http_timeout_s, connect=10.0)
        self.client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            http2=http2,
            headers=self.headers,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self.requests = 0
        self.errors = 0

    @property
    def phase(self) -> str:
        return self.collector.phase

    async def close(self) -> None:
        await self.client.aclose()

    # -- fetching ----------------------------------------------------------------

    async def get(
        self,
        url: str,
        *,
        req_class: str | None = None,
        action_id: str | None = None,
        accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        params: dict[str, str] | None = None,
        want_body: bool = True,
    ) -> FetchResult:
        """GET a page or JSON document; body kept only up to ``max_body_bytes``."""
        cls = req_class or classify_url(url)
        headers = {"Accept": accept}
        start = time.perf_counter()
        status: int | None = None
        text: str | None = None
        error: str | None = None
        received = 0
        truncated = False
        final_url = url
        resp_headers: dict[str, str] = {}
        ttfb: float | None = None
        async with self.inflight:
            self.collector.inflight_inc()
            try:
                async with self.client.stream("GET", url, headers=headers, params=params) as r:
                    status = r.status_code
                    final_url = str(r.url)
                    ttfb = time.perf_counter() - start
                    resp_headers = {k.lower(): v for k, v in r.headers.items()}
                    chunks: list[bytes] = []
                    async for chunk in r.aiter_bytes(64 * 1024):
                        received += len(chunk)
                        if want_body and received <= self.max_body_bytes:
                            chunks.append(chunk)
                        elif want_body and not truncated:
                            truncated = True
                    if want_body:
                        raw = b"".join(chunks)
                        text = raw.decode(r.encoding or "utf-8", errors="replace")
            except httpx.TimeoutException as e:
                error = f"timeout: {type(e).__name__}"
            except httpx.HTTPError as e:
                error = f"{type(e).__name__}: {e}"[:200]
            except Exception as e:  # never let one user thread die on a surprise
                error = f"{type(e).__name__}: {e}"[:200]
            finally:
                self.collector.inflight_dec()
        duration = time.perf_counter() - start
        self._record(
            url=final_url or url,
            req_class=cls,
            status=status,
            duration=duration,
            ttfb=ttfb,
            received=received,
            error=error,
            headers=resp_headers,
            action_id=action_id,
            truncated=truncated,
        )
        return FetchResult(
            url, final_url, status, text, resp_headers, duration, received, error, truncated
        )

    async def download(self, url: str, *, action_id: str | None = None) -> DownloadResult:
        """Stream a bitstream through the byte-counting sink; nothing is stored."""
        cap = int(self.cfg.max_download_mb * 1024 * 1024) if self.cfg.max_download_mb > 0 else None
        async with self.inflight:
            self.collector.inflight_inc()
            try:
                result = await stream_and_discard(
                    self.client,
                    url,
                    headers={"Accept": "*/*"},
                    max_bytes=cap,
                    budget=self.budget,
                )
            finally:
                self.collector.inflight_dec()
        if result.error == "traffic_budget_exhausted":
            return result
        self._record(
            url=url,
            req_class="bitstream",
            status=result.status,
            duration=result.duration_s,
            ttfb=result.ttfb_s,
            received=result.bytes_received,
            error=result.error,
            headers=result.headers,
            action_id=action_id,
            truncated=result.truncated,
        )
        return result

    def _record(
        self,
        *,
        url: str,
        req_class: str,
        status: int | None,
        duration: float,
        ttfb: float | None,
        received: int,
        error: str | None,
        headers: dict[str, str],
        action_id: str | None,
        truncated: bool,
    ) -> None:
        self.requests += 1
        rec = RequestRecord(
            ts_end=time.time(),
            user_id=self.user_id,
            persona=self.persona,
            phase=self.phase,
            method="GET",
            url=url,
            req_class=req_class,
            status=status,
            duration_s=duration,
            ttfb_s=ttfb,
            bytes_received=received,
            error=error,
            source="httpx",
            action_id=action_id,
            edge_block=looks_like_edge_block(status, headers),
            truncated=truncated,
        )
        if not rec.ok:
            self.errors += 1
        self.collector.record(rec)


def same_site(url: str, allowed_hosts: set[str]) -> bool:
    return (urlparse(url).hostname or "") in allowed_hosts


__all__ = ["FetchResult", "HttpUser", "looks_like_edge_block", "same_site"]
