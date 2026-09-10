"""Tests for adaptive concurrency control."""

import asyncio
import time

import pytest

from dspace_client.concurrency import (
    AdaptiveDelayController,
    AdaptiveSemaphore,
    ConcurrencyConfig,
    ConcurrencyController,
    PerformanceMonitor,
)


@pytest.mark.asyncio
async def test_adaptive_semaphore_ramp_up_increases_available_permits():
    config = ConcurrencyConfig(initial=2, min_concurrency=1, max_concurrency=8)
    semaphore = AdaptiveSemaphore(config)

    acquired = []
    for _ in range(2):
        await semaphore.acquire()
        acquired.append(True)

    await semaphore.adjust_limit(4)

    async def try_acquire():
        await semaphore.acquire()
        return True

    results = await asyncio.gather(try_acquire(), try_acquire())
    assert all(results)

    for _ in acquired:
        semaphore.release()
    for _ in results:
        semaphore.release()


@pytest.mark.asyncio
async def test_adaptive_semaphore_context_manager():
    config = ConcurrencyConfig(initial=1, max_concurrency=4)
    semaphore = AdaptiveSemaphore(config)

    async with semaphore:
        assert semaphore.current_limit == 1


@pytest.mark.asyncio
async def test_throughput_uses_timestamps_not_durations():
    """Throughput must not be computed from latency deltas (which can be negative)."""
    config = ConcurrencyConfig(window_size=10)
    monitor = PerformanceMonitor(config)

    base_time = time.time()
    for idx in range(10):
        # Decreasing durations would produce a negative denominator with the old bug.
        await monitor.record_operation(0.5 - idx * 0.04, success=True)
        monitor.operation_timestamps[-1] = base_time + idx

    metrics = await monitor.get_metrics(current_concurrency=4)
    assert metrics.throughput > 0


def test_adaptive_delay_controller_has_no_context_manager():
    controller = AdaptiveDelayController()
    assert not hasattr(controller, "acquire")
    assert not hasattr(controller, "release")


@pytest.mark.asyncio
async def test_adaptive_semaphore_ramp_down_does_not_block_with_permits_outstanding():
    """Regression: shrinking the limit while every slot is held used to deadlock.

    ``adjust_limit`` awaited ``Semaphore.acquire()`` while holding its own lock, so a
    ramp-down with no slots free never returned and wedged the controller for good.
    """
    config = ConcurrencyConfig(initial=2, min_concurrency=1, max_concurrency=8)
    semaphore = AdaptiveSemaphore(config)

    await semaphore.acquire()
    await semaphore.acquire()
    assert semaphore.active == 2

    await asyncio.wait_for(semaphore.adjust_limit(1), timeout=1.0)
    assert semaphore.current_limit == 1

    # The controller must stay usable afterwards, in both directions.
    await asyncio.wait_for(semaphore.adjust_limit(4), timeout=1.0)
    assert semaphore.current_limit == 4

    semaphore.release()
    semaphore.release()


@pytest.mark.asyncio
async def test_adaptive_semaphore_ramp_down_lets_inflight_work_finish():
    """A ramp-down never cancels or waits on work already holding a slot."""
    config = ConcurrencyConfig(initial=3, min_concurrency=1, max_concurrency=8)
    semaphore = AdaptiveSemaphore(config)

    for _ in range(3):
        await semaphore.acquire()

    await semaphore.adjust_limit(1)
    assert semaphore.active == 3  # over the new limit, but still running

    # A newcomer must wait until active work drains below the new limit of 1.
    waiter = asyncio.create_task(semaphore.acquire())
    await asyncio.sleep(0)
    assert not waiter.done()

    semaphore.release()
    semaphore.release()
    await asyncio.sleep(0)
    assert not waiter.done()  # active == 1, still at the limit

    semaphore.release()
    await asyncio.wait_for(waiter, timeout=1.0)
    assert semaphore.active == 1
    semaphore.release()


@pytest.mark.asyncio
async def test_adaptive_semaphore_never_exceeds_current_limit():
    """Concurrent workers must respect the limit as it moves up and down."""
    config = ConcurrencyConfig(initial=2, min_concurrency=1, max_concurrency=6)
    semaphore = AdaptiveSemaphore(config)

    active = 0
    peak = 0

    async def worker():
        nonlocal active, peak
        async with semaphore:
            active += 1
            peak = max(peak, active)
            assert active <= semaphore.current_limit
            await asyncio.sleep(0.001)
            active -= 1

    async def churn():
        for limit in (4, 6, 3, 1, 5, 2):
            await semaphore.adjust_limit(limit)
            await asyncio.sleep(0.002)

    await asyncio.wait_for(
        asyncio.gather(*[worker() for _ in range(40)], churn()),
        timeout=10.0,
    )
    assert active == 0
    assert peak > 1  # actually ran concurrently


@pytest.mark.asyncio
async def test_adaptive_semaphore_cancelled_waiter_does_not_leak_a_slot():
    """A waiter cancelled while queued must not strand the slot it was owed."""
    config = ConcurrencyConfig(initial=1, min_concurrency=1, max_concurrency=4)
    semaphore = AdaptiveSemaphore(config)

    await semaphore.acquire()

    doomed = asyncio.create_task(semaphore.acquire())
    await asyncio.sleep(0)
    doomed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await doomed

    semaphore.release()
    await asyncio.wait_for(semaphore.acquire(), timeout=1.0)
    assert semaphore.active == 1
    semaphore.release()
    assert semaphore.active == 0


@pytest.mark.asyncio
async def test_controller_workers_complete_through_a_ramp_down():
    """Adjusting the limit from inside a held slot must not deadlock.

    ``record_operation`` can decide to ramp down, and callers may well invoke it while
    still holding a slot. With the old permit-shuffling semaphore that combination parked
    every worker forever with the event loop idle: the ramp-down waited for a slot that
    only a worker blocked behind it could free.
    """
    config = ConcurrencyConfig(
        initial=4, min_concurrency=1, max_concurrency=8, window_size=4, ramp_up_interval=2
    )
    controller = ConcurrencyController(config)

    async def always_ramp_down(_metrics):
        return True

    controller.monitor.should_ramp_down = always_ramp_down

    completed = 0

    async def worker():
        nonlocal completed
        # Deliberately records while the slot is still held - the shape that used to hang.
        async with controller.semaphore:
            start = time.time()
            await asyncio.sleep(0.001)
            await controller.record_operation(time.time() - start, success=True)
        completed += 1

    await asyncio.wait_for(asyncio.gather(*[worker() for _ in range(20)]), timeout=10.0)
    assert completed == 20
    assert controller.semaphore.current_limit == config.min_concurrency
