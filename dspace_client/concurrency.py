"""Adaptive concurrency (and delay) control for DSpace operations."""

import asyncio
import contextlib
import statistics
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class ConcurrencyConfig:
    """Configuration for adaptive concurrency control."""
    initial: int = 4  # Balanced start
    min_concurrency: int = 1
    max_concurrency: int = 128
    ramp_up_threshold: float = 0.1  # 10% improvement
    ramp_down_threshold: float = 0.2  # 20% degradation
    window_size: int = 20  # Operations to track for metrics
    ramp_up_interval: int = 10  # Operations between ramp-up checks
    ramp_up_amount: int = 1  # Conservative ramp-up (1 at a time)
    ramp_down_amount: int = 1  # Conservative ramp-down (1 at a time)


@dataclass
class PerformanceMetrics:
    """Performance metrics for concurrency control."""
    throughput: float  # Operations per second
    p95_latency: float  # 95th percentile latency in seconds
    p50_latency: float  # 50th percentile latency in seconds
    current_concurrency: int
    total_operations: int
    successful_operations: int
    failed_operations: int


class AdaptiveSemaphore:
    """
    Dynamic semaphore whose concurrency limit can be raised or lowered at any time.

    The limit is a plain integer rather than a fixed pool of permits that has to be
    shuffled around, so changing it never blocks in either direction:

    - Raising the limit hands slots to queued waiters immediately.
    - Lowering it needs no action at all. ``release()`` simply stops waking waiters
      until enough in-flight work has finished for ``_active`` to fall below the new
      limit. Work already in flight above the new limit is allowed to finish.

    Waiters are served strictly FIFO.
    """

    def __init__(self, config: ConcurrencyConfig):
        self.config = config
        self._current_limit = self._clamp(config.initial)
        self._active = 0
        self._waiters: deque[asyncio.Future] = deque()

    def _clamp(self, limit: int) -> int:
        """Clamp a requested limit to the configured range."""
        return max(self.config.min_concurrency, min(self.config.max_concurrency, limit))

    @property
    def current_limit(self) -> int:
        """Get current concurrency limit."""
        return self._current_limit

    @property
    def active(self) -> int:
        """Number of slots currently held."""
        return self._active

    def _wake_waiters(self) -> None:
        """Hand slots to queued waiters for as long as the current limit allows."""
        while self._waiters and self._active < self._current_limit:
            waiter = self._waiters.popleft()
            if waiter.done():  # cancelled while queued
                continue
            self._active += 1
            waiter.set_result(True)

    async def acquire(self) -> None:
        """Acquire a slot, waiting until the current limit allows it."""
        # Fast path: spare capacity and nobody queued ahead of us (keeps the queue FIFO).
        if not self._waiters and self._active < self._current_limit:
            self._active += 1
            return

        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            if waiter.done() and not waiter.cancelled():
                # A slot was handed to us but we were cancelled before resuming: pass it on.
                self._active -= 1
                self._wake_waiters()
            else:
                # Already dequeued if a concurrent wake-up beat the cancellation.
                with contextlib.suppress(ValueError):
                    self._waiters.remove(waiter)
            raise

    def release(self) -> None:
        """Release a previously acquired slot."""
        if self._active <= 0:
            msg = "AdaptiveSemaphore released more times than it was acquired"
            raise RuntimeError(msg)
        self._active -= 1
        self._wake_waiters()

    def set_limit(self, new_limit: int) -> int:
        """
        Set the concurrency limit, clamped to the configured range.

        Never blocks. Returns the limit actually applied.
        """
        new_limit = self._clamp(new_limit)
        if new_limit != self._current_limit:
            self._current_limit = new_limit
            self._wake_waiters()
        return self._current_limit

    async def adjust_limit(self, new_limit: int) -> int:
        """Adjust the semaphore limit. Async alias of :meth:`set_limit`; never blocks."""
        return self.set_limit(new_limit)

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()


