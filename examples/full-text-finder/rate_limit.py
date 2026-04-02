"""Async spacing between HTTP calls per host (CORE ~6s like GAS)."""

from __future__ import annotations

import asyncio
import time


class HostThrottle:
    """Ensure at least ``min_interval_s`` between consecutive ``wait()`` calls."""

    def __init__(self, min_interval_s: float) -> None:
        self._min = min_interval_s
        self._last = 0.0

    async def wait(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed < self._min:
            await asyncio.sleep(self._min - elapsed)
        self._last = time.monotonic()


__all__ = ["HostThrottle"]
