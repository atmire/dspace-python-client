"""
Metrics for the access load test: per-request records, time windows, trend detection.

Design notes
------------
* Every HTTP request, whether issued by httpx (bots, downloads) or observed inside a
  Chromium page (humans), becomes one ``RequestRecord`` and lands in the window of its
  **completion** time. Using completion time means a window is final once the clock
  passes it; nothing arrives late.
* Windows are summarised into ``WindowStats`` as soon as they close. Raw durations are
  kept only for the open window, so memory stays flat no matter how long the run is.
* ``TrendDetector`` compares each closed window against the baseline phase and flags
  **degradation onset** (latency trending up while the server still answers) separately
  from the **breaking point** (errors, timeouts, extreme latency).
* ``LoopLagMonitor`` measures how late the generator's own event loop wakes up. If the
  generator is saturated, the results say nothing about the server, and the report says so.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import math
import os
import re
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

# ---- request classification -----------------------------------------------------------

STATIC_SUFFIXES = (
    ".js",
    ".css",
    ".map",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".webp",
    ".json",
    ".webmanifest",
)

CLASS_ORDER = (
    "ssr-item",
    "ssr-page",
    "ssr-search",
    "ssr-browse",
    "search",
    "browse",
    "rest",
    "event",
    "thumbnail",
    "bitstream",
    "static",
    "oai",
    "crawl-meta",
    "other",
)


def classify_url(url: str, *, resource_type: str | None = None, hint: str | None = None) -> str:
    """
    Map a URL (plus optional browser resource type or caller hint) to a request class.

    ``hint`` wins when given (the caller knows it is downloading a bitstream). Browser
    ``resource_type`` distinguishes thumbnails (images) from downloads on the same
    ``/content`` URL.
    """
    if hint:
        return hint
    parsed = urlparse(url)
    path = parsed.path or "/"
    lower = path.lower()

    if "/oai" in lower and ("verb=" in (parsed.query or "").lower() or lower.endswith("/oai")):
        return "oai"
    if lower.endswith("/robots.txt") or "sitemap" in lower:
        return "crawl-meta"

    api_idx = lower.find("/api/")
    if api_idx != -1:
        api_path = lower[api_idx + 4 :]
        if api_path.startswith(("/discover/search", "/discover/facets")):
            return "search"
        if api_path.startswith("/discover/browses"):
            return "browse"
        if api_path.startswith("/statistics/"):
            return "event"
        if api_path.startswith("/core/bitstreams/") and api_path.endswith("/content"):
            if resource_type == "image":
                return "thumbnail"
            return "bitstream"
        return "rest"

    if lower.endswith(STATIC_SUFFIXES) or "/assets/" in lower:
        return "static"
    if resource_type in ("script", "stylesheet", "font", "image", "media", "manifest"):
        return "static"

    if lower.startswith("/bitstreams/") and lower.endswith("/download"):
        return "bitstream"
    if lower.startswith(("/search", "/discover")):
        return "ssr-search"
    if lower.startswith("/browse"):
        return "ssr-browse"
    if lower.startswith(("/items/", "/handle/", "/entities/")):
        return "ssr-item"
    if resource_type in (None, "document", "fetch", "xhr"):
        return "ssr-page"
    return "other"


# ---- records ---------------------------------------------------------------------------


@dataclass(slots=True)
class RequestRecord:
    ts_end: float
    user_id: str
    persona: str
    phase: str
    method: str
    url: str
    req_class: str
    status: int | None
    duration_s: float
    ttfb_s: float | None
    bytes_received: int
    error: str | None = None
    source: str = "httpx"
    action_id: str | None = None
    edge_block: bool = False
    truncated: bool = False
    aborted: bool = False

    @property
    def ok(self) -> bool:
        # A browser-cancelled request (navigation superseded it, or the tab closed) is not
        # a failure: it never got a chance to succeed or fail on the server's account.
        if self.aborted:
            return True
        return self.error is None and self.status is not None and self.status < 400

    @property
    def is_timeout(self) -> bool:
        return self.error is not None and "timeout" in self.error.lower()

    @property
    def is_server_error(self) -> bool:
        """A server fault: 5xx or a transport error (timeout/connection reset).

        Deliberately excludes: browser-cancelled requests; 4xx like 401/403/404/400 (the
        Angular app makes anonymous requests that legitimately return 401, and crawlers hit
        404 on stale links); and 429, which is the server *deliberately* shedding load (see
        ``is_rate_limited``). Those are all fast, correct responses, not signs the server is
        failing. The breaking-point detector keys on this, not on the raw >= 400 count.
        """
        if self.aborted:
            return False
        if self.error is not None:
            return True
        return self.status is not None and self.status >= 500

    @property
    def is_rate_limited(self) -> bool:
        """429 Too Many Requests: the server (or its SSR layer / edge) is shedding load.

        A capacity signal worth reporting on its own, but NOT a fault: DSpace's Angular SSR
        commonly rate-limits bot traffic to /search and /items by design.
        """
        return not self.aborted and self.status == 429


@dataclass(slots=True)
class ActionRecord:
    ts_end: float
    user_id: str
    persona: str
    phase: str
    action_type: str
    url: str
    duration_s: float
    requests: int
    burst_max_per_s: int
    outcome: str
    completion_reason: str = ""
    extra: dict = field(default_factory=dict)


@dataclass(slots=True)
class BrowserErrorRecord:
    """A client-side browser event with no HTTP status: a JS exception, a console
    error, or a page crash. Captured from Playwright, invisible to server telemetry."""

    ts: float
    user_id: str
    persona: str
    phase: str
    kind: str  # "pageerror" | "console.error" | "crash"
    text: str
    url: str
    action_id: str | None = None


def error_template(text: str, limit: int = 160) -> str:
    """Collapse UUIDs, numbers and whitespace so similar browser errors group together."""
    t = re.sub(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "{uuid}",
        text,
        flags=re.IGNORECASE,
    )
    t = re.sub(r"\b\d[\d.,:]*\b", "{n}", t)
    t = " ".join(t.split())
    return t[:limit]


# ---- percentiles -----------------------------------------------------------------------


def percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile on an already sorted list. ``q`` in [0, 1]."""
    if not sorted_values:
        return 0.0
    k = max(0, min(len(sorted_values) - 1, math.ceil(q * len(sorted_values)) - 1))
    return sorted_values[k]


