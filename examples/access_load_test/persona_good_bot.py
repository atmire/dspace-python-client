"""
Good bot: identifies itself, reads robots.txt, obeys Disallow, crawls from the sitemap.

It fetches HTML only (crawlers do not run the Angular app), follows allowed same-site
links, and downloads a linked bitstream now and then. Crawl-delay in robots.txt is
deliberately ignored, as the big crawlers do; the configured think time is the gap.
An optional share of good bots are OAI-PMH harvesters instead of page crawlers.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque

from access_load_test.context import RunContext
from access_load_test.html_links import extract_links
from access_load_test.http_user import HttpUser

FRONTIER_CAP = 10000
_TOKEN_RE = re.compile(r"<resumptionToken[^>]*>([^<]+)</resumptionToken>")


async def run_good_bot(user: HttpUser, ctx: RunContext, *, oai: bool = False) -> None:
    if oai:
        await _run_oai_harvester(user, ctx)
        return
    assert ctx.pool is not None
    rng = ctx.rng_for(user.user_id)
    pool = ctx.pool
    robots = pool.robots
    visited: set[str] = set()
    frontier: deque[str] = deque(pool.seeds())
    for item in rng.sample(pool.items, k=min(200, len(pool.items))):
        frontier.append(item.url)

    while not ctx.stop.is_set():
        if not frontier:
            for item in rng.sample(pool.items, k=min(200, len(pool.items))):
                if item.url not in visited:
                    frontier.append(item.url)
            if not frontier:
                visited.clear()
                frontier.extend(pool.seeds())
        url = frontier.popleft()
        if url in visited:
            continue
        visited.add(url)
        if not robots.allowed(url):
            ctx.collector.robots_skipped += 1
            ctx.bump("good_bot_robots_skipped")
            continue

        act = ctx.action(user.user_id, user.persona, "crawl", url)
        res = await user.get(url, action_id=act.id)
        act.note_request()
        if not res.ok:
            act.outcome = "timeout" if (res.error and "timeout" in res.error) else "error"
        elif res.text:
            links = extract_links(res.text, res.final_url, ctx.cfg.allowed_hosts)
            candidates = [u for u in links.links if u not in visited and robots.allowed(u)]
            rng.shuffle(candidates)
            for u in candidates:
                if len(frontier) >= FRONTIER_CAP:
                    break
                frontier.append(u)
            if (
                ctx.cfg.downloads_enabled
                and links.downloads
                and rng.random() < ctx.cfg.download_probability
            ):
                target = rng.choice(links.downloads)
                if robots.allowed(target):
                    dl = await user.download(target, action_id=act.id)
                    act.note_request()
                    act.extra["download_bytes"] = dl.bytes_received
        act.finish()
        await ctx.think(rng)


async def _run_oai_harvester(user: HttpUser, ctx: RunContext) -> None:
    """ListRecords with resumption tokens, page after page, gap = think time."""
    rng = ctx.rng_for(user.user_id)
    base = f"{ctx.cfg.base_url.rstrip('/')}/server/oai/request"
    token: str | None = None
    empty_streak = 0
    while not ctx.stop.is_set():
        params = {"verb": "ListRecords"}
        if token:
            params["resumptionToken"] = token
        else:
            params["metadataPrefix"] = "oai_dc"
        act = ctx.action(user.user_id, user.persona, "oai", base)
        res = await user.get(
            base,
            req_class="oai",
            action_id=act.id,
            accept="application/xml,text/xml,*/*",
            params=params,
        )
        act.note_request()
        token = None
        if not res.ok:
            act.outcome = "timeout" if (res.error and "timeout" in res.error) else "error"
            empty_streak += 1
        elif res.text:
            m = _TOKEN_RE.search(res.text)
            token = m.group(1).strip() if m and m.group(1).strip() else None
            empty_streak = 0 if token else empty_streak + 1
        act.finish()
        if empty_streak >= 3:
            # Harvest finished (or OAI is unavailable): idle politely, then start over.
            ctx.bump("oai_restarts")
            empty_streak = 0
            await asyncio.sleep(min(30.0, max(5.0, ctx.cfg.think_time_s * 5)))
        await ctx.think(rng)


__all__ = ["run_good_bot"]
