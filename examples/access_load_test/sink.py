"""
Byte-counting download sink: generate real download traffic without ever touching disk.

The response body is streamed in raw (still compressed) chunks, each chunk's length is
added to a counter and the chunk is dropped. Nothing is buffered beyond one chunk, and
nothing is written anywhere. A per-download byte cap closes the connection early once
reached, which is what a browser tab being closed mid-download looks like to the server.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx


@dataclass(slots=True)
class DownloadResult:
    url: str
    status: int | None
    bytes_received: int
    duration_s: float
    ttfb_s: float | None
    error: str | None
    truncated: bool
    headers: dict[str, str]

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and self.status < 400


class TrafficBudget:
    """Run-wide cap on downloaded bytes, shared by every simulated user."""

    def __init__(self, max_bytes: int | None) -> None:
        self.max_bytes = max_bytes
        self.used = 0
        self.refused = 0

    @property
    def exhausted(self) -> bool:
        return self.max_bytes is not None and self.used >= self.max_bytes

    def remaining(self) -> int | None:
        if self.max_bytes is None:
            return None
        return max(0, self.max_bytes - self.used)

    def note(self, n: int) -> None:
        self.used += n


async def stream_and_discard(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    max_bytes: int | None = None,
    budget: TrafficBudget | None = None,
    chunk_size: int = 64 * 1024,
) -> DownloadResult:
    """GET ``url``, count the bytes on the wire, keep nothing."""
    if budget is not None and budget.exhausted:
        budget.refused += 1
        return DownloadResult(url, None, 0, 0.0, None, "traffic_budget_exhausted", False, {})
    cap = max_bytes
    if budget is not None:
        remaining = budget.remaining()
        if remaining is not None:
            cap = remaining if cap is None else min(cap, remaining)

    start = time.perf_counter()
    received = 0
    ttfb: float | None = None
    status: int | None = None
    error: str | None = None
    truncated = False
    resp_headers: dict[str, str] = {}
    try:
        async with client.stream("GET", url, headers=headers) as response:
            status = response.status_code
            ttfb = time.perf_counter() - start
            resp_headers = {k.lower(): v for k, v in response.headers.items()}
            async for chunk in response.aiter_raw(chunk_size):
                received += len(chunk)
                if cap is not None and received >= cap:
                    truncated = True
                    break
    except httpx.TimeoutException as e:
        error = f"timeout: {type(e).__name__}"
    except httpx.HTTPError as e:
        error = f"{type(e).__name__}: {e}"[:200]
    duration = time.perf_counter() - start
    if budget is not None:
        budget.note(received)
    return DownloadResult(url, status, received, duration, ttfb, error, truncated, resp_headers)


__all__ = ["DownloadResult", "TrafficBudget", "stream_and_discard"]