@dataclass
class ClassStats:
    count: int = 0
    errors: int = 0
    server_errors: int = 0
    rate_limited: int = 0
    timeouts: int = 0
    edge_blocks: int = 0
    bytes_received: int = 0
    p50: float = 0.0
    p90: float = 0.0
    p95: float = 0.0
    p99: float = 0.0
    max: float = 0.0
    mean: float = 0.0
    ttfb_p50: float | None = None
    mb_per_s: float | None = None

    @property
    def error_rate(self) -> float:
        return self.errors / self.count if self.count else 0.0

    @property
    def server_error_rate(self) -> float:
        return self.server_errors / self.count if self.count else 0.0

    @property
    def rate_limited_rate(self) -> float:
        return self.rate_limited / self.count if self.count else 0.0


def summarise(records: list[RequestRecord], window_s: float) -> ClassStats:
    if not records:
        return ClassStats()
    durations = sorted(r.duration_s for r in records)
    ttfbs = sorted(r.ttfb_s for r in records if r.ttfb_s is not None)
    total_bytes = sum(r.bytes_received for r in records)
    stats = ClassStats(
        count=len(records),
        errors=sum(1 for r in records if not r.ok),
        server_errors=sum(1 for r in records if r.is_server_error),
        rate_limited=sum(1 for r in records if r.is_rate_limited),
        timeouts=sum(1 for r in records if r.is_timeout),
        edge_blocks=sum(1 for r in records if r.edge_block),
        bytes_received=total_bytes,
        p50=percentile(durations, 0.50),
        p90=percentile(durations, 0.90),
        p95=percentile(durations, 0.95),
        p99=percentile(durations, 0.99),
        max=durations[-1],
        mean=statistics.fmean(durations),
        ttfb_p50=percentile(ttfbs, 0.50) if ttfbs else None,
    )
    transfer_s = sum(r.duration_s for r in records if r.bytes_received > 0)
    if transfer_s > 0 and total_bytes > 0:
        stats.mb_per_s = (total_bytes / (1024 * 1024)) / transfer_s
    return stats


