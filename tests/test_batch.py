"""Tests for batch item creation."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from dspace_client.batch import BatchItemCreator
from dspace_client.concurrency import ConcurrencyConfig


def _mock_client(latency: float = 0.0) -> MagicMock:
    """A stand-in DSpace client.

    ``latency`` makes each call actually suspend, so concurrent workers genuinely overlap
    inside the semaphore. A bare AsyncMock resolves without yielding to the event loop,
    which would quietly serialise the batch and hide any concurrency bug.
    """

    async def _create_item(**_kwargs):
        if latency:
            await asyncio.sleep(latency)
        return {"uuid": "item-1"}

    async def _create_bundle(*_args, **_kwargs):
        if latency:
            await asyncio.sleep(latency)
        return {"uuid": "bundle-1"}

    client = MagicMock()
    client.create_item = AsyncMock(side_effect=_create_item)
    client.create_bundle = AsyncMock(side_effect=_create_bundle)
    return client


@pytest.mark.asyncio
async def test_batch_uses_shared_adaptive_semaphore():
    batch = BatchItemCreator(_mock_client(), config=ConcurrencyConfig(initial=2, max_concurrency=4))

    mock_semaphore = AsyncMock()
    mock_semaphore.__aenter__ = AsyncMock(return_value=mock_semaphore)
    mock_semaphore.__aexit__ = AsyncMock(return_value=None)
    batch.controller.semaphore = mock_semaphore

    results = await batch._execute_batch_with_concurrency(
        [
            ({"title": "Item 1"}, "collection-uuid"),
            ({"title": "Item 2"}, "collection-uuid"),
        ]
    )

    assert mock_semaphore.__aenter__.await_count == 2
    assert mock_semaphore.__aexit__.await_count == 2
    assert [r["success"] for r in results] == [True, True]


@pytest.mark.asyncio
async def test_batch_completes_when_concurrency_ramps_down():
    """Regression for the reported permanent hang on ramp-down.

    A batch large enough to trigger a concurrency *reduction* mid-flight used to stop
    issuing requests and never return: the ramp-down blocked waiting for a slot held by
    workers that were themselves blocked behind it.
    """
    batch = BatchItemCreator(
        _mock_client(latency=0.005),
        config=ConcurrencyConfig(
            initial=4, min_concurrency=1, max_concurrency=8, window_size=4, ramp_up_interval=2
        ),
    )

    # Force every adjustment check to demand a ramp-down.
    async def always_ramp_down(_metrics):
        return True

    batch.controller.monitor.should_ramp_down = always_ramp_down

    specs = [({"title": f"Item {i}"}, "collection-uuid") for i in range(20)]
    results = await asyncio.wait_for(batch._execute_batch_with_concurrency(specs), timeout=10.0)

    assert len(results) == 20
    assert all(r["success"] for r in results)
    assert batch.controller.semaphore.current_limit == 1


@pytest.mark.asyncio
async def test_batch_records_failures_without_stalling_the_batch():
    """A failing item must still release its slot and be recorded."""
    client = _mock_client()
    client.create_item = AsyncMock(side_effect=RuntimeError("boom"))

    batch = BatchItemCreator(client, config=ConcurrencyConfig(initial=2, max_concurrency=4))

    specs = [({"title": f"Item {i}"}, "collection-uuid") for i in range(6)]
    results = await asyncio.wait_for(batch._execute_batch_with_concurrency(specs), timeout=10.0)

    assert len(results) == 6
    assert all(not r["success"] and "boom" in r["error"] for r in results)
    assert batch.controller.semaphore.active == 0
