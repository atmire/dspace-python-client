"""Shared state for one run: config, target pool, query generator, budget, stop signal."""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
import uuid
from dataclasses import dataclass, field

from access_load_test.config import RunConfig
from access_load_test.metrics import ActionRecord, MetricsCollector
from access_load_test.pacing import think_time
from access_load_test.pool import TargetPool
from access_load_test.queries import QueryGenerator
from access_load_test.sink import TrafficBudget


@dataclass
class ActionScope:
    """Measures one top-level user action (a page visit, a crawl fetch, a search)."""

    ctx: RunContext
    user_id: str
    persona: str
    action_type: str
    url: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    started: float = field(default_factory=time.perf_counter)
    outcome: str = "ok"
    completion_reason: str = ""
    request_ts: list[float] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def note_request(self, ts: float | None = None) -> None:
        self.request_ts.append(ts if ts is not None else time.time())

    def burst_max(self) -> int:
        if not self.request_ts:
            return 0
        buckets: dict[int, int] = {}
        for t in self.request_ts:
            b = int(t)
            buckets[b] = buckets.get(b, 0) + 1
        return max(buckets.values())

    def finish(self) -> ActionRecord:
        rec = ActionRecord(
            ts_end=time.time(),
            user_id=self.user_id,
            persona=self.persona,
            phase=self.ctx.collector.phase,
            action_type=self.action_type,
            url=self.url,
            duration_s=time.perf_counter() - self.started,
            requests=len(self.request_ts),
            burst_max_per_s=self.burst_max(),
            outcome=self.outcome,
            completion_reason=self.completion_reason,
            extra=self.extra,
        )
        self.ctx.collector.record_action(rec)
        return rec


@dataclass
class RunContext:
    cfg: RunConfig
    collector: MetricsCollector
    queries: QueryGenerator
    budget: TrafficBudget
    inflight: asyncio.Semaphore
    stop: asyncio.Event
    pool: TargetPool | None = None
    master_rng: random.Random = field(default_factory=random.Random)
    user_errors: dict[str, int] = field(default_factory=dict)
    browser_restarts: int = 0
    persona_notes: dict[str, int] = field(default_factory=dict)

    def rng_for(self, user_id: str) -> random.Random:
        return random.Random(
            f"{self.cfg.seed}:{user_id}" if self.cfg.seed is not None else self.master_rng.random()
        )

    def action(self, user_id: str, persona: str, action_type: str, url: str) -> ActionScope:
        return ActionScope(self, user_id, persona, action_type, url)

    async def think(self, rng: random.Random) -> None:
        """Sleep the think time, but wake up early if the run is stopping."""
        delay = think_time(self.cfg.think_time_s, rng, fixed=self.cfg.fixed_think_time)
        if delay <= 0:
            await asyncio.sleep(0)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.stop.wait(), timeout=delay)

    def bump(self, key: str, n: int = 1) -> None:
        self.persona_notes[key] = self.persona_notes.get(key, 0) + n


__all__ = ["ActionScope", "RunContext"]