@dataclass
class WindowStats:
    index: int
    start_ts: float
    end_ts: float
    phase: str
    total: ClassStats
    by_class: dict[str, ClassStats]
    by_persona: dict[str, ClassStats]
    status_counts: dict[str, int]
    actions: int
    actions_by_type: dict[str, int]
    action_p50_s: float | None
    action_timeouts: int
    active_users: int
    in_flight_max: int
    blocked_third_party: int
    offered_action_rate: float | None
    achieved_action_rate: float
    loop_lag_p95_ms: float | None
    loop_lag_max_ms: float | None
    load_avg_1m: float | None

    @property
    def rps(self) -> float:
        span = self.end_ts - self.start_ts
        return self.total.count / span if span > 0 else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["rps"] = round(self.rps, 3)
        return d


# ---- collector -------------------------------------------------------------------------


class MetricsCollector:
    """Accumulates records into windows; the orchestrator calls ``roll()`` once per tick."""

    def __init__(self, window_s: float, *, top_slow: int = 50, log_sink=None) -> None:
        self.window_s = float(window_s)
        self.t0 = time.time()
        self.phase = "setup"
        self._open: dict[int, list[RequestRecord]] = defaultdict(list)
        self._open_actions: dict[int, list[ActionRecord]] = defaultdict(list)
        self.windows: list[WindowStats] = []
        self.status_counts: Counter[str] = Counter()
        self.class_counts: Counter[str] = Counter()
        self.persona_counts: Counter[str] = Counter()
        self.error_messages: Counter[str] = Counter()
        self.total_requests = 0
        self.total_errors = 0
        self.total_server_errors = 0
        self.total_rate_limited = 0
        self.total_browser_errors = 0
        self.browser_error_counts: Counter[tuple[str, str]] = Counter()
        self._browser_error_samples: list[BrowserErrorRecord] = []
        self._browser_error_sample_cap = 100
        self.total_bytes = 0
        self.total_actions = 0
        self.blocked_third_party = 0
        self.robots_skipped = 0
        self.in_flight = 0
        self._in_flight_max_open: dict[int, int] = defaultdict(int)
        self._blocked_open: dict[int, int] = defaultdict(int)
        self.active_users = 0
        self._top_slow: list[tuple[float, int, str, str, int | None]] = []
        self._slow_seq = 0
        self._top_slow_n = top_slow
        self._log_sink = log_sink
        self.offered_action_rate: float | None = None
        self.lag = LoopLagMonitor()
        self._window_phase: dict[int, str] = {}
        self._last_rolled = -1
        self.errors_by_class_url: Counter[tuple[str, str, str]] = Counter()

    # -- bookkeeping ---------------------------------------------------------------

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def window_index(self, ts: float) -> int:
        return int((ts - self.t0) // self.window_s)

    def inflight_inc(self) -> None:
        self.in_flight += 1
        idx = self.window_index(time.time())
        self._in_flight_max_open[idx] = max(self._in_flight_max_open[idx], self.in_flight)

    def inflight_dec(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)

    def record(self, rec: RequestRecord) -> None:
        idx = self.window_index(rec.ts_end)
        self._open[idx].append(rec)
        self._window_phase.setdefault(idx, rec.phase)
        self.total_requests += 1
        self.total_bytes += rec.bytes_received
        self.class_counts[rec.req_class] += 1
        self.persona_counts[rec.persona] += 1
        if rec.aborted:
            key = "aborted(browser)"
        elif rec.status is not None:
            key = str(rec.status)
        else:
            key = rec.error or "error"
        self.status_counts[key] += 1
        if rec.is_server_error:
            self.total_server_errors += 1
        if rec.is_rate_limited:
            self.total_rate_limited += 1
        if not rec.ok:
            self.total_errors += 1
            msg = rec.error or f"HTTP {rec.status}"
            self.error_messages[msg] += 1
            self.errors_by_class_url[(rec.req_class, msg, _url_template(rec.url))] += 1
        self._slow_seq += 1
        entry = (rec.duration_s, self._slow_seq, rec.req_class, rec.url, rec.status)
        if len(self._top_slow) < self._top_slow_n:
            heapq.heappush(self._top_slow, entry)
        elif entry > self._top_slow[0]:
            heapq.heapreplace(self._top_slow, entry)
        if self._log_sink is not None:
            self._log_sink.write_request(rec)

    def record_action(self, rec: ActionRecord) -> None:
        idx = self.window_index(rec.ts_end)
        self._open_actions[idx].append(rec)
        self._window_phase.setdefault(idx, rec.phase)
        self.total_actions += 1
        if self._log_sink is not None:
            self._log_sink.write_action(rec)

    def record_browser_error(self, rec: BrowserErrorRecord) -> None:
        """Aggregate a client-side browser error (JS exception / console error / crash)."""
        self.total_browser_errors += 1
        self.browser_error_counts[(rec.kind, error_template(rec.text))] += 1
        if len(self._browser_error_samples) < self._browser_error_sample_cap:
            self._browser_error_samples.append(rec)
        if self._log_sink is not None and hasattr(self._log_sink, "write_browser_error"):
            self._log_sink.write_browser_error(rec)

    def top_browser_errors(self, n: int = 25) -> list[dict]:
        return [
            {"kind": k, "error_template": t, "count": c}
            for (k, t), c in self.browser_error_counts.most_common(n)
        ]

    def browser_error_samples(self) -> list[dict]:
        return [asdict(r) for r in self._browser_error_samples]

    def note_blocked_third_party(self) -> None:
        self.blocked_third_party += 1
        idx = self.window_index(time.time())
        self._blocked_open[idx] += 1

    # -- windows -----------------------------------------------------------------

    def roll(self, now: float | None = None, *, final: bool = False) -> list[WindowStats]:
        """Close every window that ended before ``now`` (all of them when ``final``)."""
        now = time.time() if now is None else now
        current = self.window_index(now)
        closed: list[WindowStats] = []
        pending = sorted(set(self._open) | set(self._open_actions) | set(self._in_flight_max_open))
        for idx in pending:
            if idx <= self._last_rolled:
                continue
            if idx >= current and not final:
                break
            closed.append(self._close(idx))
            self._last_rolled = idx
        self.windows.extend(closed)
        return closed

    def _close(self, idx: int) -> WindowStats:
        recs = self._open.pop(idx, [])
        acts = self._open_actions.pop(idx, [])
        inflight_max = self._in_flight_max_open.pop(idx, 0)
        blocked = self._blocked_open.pop(idx, 0)
        start = self.t0 + idx * self.window_s
        by_class: dict[str, list[RequestRecord]] = defaultdict(list)
        by_persona: dict[str, list[RequestRecord]] = defaultdict(list)
        statuses: Counter[str] = Counter()
        for r in recs:
            by_class[r.req_class].append(r)
            by_persona[r.persona].append(r)
            statuses[str(r.status) if r.status is not None else (r.error or "error")] += 1
        actions_by_type: Counter[str] = Counter(a.action_type for a in acts)
        action_durations = sorted(a.duration_s for a in acts if a.outcome == "ok")
        lag_p95, lag_max = self.lag.take_window()
        phase = self._window_phase.pop(idx, self.phase)
        try:
            load1 = os.getloadavg()[0]
        except (AttributeError, OSError):
            load1 = None
        return WindowStats(
            index=idx,
            start_ts=start,
            end_ts=start + self.window_s,
            phase=phase,
            total=summarise(recs, self.window_s),
            by_class={k: summarise(v, self.window_s) for k, v in by_class.items()},
            by_persona={k: summarise(v, self.window_s) for k, v in by_persona.items()},
            status_counts=dict(statuses),
            actions=len(acts),
            actions_by_type=dict(actions_by_type),
            action_p50_s=percentile(action_durations, 0.5) if action_durations else None,
            action_timeouts=sum(1 for a in acts if a.outcome == "timeout"),
            active_users=self.active_users,
            in_flight_max=inflight_max,
            blocked_third_party=blocked,
            offered_action_rate=self.offered_action_rate,
            achieved_action_rate=len(acts) / self.window_s,
            loop_lag_p95_ms=lag_p95,
            loop_lag_max_ms=lag_max,
            load_avg_1m=load1,
        )

    # -- summaries ---------------------------------------------------------------

    def slowest(self) -> list[dict]:
        rows = sorted(self._top_slow, reverse=True)
        return [
            {"duration_s": round(d, 3), "class": c, "url": u, "status": s} for d, _, c, u, s in rows
        ]

    def top_errors(self, n: int = 25) -> list[dict]:
        return [
            {"class": c, "error": e, "url_template": u, "count": k}
            for (c, e, u), k in self.errors_by_class_url.most_common(n)
        ]


def _url_template(url: str) -> str:
    """Collapse UUIDs and numbers so errors group by endpoint rather than by object."""
    path = urlparse(url).path
    path = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "{uuid}", path)
    return re.sub(r"/\d+(?=/|$)", "/{n}", path)


