"""Tests for batch item creation."""

import asyncio
import itertools
from unittest.mock import AsyncMock, MagicMock

import pytest

from dspace_client.batch import BatchItemCreator
from dspace_client.concurrency import ConcurrencyConfig


def _mock_client(latency: float = 0.0) -> MagicMock:
    """A stand-in DSpace client.

    ``latency`` makes each call actually suspend, so concurrent workers genuinely overlap
    inside the semaphore. A bare AsyncMock resolves without yielding to the event loop,
    which would quietly serialise the batch and hide any concurrency bug.

    Returned objects carry back their inputs (item name, owning collection, filename) so
    tests can assert on what was actually sent rather than on opaque ids.
    """
    counter = itertools.count(1)

    async def _create_item(*, name, owning_collection_uuid, metadata=None):
        if latency:
            await asyncio.sleep(latency)
        return {
            "uuid": f"item-{next(counter)}",
            "name": name,
            "collection": owning_collection_uuid,
            "metadata": metadata,
        }

    async def _create_bundle(item_uuid, bundle_name):
        if latency:
            await asyncio.sleep(latency)
        return {"uuid": f"bundle-for-{item_uuid}", "name": bundle_name}

    async def _upload_bitstream(*, bundle_uuid, filename, content):
        if latency:
            await asyncio.sleep(latency)
        return {"uuid": f"bitstream-for-{bundle_uuid}", "name": filename, "size": len(content)}

    client = MagicMock()
    client.create_item = AsyncMock(side_effect=_create_item)
    client.create_bundle = AsyncMock(side_effect=_create_bundle)
    client.upload_bitstream = AsyncMock(side_effect=_upload_bitstream)
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


@pytest.mark.asyncio
async def test_create_items_batch_round_robins_collections():
    """Items are spread across the supplied collections in order."""
    batch = BatchItemCreator(_mock_client(), config=ConcurrencyConfig(initial=2, max_concurrency=4))

    items, bundles, bitstreams = await batch.create_items_batch(
        ["coll-a", "coll-b"],
        [{"title": f"Item {i}"} for i in range(5)],
    )

    assert [i["collection"] for i in items] == ["coll-a", "coll-b", "coll-a", "coll-b", "coll-a"]
    assert [i["name"] for i in items] == [f"Item {i}" for i in range(5)]
    assert len(bundles) == 5
    assert bitstreams == []


@pytest.mark.asyncio
async def test_create_items_batch_chunks_work_and_samples_metrics():
    """Work is chunked (20 at a time) and metrics are sampled once per chunk."""
    batch = BatchItemCreator(_mock_client(), config=ConcurrencyConfig(initial=4, max_concurrency=8))

    samples: list[tuple[int, int]] = []

    def on_sample(completed, total, _metrics):
        samples.append((completed, total))

    items, _bundles, _bitstreams = await batch.create_items_batch(
        ["coll-a"],
        [{"title": f"Item {i}"} for i in range(45)],
        on_metrics_sample=on_sample,
    )

    assert len(items) == 45
    assert samples == [(20, 45), (40, 45), (45, 45)]


@pytest.mark.asyncio
async def test_create_items_batch_awaits_async_metrics_callback():
    """``on_metrics_sample`` may be a coroutine function."""
    batch = BatchItemCreator(_mock_client(), config=ConcurrencyConfig(initial=2, max_concurrency=4))

    samples: list[int] = []

    async def on_sample(completed, _total, _metrics):
        await asyncio.sleep(0)
        samples.append(completed)

    await batch.create_items_batch(
        ["coll-a"],
        [{"title": f"Item {i}"} for i in range(3)],
        on_metrics_sample=on_sample,
    )

    assert samples == [3]


@pytest.mark.asyncio
async def test_create_items_batch_records_bitstreams_only_when_content_supplied():
    """An item without content still gets a bundle, but contributes no bitstream."""
    batch = BatchItemCreator(_mock_client(), config=ConcurrencyConfig(initial=2, max_concurrency=4))

    items, bundles, bitstreams = await batch.create_items_batch(
        ["coll-a"],
        [
            {"title": "No file"},
            {"title": "With file", "content": b"hello", "filename": "paper.pdf"},
        ],
    )

    assert len(items) == 2
    assert len(bundles) == 2
    assert [b["name"] for b in bitstreams] == ["paper.pdf"]
    assert batch.get_created_counts() == {"items": 2, "bundles": 2, "bitstreams": 1}


@pytest.mark.asyncio
async def test_create_items_batch_keeps_going_after_a_failed_item():
    """One failing item must not abort the run or be counted as created."""
    client = _mock_client()

    async def _create_item(*, name, owning_collection_uuid, metadata=None):
        if name == "Item 1":
            msg = "server said no"
            raise RuntimeError(msg)
        return {"uuid": name, "name": name, "collection": owning_collection_uuid}

    client.create_item = AsyncMock(side_effect=_create_item)
    batch = BatchItemCreator(client, config=ConcurrencyConfig(initial=2, max_concurrency=4))

    items, bundles, _bitstreams = await batch.create_items_batch(
        ["coll-a"],
        [{"title": f"Item {i}"} for i in range(3)],
    )

    assert [i["name"] for i in items] == ["Item 0", "Item 2"]
    assert len(bundles) == 2


@pytest.mark.asyncio
async def test_create_items_batch_does_not_accumulate_across_runs():
    """Results are reset per call, so a reused creator does not double-count."""
    batch = BatchItemCreator(_mock_client(), config=ConcurrencyConfig(initial=2, max_concurrency=4))

    await batch.create_items_batch(["coll-a"], [{"title": "First"}])
    items, bundles, _bitstreams = await batch.create_items_batch(
        ["coll-a"], [{"title": "Second"}]
    )

    assert [i["name"] for i in items] == ["Second"]
    assert len(bundles) == 1
    assert batch.get_created_counts() == {"items": 1, "bundles": 1, "bitstreams": 0}
