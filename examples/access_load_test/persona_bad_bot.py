"""
Bad bot: pretends to be a browser, ignores robots.txt, follows every link it can find.

Closed-loop mode (default) fetches one URL at a time per bot, waiting for each response,
with the configured think time as the gap. Open-loop mode launches a new fetch every
``think_time`` seconds whether or not earlier ones finished, up to a per-bot in-flight
cap. Open loop models the pile-up a scraper population causes when the server slows down.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque

from access_load_test.context import RunContext
from access_load_test.html_links import extract_links
from access_load_test.http_user import HttpUser

FRONTIER_CAP = 50000
OPEN_LOOP_MIN_INTERVAL_S = 0.05
OPEN_LOOP_MAX_INFLIGHT_PER_BOT = 20


class _Crawler:
    def __init__(self, user: HttpUser, ctx: RunContext) -> None:
        assert ctx.pool is not None
        self.user = user
        self.ctx = ctx
        self.rng = ctx.rng_for(user.user_id)
        self.pool = ctx.pool
        self.visited: set[str] = set()
        self.frontier: deque[tuple[str, int]] = deque()
        self.restarts = 0
        self._reseed()

    def _reseed(self) -> None:
        self.visited.clear()
        self.frontier.clear()
        for u in self.pool.seeds():
            self.frontier.append((u, 0))
        for u in self.pool.collections[:20] + self.pool.communities[:20]:
            self.frontier.append((u, 0))
        for item in self.rng.sample(self.pool.items, k=min(50, len(self.pool.items))):
            self.frontier.append((item.url, 1))
        self.frontier.append((f"{self.pool.ui_base}/search", 0))
        self.frontier.append((f"{self.pool.ui_base}/browse/title", 0))

    def next_url(self) -> tuple[str, int] | None:
        while self.frontier:
            url, depth = self.frontier.popleft()
            if url in self.visited:
                continue
            self.visited.add(url)
            if len(self.visited) >= self.ctx.cfg.bad_bot_max_urls:
                self.restarts += 1
                self.ctx.bump("bad_bot_restarts")
                self._reseed()
            return url, depth
        self._reseed()
        return self.next_url() if self.frontier else None

    async def fetch(self, url: str, depth: int) -> None:
        act = self.ctx.action(self.user.user_id, self.user.persona, "crawl", url)
        res = await self.user.get(url, action_id=act.id)
        act.note_request()
        if not res.ok:
            act.outcome = "timeout" if (res.error and "timeout" in res.error) else "error"
        elif res.text and depth < self.ctx.cfg.bad_bot_max_depth:
            links = extract_links(res.text, res.final_url, self.ctx.cfg.allowed_hosts)
            new = [u for u in links.links if u not in self.visited]
            self.rng.shuffle(new)
            for u in new:
                if len(self.frontier) >= FRONTIER_CAP:
                    break
                self.frontier.append((u, depth + 1))
            if (
                self.ctx.cfg.downloads_enabled
                and links.downloads
                and self.rng.random() < self.ctx.cfg.download_probability
            ):
                dl = await self.user.download(self.rng.choice(links.downloads), action_id=act.id)
                act.note_request()
                act.extra["download_bytes"] = dl.bytes_received
        act.finish()


async def run_bad_bot(user: HttpUser, ctx: RunContext) -> None:
    crawler = _Crawler(user, ctx)
    if ctx.cfg.bad_bot_open_loop:
        await _open_loop(crawler)
    else:
        await _closed_loop(crawler)


async def _closed_loop(c: _Crawler) -> None:
    while not c.ctx.stop.is_set():
        nxt = c.next_url()
        if nxt is None:
            await asyncio.sleep(1.0)
            continue
        await c.fetch(*nxt)
        await c.ctx.think(c.rng)


async def _open_loop(c: _Crawler) -> None:
    interval = max(OPEN_LOOP_MIN_INTERVAL_S, c.ctx.cfg.think_time_s)
    sem = asyncio.Semaphore(OPEN_LOOP_MAX_INFLIGHT_PER_BOT)
    tasks: set[asyncio.Task] = set()

    async def one(url: str, depth: int) -> None:
        async with sem:
            await c.fetch(url, depth)

    while not c.ctx.stop.is_set():
        nxt = c.next_url()
        if nxt is not None:
            if sem.locked():
                c.ctx.bump("bad_bot_open_loop_launch_delayed")
            t = asyncio.create_task(one(*nxt))
            tasks.add(t)
            t.add_done_callback(tasks.discard)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(c.ctx.stop.wait(), timeout=interval)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["run_bad_bot"]