# ---- generator health ------------------------------------------------------------------


class LoopLagMonitor:
    """Measures event-loop wake-up lag: sleep(0.25) that returns 0.35 later means 100 ms lag."""

    def __init__(self, interval_s: float = 0.25) -> None:
        self.interval_s = interval_s
        self._samples_ms: list[float] = []
        self.max_ms_overall = 0.0
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="loop-lag-monitor")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            t = loop.time()
            await asyncio.sleep(self.interval_s)
            lag_ms = max(0.0, (loop.time() - t - self.interval_s) * 1000)
            self._samples_ms.append(lag_ms)
            self.max_ms_overall = max(self.max_ms_overall, lag_ms)

    def take_window(self) -> tuple[float | None, float | None]:
        if not self._samples_ms:
            return None, None
        s = sorted(self._samples_ms)
        self._samples_ms = []
        return percentile(s, 0.95), s[-1]


# ---- trend detection -------------------------------------------------------------------


@dataclass
class Baseline:
    p50: dict[str, float]
    p95: dict[str, float]
    total_p50: float
    total_p95: float
    action_p50: float | None
    windows: int
    requests: int

    def to_dict(self) -> dict:
        return asdict(self)


def build_baseline(windows: list[WindowStats]) -> Baseline | None:
    """Pool the baseline-phase windows into per-class reference latencies."""
    base = [w for w in windows if w.phase == "baseline" and w.total.count > 0]
    if not base:
        return None
    # Weighted by count so a quiet window does not dominate.
    p50: dict[str, float] = {}
    p95: dict[str, float] = {}
    classes = {c for w in base for c in w.by_class}
    for c in classes:
        rows = [
            (w.by_class[c].p50, w.by_class[c].p95, w.by_class[c].count)
            for w in base
            if c in w.by_class
        ]
        n = sum(r[2] for r in rows)
        if n == 0:
            continue
        p50[c] = sum(r[0] * r[2] for r in rows) / n
        p95[c] = sum(r[1] * r[2] for r in rows) / n
    n_total = sum(w.total.count for w in base)
    total_p50 = sum(w.total.p50 * w.total.count for w in base) / n_total
    total_p95 = sum(w.total.p95 * w.total.count for w in base) / n_total
    acts = [w.action_p50_s for w in base if w.action_p50_s is not None]
    return Baseline(
        p50=p50,
        p95=p95,
        total_p50=total_p50,
        total_p95=total_p95,
        action_p50=statistics.fmean(acts) if acts else None,
        windows=len(base),
        requests=n_total,
    )


