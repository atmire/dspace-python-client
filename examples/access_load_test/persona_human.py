"""
Human persona: a real headless Chromium tab per simulated user (Playwright).

The browser is never throttled. Whatever the Angular app requests, it requests at full
speed, and every one of those requests is observed and recorded. Think time applies only
between top-level actions, and the next action starts no earlier than think time after
the previous one has fully settled (key element visible and the network quiet for half a
second, capped).

Safety inside the browser:
* requests to any host other than the target UI and REST hosts are aborted and counted;
* downloads are disabled; when the app navigates to a bitstream's ``/content`` URL the
  navigation is answered with an empty 204 and the same URL is streamed through the
  byte-counting sink instead, so real download traffic happens and nothing is stored;
* contexts are non-persistent (no profile on disk) and closed after every session.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any
from urllib.parse import urlparse

from access_load_test.config import RUN_ID_HEADER, RunConfig
from access_load_test.context import ActionScope, RunContext
from access_load_test.http_user import HttpUser, looks_like_edge_block
from access_load_test.metrics import BrowserErrorRecord, RequestRecord, classify_url
from access_load_test.pacing import session_length

NETWORK_QUIET_S = 0.5
CLICK_TIMEOUT_MS = 5000
KEY_SELECTORS = {
    "item": "ds-item-page-title-field, .item-page-title-field, ds-item-page h1, ds-item-page h2",
    "search": "ds-search-results, ds-search-page, ds-themed-search-page, .search-results",
    "home": "ds-home-page, ds-themed-home-page, ds-home-news",
    "container": "ds-collection-page, ds-community-page, ds-themed-collection-page, ds-themed-community-page, ds-comcol-page-header",
    "browse": "ds-browse-by, ds-browse-by-page, ds-themed-browse-by-page",
    "other": "main, ds-root",
}
SEARCH_BOX_SELECTORS = (
    "input[data-test='header-search-box']",
    "ds-search-form input[name='query']",
    "input[name='query']",
    "input[type='search']",
)
LANDING_WEIGHTS = (("item", 60), ("home", 25), ("search", 15))
NEXT_WEIGHTS = (
    ("item", 30),
    ("search", 25),
    ("container", 15),
    ("browse", 10),
    ("download", 10),
    ("pagination", 5),
    ("home", 5),
)
BROWSER_ARGS = ["--disable-gpu", "--disable-dev-shm-usage", "--no-first-run", "--mute-audio"]


def should_record_console(msg_type: str, text: str) -> bool:
    """Whether a browser console message is worth recording as a client-side error.

    Only ``error``-level messages, and not the browser's console echo of a failed
    network request (those are already captured as request records, so recording the
    console line too would double-count).
    """
    if msg_type != "error":
        return False
    low = text.strip().lower()
    if low.startswith("failed to load resource"):
        return False
    return bool(low)


def playwright_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("playwright") is not None


class BrowserPool:
    """Launches Chromium processes lazily; ``humans_per_browser`` users share one process."""

    def __init__(self, cfg: RunConfig, ctx: RunContext) -> None:
        self.cfg = cfg
        self.ctx = ctx
        self._pw: Any = None
        self._browsers: dict[int, Any] = {}
        self._lock = asyncio.Lock()
        self.launches = 0

    async def start(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()

    async def browser_for(self, slot: int) -> Any:
        idx = slot // max(1, self.cfg.humans_per_browser)
        async with self._lock:
            b = self._browsers.get(idx)
            if b is not None and b.is_connected():
                return b
            if b is not None:
                self.ctx.browser_restarts += 1
            b = await self._pw.chromium.launch(headless=self.cfg.headless, args=BROWSER_ARGS)
            self._browsers[idx] = b
            self.launches += 1
            return b

    async def close(self) -> None:
        for b in self._browsers.values():
            with contextlib.suppress(Exception):
                await b.close()
        self._browsers.clear()
        if self._pw is not None:
            with contextlib.suppress(Exception):
                await self._pw.stop()
            self._pw = None


class HumanUser:
    def __init__(
        self,
        user_id: str,
        slot: int,
        ctx: RunContext,
        pool: BrowserPool,
        downloader: HttpUser,
    ) -> None:
        self.user_id = user_id
        self.slot = slot
        self.ctx = ctx
        self.cfg = ctx.cfg
        self.pool = pool
        self.downloader = downloader
        self.rng = ctx.rng_for(user_id)
        self.persona = "human"
        self._action: ActionScope | None = None
        self._browser_inflight = 0
        self._last_activity = time.monotonic()
        self._aborted_urls: dict[str, int] = {}
        self._download_tasks: set[asyncio.Task] = set()
        self._bg: set[asyncio.Task] = set()
        self.sessions = 0
        self.pages = 0
        self.link_misses = 0

    # -- lifecycle -------------------------------------------------------------------

    async def run(self) -> None:
        from playwright.async_api import Error as PlaywrightError

        while not self.ctx.stop.is_set():
            try:
                await self._session()
            except PlaywrightError as e:
                self.ctx.bump("human_playwright_errors")
                self.ctx.user_errors[self.user_id] = self.ctx.user_errors.get(self.user_id, 0) + 1
                msg = str(e).lower()
                if "closed" in msg or "crash" in msg or "disconnected" in msg:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.bump("human_unexpected_errors")
                await asyncio.sleep(1.0)
            if not self.ctx.stop.is_set():
                await self.ctx.think(self.rng)
        await self._drain_downloads()

    async def _drain_downloads(self) -> None:
        if self._download_tasks:
            await asyncio.gather(*self._download_tasks, return_exceptions=True)

    async def _session(self) -> None:
        browser = await self.pool.browser_for(self.slot)
        context = await browser.new_context(
            user_agent=self.cfg.human_ua,
            viewport={"width": 1366, "height": 850},
            locale="en-US",
            accept_downloads=False,
            service_workers="block",
            extra_http_headers={RUN_ID_HEADER: self.cfg.run_id},
        )
        self.sessions += 1
        await context.route("**/*", self._route)
        page = await context.new_page()
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_finished)
        page.on("requestfailed", self._on_failed)
        # Client-side browser errors (no HTTP status): JS exceptions, console errors,
        # page crashes. Invisible to server-side telemetry; captured here for the report.
        page.on("pageerror", self._on_page_error)
        page.on("console", self._on_console)
        page.on("crash", self._on_crash)
        try:
            pages = session_length(
                self.cfg.session_pages_mean, self.cfg.session_pages_max, self.rng
            )
            await self._landing(page)
            for _ in range(pages - 1):
                if self.ctx.stop.is_set():
                    break
                await self.ctx.think(self.rng)
                if self.ctx.stop.is_set():
                    break
                await self._next_action(page)
        finally:
            self._action = None
            with contextlib.suppress(Exception):
                await context.close()

    # -- browser event plumbing ------------------------------------------------------

    async def _route(self, route: Any, request: Any) -> None:
        url = request.url
        host = urlparse(url).hostname or ""
        if host not in self.cfg.allowed_hosts:
            self._aborted_urls[url] = self._aborted_urls.get(url, 0) + 1
            self.ctx.collector.note_blocked_third_party()
            await route.abort()
            return
        low = url.lower()
        is_content = "/api/core/bitstreams/" in low and low.split("?", 1)[0].endswith("/content")
        if is_content and (request.resource_type == "document" or request.is_navigation_request()):
            # The app is starting a download. Keep the page where it is and stream
            # the same bytes through the sink instead.
            self._aborted_urls[url] = self._aborted_urls.get(url, 0) + 1
            self._start_download(url)
            await route.fulfill(status=204, body=b"")
            return
        if not self.cfg.view_events and "/api/statistics/" in low:
            self._aborted_urls[url] = self._aborted_urls.get(url, 0) + 1
            self.ctx.bump("view_events_blocked")
            await route.abort()
            return
        await route.continue_()

    def _start_download(self, url: str) -> None:
        action_id = self._action.id if self._action else None
        t = asyncio.create_task(self.downloader.download(url, action_id=action_id))
        self._download_tasks.add(t)
        t.add_done_callback(self._download_tasks.discard)
        self.ctx.bump("human_downloads_started")

    def _on_request(self, request: Any) -> None:
        self._browser_inflight += 1
        self._last_activity = time.monotonic()
        self.ctx.collector.inflight_inc()

    def _on_finished(self, request: Any) -> None:
        self._browser_inflight = max(0, self._browser_inflight - 1)
        self._last_activity = time.monotonic()
        self.ctx.collector.inflight_dec()
        t = asyncio.create_task(self._record(request, failed=False))
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    def _on_failed(self, request: Any) -> None:
        self._browser_inflight = max(0, self._browser_inflight - 1)
        self._last_activity = time.monotonic()
        self.ctx.collector.inflight_dec()
        url = request.url
        if self._aborted_urls.get(url, 0) > 0:
            self._aborted_urls[url] -= 1
            return
        t = asyncio.create_task(self._record(request, failed=True))
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    def _record_browser_error(self, kind: str, text: str) -> None:
        self.ctx.collector.record_browser_error(
            BrowserErrorRecord(
                ts=time.time(),
                user_id=self.user_id,
                persona=self.persona,
                phase=self.ctx.collector.phase,
                kind=kind,
                text=(text or "")[:500],
                url=self._action.url if self._action else "",
                action_id=self._action.id if self._action else None,
            )
        )
        self.ctx.bump("human_browser_" + kind.replace(".", "_"))

    def _on_page_error(self, error: Any) -> None:
        # Playwright passes an Error object; str() gives message + first stack line.
        text = getattr(error, "message", None) or str(error)
        self._record_browser_error("pageerror", text)

    def _on_console(self, message: Any) -> None:
        try:
            mtype = message.type
            text = message.text
        except Exception:
            return
        if should_record_console(mtype, text):
            self._record_browser_error("console.error", text)

    def _on_crash(self, _page: Any) -> None:
        self._record_browser_error("crash", "page crashed")

    async def _record(self, request: Any, *, failed: bool) -> None:
        try:
            timing = request.timing
            response = None if failed else await request.response()
            status = response.status if response is not None else None
            headers = response.headers if response is not None else {}
            received = 0
            if response is not None:
                with contextlib.suppress(Exception):
                    sizes = await request.sizes()
                    received = int(sizes.get("responseBodySize", 0)) + int(
                        sizes.get("responseHeadersSize", 0)
                    )
        except Exception:
            return
        start_ms = float(timing.get("startTime", 0) or 0)
        end_ms = float(timing.get("responseEnd", -1))
        resp_start_ms = float(timing.get("responseStart", -1))
        now = time.time()
        if start_ms <= 0:
            start_s = now
            duration = 0.0
        else:
            start_s = start_ms / 1000.0
            duration = end_ms / 1000.0 if end_ms >= 0 else max(0.0, now - start_s)
        ttfb = resp_start_ms / 1000.0 if resp_start_ms >= 0 else None
        error = None
        aborted = False
        if failed:
            failure = request.failure or "request failed"
            # net::ERR_ABORTED / ERR_CANCELED come from the browser cancelling an in-flight
            # request when a navigation supersedes it or the page/context closes. That is not
            # a server fault, so record it as a (non-error) cancellation, not a failure.
            if "ABORTED" in failure.upper() or "CANCEL" in failure.upper():
                aborted = True
            elif "TIMED_OUT" in failure:
                error = "timeout: " + failure
            else:
                error = failure
        rec = RequestRecord(
            ts_end=start_s + duration if start_ms > 0 else now,
            user_id=self.user_id,
            persona=self.persona,
            phase=self.ctx.collector.phase,
            method=request.method,
            url=request.url,
            req_class=classify_url(request.url, resource_type=request.resource_type),
            status=status,
            duration_s=duration,
            ttfb_s=ttfb,
            bytes_received=received,
            error=error,
            source="browser",
            action_id=self._action.id if self._action else None,
            edge_block=looks_like_edge_block(status, {k.lower(): v for k, v in headers.items()}),
            aborted=aborted,
        )
        self.ctx.collector.record(rec)
        if self._action is not None:
            self._action.note_request(start_s)

    # -- actions -------------------------------------------------------------------

    async def _settle(self, page: Any, kind: str) -> str:
        """Key element visible, then network quiet for NETWORK_QUIET_S; capped."""
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        cap = self.cfg.action_settle_cap_s
        deadline = time.monotonic() + cap
        reason = "network-idle"
        try:
            await page.wait_for_selector(
                KEY_SELECTORS.get(kind, KEY_SELECTORS["other"]),
                timeout=max(1000, int(cap * 500)),
                state="attached",
            )
        except PlaywrightTimeoutError:
            reason = "key-element-missing"
        while time.monotonic() < deadline:
            quiet_for = time.monotonic() - self._last_activity
            if self._browser_inflight == 0 and quiet_for >= NETWORK_QUIET_S:
                return reason
            await asyncio.sleep(0.1)
        return "cap"

    async def _navigation_metrics(self, page: Any) -> dict:
        try:
            nav = await page.evaluate(
                "() => { const n = performance.getEntriesByType('navigation')[0];"
                " return n ? {ttfb_ms: n.responseStart, dcl_ms: n.domContentLoadedEventEnd,"
                " load_ms: n.loadEventEnd, transfer_bytes: n.transferSize} : null }"
            )
        except Exception:
            return {}
        return nav or {}

    async def _landing(self, page: Any) -> None:
        kind = self.rng.choices(
            [k for k, _ in LANDING_WEIGHTS], weights=[w for _, w in LANDING_WEIGHTS]
        )[0]
        assert self.ctx.pool is not None
        if kind == "item":
            item = self.ctx.pool.pick_item(self.rng)
            url = item.url if item else self.cfg.ui_base + "/"
            kind = "item" if item else "home"
        elif kind == "search":
            url = f"{self.cfg.ui_base}/search?query={self.ctx.queries.next_query()}"
        else:
            url = self.cfg.ui_base + "/"
        await self._goto(page, url, kind, action_type=f"landing:{kind}")

    async def _goto(self, page: Any, url: str, kind: str, *, action_type: str) -> None:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        act = self.ctx.action(self.user_id, self.persona, action_type, url)
        self._action = act
        try:
            await page.goto(
                url, wait_until="commit", timeout=int(self.cfg.action_settle_cap_s * 1000)
            )
            act.completion_reason = await self._settle(page, kind)
            act.extra.update(await self._navigation_metrics(page))
            self.pages += 1
        except PlaywrightTimeoutError:
            act.outcome = "timeout"
            act.completion_reason = "navigation-timeout"
        except PlaywrightError as e:
            act.outcome = "error"
            act.completion_reason = str(e).splitlines()[0][:120]
        finally:
            await asyncio.sleep(0)  # let pending event handlers attach to this action
            act.finish()
            self._action = None

    async def _hrefs(self, page: Any) -> list[str]:
        try:
            hrefs = await page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.getAttribute('href'))"
            )
        except Exception:
            return []
        out: list[str] = []
        for h in hrefs:
            if not isinstance(h, str) or not h or '"' in h or "'" in h:
                continue
            if h.startswith(("http://", "https://")):
                p = urlparse(h)
                if (p.hostname or "") not in self.cfg.allowed_hosts:
                    continue
                h = p.path + (f"?{p.query}" if p.query else "")
            if h.startswith("/"):
                out.append(h)
        return out

    @staticmethod
    def _categorise(hrefs: list[str]) -> dict[str, list[str]]:
        cats: dict[str, list[str]] = {
            k: []
            for k in ("item", "container", "browse", "search", "download", "pagination", "home")
        }
        for h in hrefs:
            low = h.lower()
            if low.startswith("/bitstreams/") and low.split("?", 1)[0].endswith("/download"):
                cats["download"].append(h)
            elif low.startswith(("/items/", "/handle/", "/entities/")):
                cats["item"].append(h)
            elif low.startswith(("/collections/", "/communities/")):
                cats["container"].append(h)
            elif low.startswith("/browse"):
                cats["browse"].append(h)
            elif "page=" in low or "spc.page=" in low:
                cats["pagination"].append(h)
            elif low.startswith("/search"):
                cats["search"].append(h)
            elif low in ("/", "/home"):
                cats["home"].append(h)
        return cats

    async def _next_action(self, page: Any) -> None:
        hrefs = await self._hrefs(page)
        cats = self._categorise(hrefs)
        order = self.rng.choices(
            [k for k, _ in NEXT_WEIGHTS], weights=[w for _, w in NEXT_WEIGHTS], k=len(NEXT_WEIGHTS)
        )
        for choice in order:
            if choice == "search":
                await self._search(page)
                return
            if choice == "download" and not self.cfg.downloads_enabled:
                continue
            links = cats.get(choice) or []
            if links:
                href = self.rng.choice(links)
                kind = "container" if choice == "container" else choice
                if choice in ("download", "pagination"):
                    kind = "other"
                await self._click(page, href, kind, action_type=f"click:{choice}")
                if choice == "download":
                    with contextlib.suppress(Exception):
                        await page.go_back(wait_until="commit", timeout=CLICK_TIMEOUT_MS)
                return
        await self._search(page)

    async def _click(self, page: Any, href: str, kind: str, *, action_type: str) -> None:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        url = self.cfg.ui_base + href
        act = self.ctx.action(self.user_id, self.persona, action_type, url)
        self._action = act
        try:
            self._last_activity = time.monotonic()
            await page.locator(f'a[href="{href}"]').first.click(timeout=CLICK_TIMEOUT_MS)
            act.completion_reason = await self._settle(page, kind)
            self.pages += 1
        except PlaywrightTimeoutError:
            self.link_misses += 1
            self.ctx.bump("human_link_click_failed")
            act.outcome = "error"
            act.completion_reason = "click-timeout"
        except PlaywrightError as e:
            act.outcome = "error"
            act.completion_reason = str(e).splitlines()[0][:120]
        finally:
            await asyncio.sleep(0)
            act.finish()
            self._action = None

    async def _search(self, page: Any) -> None:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError

        query = self.ctx.queries.next_query()
        url = f"{self.cfg.ui_base}/search?query={query}"
        box = None
        for sel in SEARCH_BOX_SELECTORS:
            loc = page.locator(sel).first
            with contextlib.suppress(Exception):
                if await loc.count() > 0 and await loc.is_visible():
                    box = loc
                    break
        if box is None:
            self.ctx.bump("human_search_box_missing")
            await self._goto(page, url, "search", action_type="goto:search")
            return
        act = self.ctx.action(self.user_id, self.persona, "search", url)
        self._action = act
        try:
            self._last_activity = time.monotonic()
            await box.fill(query, timeout=CLICK_TIMEOUT_MS)
            await box.press("Enter", timeout=CLICK_TIMEOUT_MS)
            act.completion_reason = await self._settle(page, "search")
            self.pages += 1
        except PlaywrightTimeoutError:
            act.outcome = "error"
            act.completion_reason = "search-timeout"
        except PlaywrightError as e:
            act.outcome = "error"
            act.completion_reason = str(e).splitlines()[0][:120]
        finally:
            await asyncio.sleep(0)
            act.finish()
            self._action = None


__all__ = ["BrowserPool", "HumanUser", "playwright_available", "should_record_console"]
