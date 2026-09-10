"""Adaptive single-threaded throttling for DSpace operations."""

import asyncio
import os
from collections import deque
from dataclasses import dataclass

from rich.console import Console

console = Console()


@dataclass
class ThrottleConfig:
    """Configuration for adaptive delay-based throttling."""

    initial_delay: float = 1.0
    min_delay: float = 0.1
    max_delay: float = 5.0
    ramp_up_factor: float = 0.9  # < 1.0 → shorter delay (faster)
    ramp_down_factor: float = 1.5  # > 1.0 → longer delay (slower)
    window_size: int = 20  # number of recent operations to track
    adjust_interval: int = 10  # operations between adjustment checks
    latency_good_threshold: float = 0.75  # seconds (p95 below this → safe to speed up)
    latency_bad_threshold: float = 2.0  # seconds (p95 above this → slow down)


class ThrottleController:
    """Adaptive controller for delay between calls (no concurrency)."""

    def __init__(self, config: ThrottleConfig):
        self.config = config

        # Load bounds/flags from environment (with sane defaults)
        min_delay_env = os.environ.get("DSPACE_THROTTLE_MIN_DELAY")
        max_delay_env = os.environ.get("DSPACE_THROTTLE_MAX_DELAY")
        try:
            if min_delay_env is not None:
                self.config.min_delay = float(min_delay_env)
        except ValueError:
            pass
        try:
            if max_delay_env is not None:
                self.config.max_delay = float(max_delay_env)
        except ValueError:
            pass

        self.current_delay: float = max(
            self.config.min_delay,
            min(self.config.max_delay, self.config.initial_delay),
        )

        adaptive_flag = os.environ.get("DSPACE_THROTTLE_ADAPTIVE", "1").lower()
        self.adaptive_enabled: bool = adaptive_flag not in ("0", "false", "no")

        self._durations: deque[float] = deque(maxlen=self.config.window_size)
        self._status_codes: deque[int | None] = deque(maxlen=self.config.window_size)
        self._ops_since_adjust: int = 0
        self._lock = asyncio.Lock()

    async def before_call(self) -> None:
        """Sleep for the current delay before issuing a request."""
        if self.current_delay > 0:
            await asyncio.sleep(self.current_delay)

    async def after_call(
        self,
        duration: float,
        success: bool,
        status_code: int | None = None,
    ) -> None:
        """Record outcome and optionally adjust delay."""
        async with self._lock:
            self._durations.append(duration)
            # Track only HTTP-ish codes; other errors get None
            self._status_codes.append(status_code)
            self._ops_since_adjust += 1

            if not self.adaptive_enabled:
                return

            if self._ops_since_adjust < self.config.adjust_interval:
                return

            self._ops_since_adjust = 0
            await self._adjust_delay()

    async def _adjust_delay(self) -> None:
        """Adjust `current_delay` based on recent latency and status codes."""
        if not self._durations:
            return

        recent = list(self._durations)
        recent_status = list(self._status_codes)

        # Compute a simple p95 latency over recent durations
        sorted_times = sorted(recent)
        idx = max(0, min(len(sorted_times) - 1, int(len(sorted_times) * 0.95) - 1))
        p95_latency = sorted_times[idx] if sorted_times else 0.0

        had_429_or_5xx = any(
            (code is not None and (code == 429 or 500 <= code < 600)) for code in recent_status
        )

        old_delay = self.current_delay
        new_delay = old_delay

        # Slow down on load signals: 429, 5xx, or high latency
        if had_429_or_5xx or p95_latency > self.config.latency_bad_threshold:
            new_delay = min(
                self.config.max_delay,
                self.current_delay * self.config.ramp_down_factor,
            )
            reason = "saw 429/5xx" if had_429_or_5xx else f"high p95 latency={p95_latency:.2f}s"
            if new_delay > old_delay * 1.01:
                console.print(
                    f"[dim]Adaptive throttle: slowing down to {new_delay:.2f}s ({reason}).[/dim]"
                )
        # Speed up cautiously when latency is very good and no load signals
        elif (
            not had_429_or_5xx
            and p95_latency > 0
            and p95_latency < self.config.latency_good_threshold
        ):
            new_delay = max(
                self.config.min_delay,
                self.current_delay * self.config.ramp_up_factor,
            )
            if new_delay < old_delay * 0.99:
                console.print(
                    f"[dim]Adaptive throttle: speeding up to {new_delay:.2f}s "
                    f"(p95 latency={p95_latency:.2f}s).[/dim]"
                )

        self.current_delay = max(self.config.min_delay, min(self.config.max_delay, new_delay))