@dataclass
class Signal:
    """A detected event: when it happened and what the load looked like."""

    kind: str
    window_index: int
    at_s: float
    req_class: str
    active_users: int
    rps: float
    baseline_value: float | None
    observed_value: float
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Verdict:
    onset: Signal | None = None
    breaking: Signal | None = None
    collapse: Signal | None = None
    rate_limited: Signal | None = None
    onset_by_class: dict[str, Signal] = field(default_factory=dict)
    generator_unreliable: bool = False
    generator_reasons: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.breaking:
            return "breaking"
        if self.onset:
            return "degraded"
        return "healthy"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "onset": self.onset.to_dict() if self.onset else None,
            "breaking": self.breaking.to_dict() if self.breaking else None,
            "collapse": self.collapse.to_dict() if self.collapse else None,
            "rate_limited": self.rate_limited.to_dict() if self.rate_limited else None,
            "onset_by_class": {k: v.to_dict() for k, v in self.onset_by_class.items()},
            "generator_unreliable": self.generator_unreliable,
            "generator_reasons": list(self.generator_reasons),
            "notes": list(self.notes),
        }


class TrendDetector:
    """
    Watches closed windows and raises onset / breaking / collapse signals.

    Onset rule (per class and overall): rolling median of p50 over the last
    ``confirm_windows`` windows is above ``baseline * factor`` and above
    ``baseline + min_increase`` for ``confirm_windows`` consecutive windows.

    Breaking rule: error rate >= ``break_error_rate`` (with at least 10 requests),
    or p95 >= ``break_p95_s``, or any timeouts, for ``break_confirm_windows`` in a row.

    Collapse rule: achieved action rate below 70 % of offered for 3 windows in a row.
    """

    def __init__(
        self,
        *,
        factor: float = 1.5,
        min_increase_s: float = 0.05,
        confirm_windows: int = 3,
        break_error_rate: float = 0.05,
        break_p95_s: float = 10.0,
        break_confirm_windows: int = 2,
        min_requests_per_window: int = 5,
        min_requests_for_error_rate: int = 20,
        rate_limit_confirm_windows: int = 2,
        loop_lag_unreliable_ms: float = 250.0,
    ) -> None:
        self.factor = factor
        self.min_increase_s = min_increase_s
        self.confirm_windows = max(1, confirm_windows)
        self.break_error_rate = break_error_rate
        self.break_p95_s = break_p95_s
        self.break_confirm_windows = max(1, break_confirm_windows)
        self.min_requests = min_requests_per_window
        self.min_requests_for_error_rate = min_requests_for_error_rate
        self.rate_limit_confirm_windows = max(1, rate_limit_confirm_windows)
        self.loop_lag_unreliable_ms = loop_lag_unreliable_ms
        self.baseline: Baseline | None = None
        self.verdict = Verdict()
        self._p50_history: dict[str, list[float]] = defaultdict(list)
        self._above: dict[str, int] = defaultdict(int)
        self._break_streak = 0
        self._rate_limit_streak = 0
        self._collapse_streak = 0
        self._t0: float | None = None
        self._loaded_windows: list[WindowStats] = []

    def set_baseline(self, baseline: Baseline | None) -> None:
        self.baseline = baseline

    def observe(self, w: WindowStats) -> list[Signal]:
        """Feed one closed window; returns any new signals raised by it."""
        if self._t0 is None:
            self._t0 = w.start_ts
        new: list[Signal] = []
        if w.loop_lag_p95_ms is not None and w.loop_lag_p95_ms > self.loop_lag_unreliable_ms:
            self.verdict.generator_unreliable = True
            reason = f"event-loop lag p95 {w.loop_lag_p95_ms:.0f} ms in window {w.index}"
            if reason not in self.verdict.generator_reasons:
                self.verdict.generator_reasons.append(reason)
        if w.phase != "load":
            return new
        self._loaded_windows.append(w)
        if self.baseline is None and len(self._loaded_windows) == 2:
            # No baseline phase: use the first loaded window as a stand-in and say so.
            self.baseline = build_baseline_from_loaded(self._loaded_windows[:1])
            self.verdict.notes.append(
                "No baseline phase was run; the first loaded window is used as the reference."
            )
        if w.total.count < self.min_requests:
            return new
        at_s = w.end_ts - self._t0

        # --- onset, per class and overall ---
        if self.baseline is not None:
            targets: list[tuple[str, float, float]] = [
                ("all", self.baseline.total_p50, w.total.p50)
            ]
            for c, cs in w.by_class.items():
                if c in self.baseline.p50 and cs.count >= self.min_requests:
                    targets.append((c, self.baseline.p50[c], cs.p50))
            for name, base_p50, p50 in targets:
                hist = self._p50_history[name]
                hist.append(p50)
                del hist[: -self.confirm_windows]
                rolling = statistics.median(hist)
                threshold = max(base_p50 * self.factor, base_p50 + self.min_increase_s)
                if rolling > threshold:
                    self._above[name] += 1
                else:
                    self._above[name] = 0
                if self._above[name] >= self.confirm_windows:
                    if name == "all" and self.verdict.onset is None:
                        sig = Signal(
                            kind="onset",
                            window_index=w.index,
                            at_s=at_s,
                            req_class=name,
                            active_users=w.active_users,
                            rps=w.rps,
                            baseline_value=base_p50,
                            observed_value=rolling,
                            reason=(
                                f"rolling p50 {rolling:.3f}s vs baseline {base_p50:.3f}s "
                                f"(x{rolling / base_p50 if base_p50 else float('inf'):.2f}) for "
                                f"{self.confirm_windows} windows"
                            ),
                        )
                        self.verdict.onset = sig
                        new.append(sig)
                    elif name != "all" and name not in self.verdict.onset_by_class:
                        sig = Signal(
                            kind="onset",
                            window_index=w.index,
                            at_s=at_s,
                            req_class=name,
                            active_users=w.active_users,
                            rps=w.rps,
                            baseline_value=base_p50,
                            observed_value=rolling,
                            reason=f"{name}: rolling p50 {rolling:.3f}s vs baseline {base_p50:.3f}s",
                        )
                        self.verdict.onset_by_class[name] = sig
                        new.append(sig)

        # --- breaking ---
        # Breaking = the server actually failing. Request-level timeouts already count as
        # server faults (is_server_error), so they flow through the error-rate rule with its
        # sample guard; a single transient timeout does not hard-break. Action-settle
        # timeouts (a browser page that never reached its rendered state within the cap) are
        # a client-side/SSR signal - usually rate-limiting - and deliberately do NOT break.
        err_rate = w.total.server_error_rate
        breaking_now = (
            w.total.count >= self.min_requests_for_error_rate
            and w.total.server_errors >= 5
            and err_rate >= self.break_error_rate
        ) or w.total.p95 >= self.break_p95_s
        self._break_streak = self._break_streak + 1 if breaking_now else 0
        if self._break_streak >= self.break_confirm_windows and self.verdict.breaking is None:
            reasons = []
            if w.total.count >= self.min_requests_for_error_rate and w.total.server_errors >= 5:
                reasons.append(f"server-fault rate {err_rate:.1%} (5xx/timeouts)")
            if w.total.p95 >= self.break_p95_s:
                reasons.append(f"p95 {w.total.p95:.2f}s")
            if w.total.timeouts:
                reasons.append(f"{w.total.timeouts} request timeouts")
            sig = Signal(
                kind="breaking",
                window_index=w.index,
                at_s=at_s,
                req_class="all",
                active_users=w.active_users,
                rps=w.rps,
                baseline_value=self.baseline.total_p95 if self.baseline else None,
                observed_value=w.total.p95,
                reason=", ".join(reasons) + f" for {self.break_confirm_windows} windows",
            )
            self.verdict.breaking = sig
            new.append(sig)

        # --- rate limiting (429): a capacity signal, reported but never 'breaking' ---
        rl_now = (
            w.total.count >= self.min_requests_for_error_rate
            and w.total.rate_limited >= 5
            and w.total.rate_limited_rate >= self.break_error_rate
        )
        self._rate_limit_streak = self._rate_limit_streak + 1 if rl_now else 0
        if (
            self._rate_limit_streak >= self.rate_limit_confirm_windows
            and self.verdict.rate_limited is None
        ):
            sig = Signal(
                kind="rate_limited",
                window_index=w.index,
                at_s=at_s,
                req_class="all",
                active_users=w.active_users,
                rps=w.rps,
                baseline_value=None,
                observed_value=w.total.rate_limited_rate,
                reason=(
                    f"server returned 429 for {w.total.rate_limited_rate:.1%} of requests "
                    f"for {self.rate_limit_confirm_windows} windows (SSR/edge shedding load; "
                    "not a fault)"
                ),
            )
            self.verdict.rate_limited = sig
            new.append(sig)

        # --- collapse of achieved vs offered action rate ---
        # Only meaningful once latency has actually risen (onset): otherwise a low achieved
        # rate just reflects fixed client-side action cost (a human "action" includes page
        # settle time), not the server slowing down. Gating on onset removes that false
        # positive, so collapse only ever appears alongside a real degradation signal.
        if self.verdict.onset is not None and w.offered_action_rate and w.offered_action_rate > 0:
            ratio = w.achieved_action_rate / w.offered_action_rate
            self._collapse_streak = self._collapse_streak + 1 if ratio < 0.7 else 0
            if self._collapse_streak >= 3 and self.verdict.collapse is None:
                sig = Signal(
                    kind="collapse",
                    window_index=w.index,
                    at_s=at_s,
                    req_class="actions",
                    active_users=w.active_users,
                    rps=w.rps,
                    baseline_value=w.offered_action_rate,
                    observed_value=w.achieved_action_rate,
                    reason=(
                        f"achieved {w.achieved_action_rate:.2f} actions/s vs offered "
                        f"{w.offered_action_rate:.2f} (ratio {ratio:.2f}) for 3 windows"
                    ),
                )
                self.verdict.collapse = sig
                new.append(sig)
        return new


def build_baseline_from_loaded(windows: list[WindowStats]) -> Baseline | None:
    """Same as ``build_baseline`` but for windows of any phase (fallback path)."""
    if not windows:
        return None
    relabelled = []
    for w in windows:
        d = w.__dict__.copy()
        d["phase"] = "baseline"
        relabelled.append(WindowStats(**d))
    return build_baseline(relabelled)


__all__ = [
    "CLASS_ORDER",
    "ActionRecord",
    "Baseline",
    "BrowserErrorRecord",
    "ClassStats",
    "LoopLagMonitor",
    "MetricsCollector",
    "RequestRecord",
    "Signal",
    "TrendDetector",
    "Verdict",
    "WindowStats",
    "build_baseline",
    "classify_url",
    "error_template",
    "percentile",
    "summarise",
]
