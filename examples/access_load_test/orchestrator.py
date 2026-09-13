"""
Runs one access load test: setup, baseline phase, load phase, graceful stop.

Phases
------
* ``setup``: robots.txt, sitemaps, a few discovery pages (target pool + vocabulary).
* ``baseline``: one user of the dominant persona for ``baseline_s`` seconds, to get
  unloaded reference latencies per request class.
* ``load``: all users (immediately, or added on the ramp schedule) until the duration
  ends, an auto-stop condition fires, or the operator presses Ctrl-C once.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import random
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.table import Table

from access_load_test.config import RunConfig
from access_load_test.context import RunContext
from access_load_test.http_user import HttpUser
from access_load_test.metrics import (
    Baseline,
    MetricsCollector,
    Signal,
    TrendDetector,
    Verdict,
    WindowStats,
    build_baseline,
)
from access_load_test.pacing import start_offsets
from access_load_test.persona_bad_bot import run_bad_bot
from access_load_test.persona_good_bot import run_good_bot
from access_load_test.pool import discover_targets
from access_load_test.queries import QueryGenerator
from access_load_test.sink import TrafficBudget

STOP_GRACE_S = 20.0


@dataclass
class RunResult:
    cfg: RunConfig
    started_utc: str
    ended_utc: str
    phase_seconds: dict[str, float]
    windows: list[WindowStats]
    baseline: Baseline | None
    verdict: Verdict
    signals: list[Signal]
    pool_summary: dict
    queries: dict
    totals: dict
    status_counts: dict[str, int]
    class_counts: dict[str, int]
    persona_counts: dict[str, int]
    slowest: list[dict]
    top_errors: list[dict]
    browser_errors: list[dict]
    browser_error_samples: list[dict]
    generator: dict
    persona_notes: dict[str, int]
    stop_reason: str
    interrupted: bool
    requests_log: dict | None = None
    dry_run: bool = False
    extra: dict = field(default_factory=dict)


class _UserSet:
    """Tracks running user tasks and the resources to close when they finish."""

    def __init__(self) -> None:
        self.tasks: dict[asyncio.Task, Callable[[], Any]] = {}
        self.started = 0
        self.died = 0

    async def close_all(self) -> None:
        for closer in list(self.tasks.values()):
            with contextlib.suppress(Exception):
                await closer()
        self.tasks.clear()


async def _spawn(
    ctx: RunContext,
    persona: str,
    index: int,
    *,
    browser_pool: Any,
    slot: int,
    oai: bool = False,
) -> tuple[asyncio.Task, Callable[[], Any]]:
    cfg = ctx.cfg
    uid = f"{persona}-{index:03d}"
    if persona == "human":
        from access_load_test.persona_human import HumanUser

        downloader = HttpUser(
            uid,
            "human",
            cfg,
            ctx.collector,
            user_agent=cfg.human_ua,
            budget=ctx.budget,
            inflight=ctx.inflight,
        )
        human = HumanUser(uid, slot, ctx, browser_pool, downloader)
        task = asyncio.create_task(human.run(), name=uid)
        return task, downloader.close
    if persona == "good-bot":
        user = HttpUser(
            uid,
            "good-bot",
            cfg,
            ctx.collector,
            user_agent=cfg.good_bot_ua,
            budget=ctx.budget,
            inflight=ctx.inflight,
        )
        task = asyncio.create_task(run_good_bot(user, ctx, oai=oai), name=uid)
        return task, user.close
    user = HttpUser(
        uid,
        "bad-bot",
        cfg,
        ctx.collector,
        user_agent=cfg.bad_bot_ua,
        budget=ctx.budget,
        inflight=ctx.inflight,
    )
    task = asyncio.create_task(run_bad_bot(user, ctx), name=uid)
    return task, user.close


def _offered_rate(cfg: RunConfig, baseline: Baseline | None, active: int) -> float | None:
    if baseline is None or baseline.action_p50 is None:
        return None
    cycle = cfg.think_time_s + baseline.action_p50
    return active / cycle if cycle > 0 else None


def _live_table(
    cfg: RunConfig, collector: MetricsCollector, detector: TrendDetector, elapsed: float, phase: str
) -> Table:
    t = Table(title=f"access load test  [{phase}]  {elapsed:6.0f}s  run {cfg.run_id}", expand=True)
    for col in (
        "window",
        "users",
        "in-flight",
        "req/s",
        "p50",
        "p95",
        "search p50",
        "ssr p50",
        "err",
        "MB",
        "3rd-party",
        "lag p95",
        "verdict",
    ):
        t.add_column(col, justify="right")
    last = collector.windows[-5:]
    for w in last:
        search = w.by_class.get("search")
        ssr = [
            w.by_class[c]
            for c in ("ssr-item", "ssr-page", "ssr-search", "ssr-browse")
            if c in w.by_class
        ]
        ssr_p50 = f"{min(s.p50 for s in ssr):.2f}" if ssr else "-"
        t.add_row(
            str(w.index),
            str(w.active_users),
            str(w.in_flight_max),
            f"{w.rps:.1f}",
            f"{w.total.p50:.2f}",
            f"{w.total.p95:.2f}",
            f"{search.p50:.2f}" if search else "-",
            ssr_p50,
            str(w.total.errors),
            f"{w.total.bytes_received / 1048576:.1f}",
            str(w.blocked_third_party),
            f"{w.loop_lag_p95_ms:.0f}ms" if w.loop_lag_p95_ms is not None else "-",
            detector.verdict.status,
        )
    if not last:
        t.add_row(*(["…"] * 13))
    return t


def install_sigint(ctx: RunContext, on_second: Callable[[], None]) -> Callable[[], None]:
    """First Ctrl-C stops gracefully (reports still written); second one aborts."""
    loop = asyncio.get_running_loop()
    state = {"count": 0}

    def handler() -> None:
        state["count"] += 1
        if state["count"] == 1:
            ctx.bump("sigint")
            ctx.stop.set()
        else:
            on_second()

    try:
        loop.add_signal_handler(signal.SIGINT, handler)
    except (NotImplementedError, RuntimeError):
        return lambda: None
    return lambda: loop.remove_signal_handler(signal.SIGINT)


async def run_load_test(
    cfg: RunConfig, console: Console, *, live: bool = True, request_log: Any = None
) -> RunResult:
    started = time.time()
    started_utc = datetime.now(UTC).isoformat()
    collector = MetricsCollector(cfg.window_s, log_sink=request_log)
    seed = cfg.seed if cfg.seed is not None else random.SystemRandom().randrange(1, 2**31)
    cfg.seed = seed
    master = random.Random(seed)
    queries = QueryGenerator(seed=master.randrange(2**31))
    budget = TrafficBudget(
        int(cfg.max_total_download_gb * 1024**3) if cfg.max_total_download_gb > 0 else None
    )
    ctx = RunContext(
        cfg=cfg,
        collector=collector,
        queries=queries,
        budget=budget,
        inflight=asyncio.Semaphore(cfg.max_inflight),
        stop=asyncio.Event(),
        master_rng=master,
    )
    detector = TrendDetector(
        factor=cfg.onset_factor,
        min_increase_s=cfg.onset_min_increase_s,
        confirm_windows=cfg.onset_confirm_windows,
        break_error_rate=cfg.break_error_rate,
        break_p95_s=cfg.break_p95_s,
        break_confirm_windows=cfg.break_confirm_windows,
    )
    signals: list[Signal] = []
    phase_seconds: dict[str, float] = {}
    stop_reason = "duration"
    interrupted = False
    collector.lag.start()

    def abort_hard() -> None:
        raise KeyboardInterrupt

    restore = install_sigint(ctx, abort_hard)
    browser_pool: Any = None
    users = _UserSet()
    baseline_ctx: RunContext | None = None

    try:
        # ---- setup ----------------------------------------------------------------
        collector.set_phase("setup")
        t_setup = time.time()
        setup_user = HttpUser(
            "setup",
            "setup",
            cfg,
            collector,
            user_agent=cfg.good_bot_ua,
            budget=budget,
            inflight=ctx.inflight,
        )
        try:
            ctx.pool = await discover_targets(setup_user, cfg, queries, master)
        finally:
            await setup_user.close()
        phase_seconds["setup"] = time.time() - t_setup
        pool = ctx.pool
        console.print(
            f"[dim]Target pool: {len(pool.items)} items, {len(pool.collections)} collections, "
            f"{len(pool.communities)} communities (source: {pool.source}); "
            f"robots.txt {'fetched' if pool.robots.fetched else 'missing'}, "
            f"{len(pool.robots.disallow_patterns)} disallow rules; vocabulary {queries.vocabulary_size} words[/dim]"
        )
        if not pool.items:
            raise RuntimeError(
                "No item URLs discovered (no sitemap and discovery returned nothing). Cannot run."
            )
        if cfg.dry_run:
            return _result(
                cfg,
                started_utc,
                phase_seconds,
                collector,
                detector,
                signals,
                ctx,
                "dry-run",
                False,
                dry_run=True,
            )

        personas = cfg.personas()
        if cfg.needs_browser:
            from access_load_test.persona_human import BrowserPool

            browser_pool = BrowserPool(cfg, ctx)
            await browser_pool.start()

        # ---- baseline ---------------------------------------------------------------
        dominant = max(set(personas), key=personas.count)
        if cfg.baseline_s > 0:
            collector.set_phase("baseline")
            t_base = time.time()
            baseline_ctx = dataclasses.replace(ctx, stop=asyncio.Event())
            task, closer = await _spawn(
                baseline_ctx, dominant, 0, browser_pool=browser_pool, slot=0
            )
            collector.active_users = 1
            with (
                Live(console=console, refresh_per_second=2, transient=True)
                if live
                else contextlib.nullcontext() as lv
            ):
                while (
                    time.time() - t_base < cfg.baseline_s
                    and not ctx.stop.is_set()
                    and not task.done()
                ):
                    await asyncio.sleep(min(1.0, cfg.window_s / 4))
                    for w in collector.roll():
                        signals.extend(detector.observe(w))
                    if lv is not None:
                        lv.update(
                            _live_table(cfg, collector, detector, time.time() - started, "baseline")
                        )
            baseline_ctx.stop.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, timeout=STOP_GRACE_S)
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
            with contextlib.suppress(Exception):
                await closer()
            collector.active_users = 0
            # Close the baseline windows so the reference is complete before load starts.
            for w in collector.roll(time.time() + cfg.window_s, final=False):
                signals.extend(detector.observe(w))
            phase_seconds["baseline"] = time.time() - t_base
            detector.set_baseline(build_baseline(collector.windows))
            if detector.baseline is None:
                detector.verdict.notes.append(
                    "Baseline phase produced no requests; first loaded window will be used."
                )
        if ctx.stop.is_set():
            stop_reason = "interrupted"
            interrupted = True
            return _result(
                cfg,
                started_utc,
                phase_seconds,
                collector,
                detector,
                signals,
                ctx,
                stop_reason,
                interrupted,
                phase_seconds_extra=None,
            )

        # ---- load -------------------------------------------------------------------
        collector.set_phase("load")
        t_load = time.time()
        offsets = start_offsets(
            len(personas), min(cfg.think_time_s, 10.0) if cfg.think_time_s > 0 else 2.0, master
        )
        n_good = sum(1 for p in personas if p == "good-bot")
        n_oai = round(n_good * cfg.oai_share)
        started_count = 0
        good_seen = 0
        human_slot = 0

        async def start_user(i: int) -> None:
            nonlocal good_seen, human_slot
            persona = personas[i]
            oai = False
            slot = 0
            if persona == "good-bot":
                oai = good_seen < n_oai
                good_seen += 1
            elif persona == "human":
                slot = human_slot
                human_slot += 1
            task, closer = await _spawn(
                ctx, persona, i + 1, browser_pool=browser_pool, slot=slot, oai=oai
            )
            users.tasks[task] = closer
            users.started += 1
            collector.active_users += 1

            def _done(t: asyncio.Task) -> None:
                collector.active_users = max(0, collector.active_users - 1)
                if not t.cancelled() and t.exception() is not None:
                    users.died += 1
                    ctx.user_errors[t.get_name()] = ctx.user_errors.get(t.get_name(), 0) + 1

            task.add_done_callback(_done)

        with (
            Live(console=console, refresh_per_second=2, transient=False)
            if live
            else contextlib.nullcontext() as lv
        ):
            last_tick = time.time()
            while not ctx.stop.is_set():
                now = time.time()
                elapsed = now - t_load
                target = cfg.ramp.users_at(elapsed, len(personas)) if cfg.ramp else len(personas)
                while (
                    started_count < target
                    and started_count < len(personas)
                    and offsets[started_count] <= elapsed
                ):
                    await start_user(started_count)
                    started_count += 1
                collector.offered_action_rate = _offered_rate(
                    cfg, detector.baseline, collector.active_users
                )
                if now - last_tick >= min(1.0, cfg.window_s / 4):
                    last_tick = now
                    for w in collector.roll():
                        new = detector.observe(w)
                        signals.extend(new)
                        for s in new:
                            console.print(
                                f"[bold yellow]{s.kind.upper()}[/bold yellow] at {s.at_s:.0f}s ({s.req_class}): {s.reason}"
                            )
                    if lv is not None:
                        lv.update(
                            _live_table(cfg, collector, detector, time.time() - started, "load")
                        )
                    if cfg.auto_stop and detector.verdict.breaking is not None:
                        stop_reason = "auto-stop: breaking point"
                        ctx.stop.set()
                    elif cfg.auto_stop and cfg.stop_at_onset and detector.verdict.onset is not None:
                        stop_reason = "auto-stop: degradation onset"
                        ctx.stop.set()
                    elif (
                        started_count == len(personas)
                        and users.tasks
                        and all(t.done() for t in users.tasks)
                    ):
                        stop_reason = "all users stopped"
                        ctx.stop.set()
                if elapsed >= cfg.duration_s:
                    stop_reason = "duration"
                    ctx.stop.set()
                await asyncio.sleep(0.2)
        if ctx.persona_notes.get("sigint"):
            stop_reason = "interrupted (Ctrl-C)"
            interrupted = True
        phase_seconds["load"] = time.time() - t_load
        return _result(
            cfg,
            started_utc,
            phase_seconds,
            collector,
            detector,
            signals,
            ctx,
            stop_reason,
            interrupted,
        )
    finally:
        ctx.stop.set()
        if baseline_ctx is not None:
            baseline_ctx.stop.set()
        pending = [t for t in users.tasks if not t.done()]
        if pending:
            console.print(
                f"[dim]Stopping {len(pending)} users (up to {STOP_GRACE_S:.0f}s grace)…[/dim]"
            )
            with contextlib.suppress(Exception):
                await asyncio.wait(pending, timeout=STOP_GRACE_S)
            for t in pending:
                if not t.done():
                    t.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.gather(*pending, return_exceptions=True)
        await users.close_all()
        if browser_pool is not None:
            await browser_pool.close()
        for w in collector.roll(final=True):
            signals.extend(detector.observe(w))
        await collector.lag.stop()
        restore()


def _result(
    cfg: RunConfig,
    started_utc: str,
    phase_seconds: dict[str, float],
    collector: MetricsCollector,
    detector: TrendDetector,
    signals: list[Signal],
    ctx: RunContext,
    stop_reason: str,
    interrupted: bool,
    *,
    dry_run: bool = False,
    phase_seconds_extra: dict | None = None,
) -> RunResult:
    for w in collector.roll(final=True):
        signals.extend(detector.observe(w))
    load_windows = [w for w in collector.windows if w.phase == "load"]
    load_reqs = sum(w.total.count for w in load_windows)
    load_span = sum(w.end_ts - w.start_ts for w in load_windows)
    gen = {
        "loop_lag_max_ms": round(collector.lag.max_ms_overall, 1),
        "browser_restarts": ctx.browser_restarts,
        "user_task_failures": dict(ctx.user_errors),
        "unreliable": detector.verdict.generator_unreliable,
        "reasons": list(detector.verdict.generator_reasons),
    }
    totals = {
        "requests": collector.total_requests,
        "errors": collector.total_errors,
        "server_errors": collector.total_server_errors,
        "rate_limited_429": collector.total_rate_limited,
        "bytes_received": collector.total_bytes,
        "actions": collector.total_actions,
        "load_phase_requests": load_reqs,
        "load_phase_avg_rps": round(load_reqs / load_span, 3) if load_span > 0 else 0.0,
        "blocked_third_party_requests": collector.blocked_third_party,
        "robots_skipped_urls": collector.robots_skipped,
        "download_bytes": ctx.budget.used,
        "downloads_refused_budget": ctx.budget.refused,
        "browser_js_errors": collector.total_browser_errors,
    }
    return RunResult(
        cfg=cfg,
        started_utc=started_utc,
        ended_utc=datetime.now(UTC).isoformat(),
        phase_seconds={k: round(v, 1) for k, v in phase_seconds.items()},
        windows=list(collector.windows),
        baseline=detector.baseline,
        verdict=detector.verdict,
        signals=list(signals),
        pool_summary=ctx.pool.summary() if ctx.pool else {},
        queries=ctx.queries.stats(),
        totals=totals,
        status_counts=dict(collector.status_counts),
        class_counts=dict(collector.class_counts),
        persona_counts=dict(collector.persona_counts),
        slowest=collector.slowest(),
        top_errors=collector.top_errors(),
        browser_errors=collector.top_browser_errors(),
        browser_error_samples=collector.browser_error_samples(),
        generator=gen,
        persona_notes=dict(ctx.persona_notes),
        stop_reason=stop_reason,
        interrupted=interrupted,
        dry_run=dry_run,
    )


__all__ = ["RunResult", "run_load_test"]