class PerformanceMonitor:
    """Tracks performance metrics for adaptive concurrency control."""

    def __init__(self, config: ConcurrencyConfig):
        self.config = config
        self.operation_times: deque = deque(maxlen=config.window_size)
        self.operation_timestamps: deque = deque(maxlen=config.window_size)
        self.total_operations = 0
        self.successful_operations = 0
        self.failed_operations = 0
        self._lock = asyncio.Lock()

    async def record_operation(self, duration: float, success: bool = True):
        """Record an operation's timing and success status."""
        async with self._lock:
            current_time = time.time()
            self.operation_times.append(duration)
            self.operation_timestamps.append(current_time)
            self.total_operations += 1

            if success:
                self.successful_operations += 1
            else:
                self.failed_operations += 1

    async def get_metrics(self, current_concurrency: int) -> PerformanceMetrics:
        """Calculate current performance metrics."""
        async with self._lock:
            if not self.operation_times:
                return PerformanceMetrics(
                    throughput=0.0,
                    p95_latency=0.0,
                    p50_latency=0.0,
                    current_concurrency=current_concurrency,
                    total_operations=self.total_operations,
                    successful_operations=self.successful_operations,
                    failed_operations=self.failed_operations,
                )

            # Calculate throughput (operations per second)
            if len(self.operation_timestamps) >= 2:
                time_span = self.operation_timestamps[-1] - self.operation_timestamps[0]
                throughput = len(self.operation_times) / time_span if time_span > 0 else 0.0
            else:
                throughput = 0.0

            # Calculate latency percentiles
            sorted_times = sorted(self.operation_times)
            p50_latency = statistics.median(sorted_times)
            p95_latency = sorted_times[int(len(sorted_times) * 0.95)] if sorted_times else 0.0

            return PerformanceMetrics(
                throughput=throughput,
                p95_latency=p95_latency,
                p50_latency=p50_latency,
                current_concurrency=current_concurrency,
                total_operations=self.total_operations,
                successful_operations=self.successful_operations,
                failed_operations=self.failed_operations,
            )

    async def should_ramp_up(self) -> bool:
        """Check if we should ramp up concurrency."""
        if len(self.operation_times) < self.config.window_size:
            return False

        # Get recent metrics
        recent_times = list(self.operation_times)[-self.config.ramp_up_interval:]
        if len(recent_times) < self.config.ramp_up_interval:
            return False

        # Check if recent performance is good
        recent_p95 = statistics.quantiles(recent_times, n=20)[18]  # 95th percentile

        # More conservative: ramp up only if p95 latency is very reasonable (< 1.5 seconds)
        # and we have enough data points
        return recent_p95 < 1.5 and len(recent_times) >= 5

    async def should_ramp_down(self, current_metrics: PerformanceMetrics) -> bool:
        """Check if we should ramp down concurrency."""
        if len(self.operation_times) < self.config.window_size:
            return False

        # Get baseline metrics (first half of window)
        baseline_size = self.config.window_size // 2
        if len(self.operation_times) < baseline_size * 2:
            return False

        baseline_times = list(self.operation_times)[:baseline_size]
        recent_times = list(self.operation_times)[-baseline_size:]
        baseline_timestamps = list(self.operation_timestamps)[:baseline_size]
        recent_timestamps = list(self.operation_timestamps)[-baseline_size:]

        # Calculate baseline metrics
        baseline_p95 = statistics.quantiles(baseline_times, n=20)[18] if len(baseline_times) > 1 else 0
        baseline_span = (
            baseline_timestamps[-1] - baseline_timestamps[0]
            if len(baseline_timestamps) > 1
            else 0
        )
        baseline_throughput = (
            len(baseline_times) / baseline_span if baseline_span > 0 else 0
        )

        # Calculate recent metrics
        recent_p95 = statistics.quantiles(recent_times, n=20)[18] if len(recent_times) > 1 else 0
        recent_span = (
            recent_timestamps[-1] - recent_timestamps[0]
            if len(recent_timestamps) > 1
            else 0
        )
        recent_throughput = len(recent_times) / recent_span if recent_span > 0 else 0

        # Check for degradation
        latency_degradation = (recent_p95 - baseline_p95) / baseline_p95 if baseline_p95 > 0 else 0
        throughput_degradation = (baseline_throughput - recent_throughput) / baseline_throughput if baseline_throughput > 0 else 0

        # More conservative: ramp down only if BOTH metrics show significant degradation
        # OR if latency is extremely high (> 3 seconds)
        return ((latency_degradation > self.config.ramp_down_threshold and
                throughput_degradation > self.config.ramp_down_threshold) or
                recent_p95 > 3.0)


class ConcurrencyController:
    """Main controller for adaptive concurrency management."""

    def __init__(self, config: ConcurrencyConfig | None = None):
        self.config = config or ConcurrencyConfig()
        self.semaphore = AdaptiveSemaphore(self.config)
        self.monitor = PerformanceMonitor(self.config)
        self.operations_since_adjustment = 0
        self._lock = asyncio.Lock()
        # Optional hook invoked as ``on_adjust(old_limit, new_limit, reason)`` whenever the
        # concurrency limit actually changes. Lets callers narrate ramp-up/ramp-down without
        # the library importing any console/IO machinery.
        self.on_adjust: Callable[[int, int, str], None] | None = None

    async def acquire(self):
        """Acquire concurrency slot."""
        await self.semaphore.acquire()

    def release(self):
        """Release concurrency slot."""
        self.semaphore.release()

    async def record_operation(self, duration: float, success: bool = True):
        """Record an operation and potentially adjust concurrency."""
        await self.monitor.record_operation(duration, success)

        # Nothing awaited under this lock may block on work that itself needs the lock,
        # or on a concurrency slot. AdaptiveSemaphore.adjust_limit() is non-blocking for
        # exactly that reason; keep it that way.
        async with self._lock:
            self.operations_since_adjustment += 1

            # Check if we should adjust concurrency
            should_adjust = (
                self.operations_since_adjustment >= self.config.ramp_up_interval or
                await self.monitor.should_ramp_down(await self.monitor.get_metrics(self.semaphore.current_limit))
            )

            if should_adjust:
                await self._adjust_concurrency()
                self.operations_since_adjustment = 0

    async def _adjust_concurrency(self):
        """Adjust concurrency based on current performance."""
        current_limit = self.semaphore.current_limit
        metrics = await self.monitor.get_metrics(current_limit)

        # Check if we should ramp down
        if await self.monitor.should_ramp_down(metrics):
            new_limit = max(
                self.config.min_concurrency,
                current_limit - self.config.ramp_down_amount
            )
            await self.semaphore.adjust_limit(new_limit)
            self._notify_adjust(current_limit, new_limit, "latency/throughput degraded")
            return

        # Check if we should ramp up
        if (self.operations_since_adjustment >= self.config.ramp_up_interval and
            await self.monitor.should_ramp_up()):
            new_limit = min(
                self.config.max_concurrency,
                current_limit + self.config.ramp_up_amount
            )
            await self.semaphore.adjust_limit(new_limit)
            self._notify_adjust(current_limit, new_limit, "latency healthy")

    def _notify_adjust(self, old_limit: int, new_limit: int, reason: str) -> None:
        """Invoke the optional ``on_adjust`` hook when the limit actually changed."""
        if new_limit != old_limit and self.on_adjust is not None:
            self.on_adjust(old_limit, new_limit, reason)

    async def get_metrics(self) -> PerformanceMetrics:
        """Get current performance metrics."""
        return await self.monitor.get_metrics(self.semaphore.current_limit)


@dataclass
class AdaptiveDelayConfig:
    """Configuration for adaptive inter-operation delay control."""

    initial_delay: float = 1.0
    min_delay: float = 0.1
    max_delay: float = 3.0
    ramp_up_step: float = 0.1
    ramp_down_step: float = 0.5
    error_ramp_down_factor: float = 1.5
    window_size: int = 20
    min_samples_before_ramp: int = 5
    latency_good_threshold: float = 0.8
    latency_bad_threshold: float = 2.0


class AdaptiveDelayController:
    """
    Adaptive controller for inter-operation delays (e.g., between discovery pages).

    Tracks recent operation durations and error/rate-limit signals and adjusts the
    delay between operations to balance throughput and server safety.
    """

    def __init__(self, config: AdaptiveDelayConfig | None = None):
        self.config = config or AdaptiveDelayConfig()
        self._current_delay = float(self.config.initial_delay)
        self._durations: deque = deque(maxlen=self.config.window_size)
        self._statuses: deque = deque(maxlen=self.config.window_size)
        self._lock = asyncio.Lock()

    @property
    def current_delay(self) -> float:
        """Current delay in seconds before the next operation."""
        return float(self._current_delay)

    def get_delay(self) -> float:
        """Return the current delay in seconds."""
        return self.current_delay

    async def record_result(self, duration: float, status: str = "ok") -> None:
        """
        Record the result of an operation and adjust delay if needed.

        Args:
            duration: Time in seconds the operation took (excluding any pre-delay).
            status: One of "ok", "error", "rate_limited".
        """
        async with self._lock:
            self._durations.append(float(duration))
            self._statuses.append(status)

            if len(self._durations) < self.config.min_samples_before_ramp:
                return

            recent = list(self._durations)
            recent_sorted = sorted(recent)
            idx = int(len(recent_sorted) * 0.95)
            idx = min(max(idx, 0), len(recent_sorted) - 1)
            p95 = recent_sorted[idx] if recent_sorted else 0.0

            recent_statuses = list(self._statuses)[-3:]  # last few operations
            has_error = any(s in ("error", "rate_limited") for s in recent_statuses)
            has_rate_limited = any(s == "rate_limited" for s in recent_statuses)

            # Ramp down (increase delay) on explicit rate-limit or high latency
            if has_rate_limited or p95 > self.config.latency_bad_threshold:
                if has_rate_limited:
                    new_delay = self._current_delay * self.config.error_ramp_down_factor
                else:
                    new_delay = self._current_delay + self.config.ramp_down_step
                self._current_delay = max(
                    self.config.min_delay, min(self.config.max_delay, new_delay)
                )
                return

            # Ramp down on generic errors (but less aggressively)
            if has_error:
                new_delay = self._current_delay + self.config.ramp_down_step
                self._current_delay = max(
                    self.config.min_delay, min(self.config.max_delay, new_delay)
                )
                return

            # Ramp up (decrease delay) when latency is consistently good and no errors
            if p95 < self.config.latency_good_threshold:
                new_delay = self._current_delay - self.config.ramp_up_step
                self._current_delay = max(
                    self.config.min_delay, min(self.config.max_delay, new_delay)
                )
