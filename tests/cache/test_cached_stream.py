"""Test @cached on an async generator producer.

The point of the shape is that one producer backs two reads: a stream
that yields items as they arrive and a buffered read of the same entry.
Anything that lets a truncated sequence become that entry is a
correctness bug, so the abandonment cases carry as much weight as the
happy path.
"""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import suppress

import pytest

from grelmicro import Grelmicro
from grelmicro.cache.cached import cached
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.cache.serializers import JsonSerializer, PickleSerializer
from grelmicro.cache.ttl import TTLCache
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import MemoryLockAdapter

pytestmark = [pytest.mark.timeout(10)]

EXPECTED_ITEMS = [0, 1, 2]
EXPECTED_CALLS_1 = 1
EXPECTED_CALLS_2 = 2
EXPECTED_PARTIAL = 2


def _make_cache(ttl: float = 60) -> TTLCache:
    """Create a TTLCache on the in-memory backend."""
    backend = MemoryCacheAdapter()
    with suppress(RuntimeError):
        backend._loop = asyncio.get_running_loop()
    return TTLCache(ttl=ttl, backend=backend, serializer=PickleSerializer())


class TestStreamAndReplay:
    """A producer runs once, then its items are replayed."""

    async def test_streams_live_then_replays(self) -> None:
        """The miss streams from the producer, the hit from the entry."""
        # Arrange
        cache = _make_cache()
        calls = 0

        @cached(cache, key="s:{n}")
        async def produce(n: int) -> AsyncIterator[int]:
            nonlocal calls
            calls += 1
            for item in range(n):
                yield item

        # Act
        miss = [item async for item in produce(3)]
        hit = [item async for item in produce(3)]
        # Assert
        assert miss == EXPECTED_ITEMS
        assert hit == EXPECTED_ITEMS
        assert calls == EXPECTED_CALLS_1

    async def test_items_arrive_before_the_producer_finishes(self) -> None:
        """A miss must stream, not buffer and hand over at the end."""
        # Arrange
        cache = _make_cache()
        released = asyncio.Event()

        @cached(cache, key="s")
        async def produce() -> AsyncIterator[int]:
            yield 0
            await released.wait()
            yield 1

        # Act
        stream = produce()
        first = await anext(stream)
        # Assert
        assert first == 0
        # Cleanup
        released.set()
        assert [item async for item in stream] == [1]

    async def test_the_key_holds_a_plain_list(self) -> None:
        """The stored form is the sequence, so anything can read it."""
        # Arrange
        cache = _make_cache()

        @cached(cache, key="s")
        async def produce() -> AsyncIterator[int]:
            for item in range(3):
                yield item

        # Act
        [item async for item in produce()]
        # Assert
        assert await cache.get("s") == EXPECTED_ITEMS


class TestCollect:
    """The buffered read and the stream share one entry."""

    async def test_collect_serves_what_the_stream_stored(self) -> None:
        """Streaming first, collecting second, one execution."""
        # Arrange
        cache = _make_cache()
        calls = 0

        @cached(cache, key="s")
        async def produce() -> AsyncIterator[int]:
            nonlocal calls
            calls += 1
            for item in range(3):
                yield item

        # Act
        streamed = [item async for item in produce()]
        collected = await produce.collect()
        # Assert
        assert streamed == collected == EXPECTED_ITEMS
        assert calls == EXPECTED_CALLS_1

    async def test_the_stream_serves_what_collect_stored(self) -> None:
        """Collecting first, streaming second, still one execution."""
        # Arrange
        cache = _make_cache()
        calls = 0

        @cached(cache, key="s")
        async def produce() -> AsyncIterator[int]:
            nonlocal calls
            calls += 1
            for item in range(3):
                yield item

        # Act
        collected = await produce.collect()
        streamed = [item async for item in produce()]
        # Assert
        assert collected == streamed == EXPECTED_ITEMS
        assert calls == EXPECTED_CALLS_1

    async def test_refresh_replaces_a_live_entry(self) -> None:
        """A caller asking for fresh items gets the producer, not the entry."""
        # Arrange
        cache = _make_cache()
        calls = 0

        @cached(cache, key="s")
        async def produce() -> AsyncIterator[int]:
            nonlocal calls
            calls += 1
            for item in range(3):
                yield item

        # Act
        await produce.collect()
        refreshed = await produce.refresh()
        # Assert
        assert refreshed == EXPECTED_ITEMS
        assert calls == EXPECTED_CALLS_2

    async def test_cache_helpers_are_attached(self) -> None:
        """The producer carries the same helpers as any cached function."""
        # Arrange
        cache = _make_cache()

        @cached(cache, key="s")
        async def produce() -> AsyncIterator[int]:
            yield 0

        # Act
        [item async for item in produce()]
        info = produce.cache_info()
        await produce.cache_clear()
        # Assert
        assert info.misses == EXPECTED_CALLS_1
        assert await cache.get("s") is None


class TestPartialStreams:
    """A truncated sequence must never become the entry."""

    async def test_an_abandoned_stream_stores_nothing(self) -> None:
        """A reader that stops early leaves the key untouched."""
        # Arrange
        cache = _make_cache()

        @cached(cache, key="s")
        async def produce() -> AsyncIterator[int]:
            for item in range(5):
                yield item

        # Act
        stream = produce()
        partial = [await anext(stream), await anext(stream)]
        await stream.aclose()
        # Assert
        assert partial == [0, 1]
        assert await cache.get("s") is None

    async def test_the_next_reader_gets_the_whole_sequence(self) -> None:
        """An abandoned stream must not truncate what anyone reads later."""
        # Arrange
        cache = _make_cache()

        @cached(cache, key="s")
        async def produce() -> AsyncIterator[int]:
            for item in range(5):
                yield item

        # Act
        stream = produce()
        await anext(stream)
        await stream.aclose()
        # Assert
        assert [item async for item in produce()] == [0, 1, 2, 3, 4]

    async def test_a_producer_failing_part_way_stores_nothing(self) -> None:
        """The error propagates and the partial sequence is discarded."""
        # Arrange
        cache = _make_cache()

        @cached(cache, key="s")
        async def produce() -> AsyncIterator[int]:
            yield 0
            yield 1
            msg = "upstream died"
            raise RuntimeError(msg)

        # Act
        stream = produce()
        seen = [await anext(stream), await anext(stream)]
        # Assert
        with pytest.raises(RuntimeError, match="upstream died"):
            await anext(stream)
        assert seen == [0, 1]
        assert await cache.get("s") is None

    async def test_an_abandoned_collect_stores_nothing(self) -> None:
        """Cancelling the buffered read leaves the key untouched too."""
        # Arrange
        cache = _make_cache()
        started = asyncio.Event()

        @cached(cache, key="s")
        async def produce() -> AsyncIterator[int]:
            started.set()
            yield 0
            await asyncio.sleep(10)
            yield 1

        # Act
        task = asyncio.create_task(produce.collect())
        await started.wait()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        # Assert
        assert await cache.get("s") is None


class TestStampede:
    """Concurrent misses fold onto one producer."""

    async def test_concurrent_streams_run_the_producer_once(self) -> None:
        """The second reader waits, then replays the stored entry."""
        # Arrange
        cache = _make_cache()
        calls = 0
        released = asyncio.Event()

        @cached(cache, key="s")
        async def produce() -> AsyncIterator[int]:
            nonlocal calls
            calls += 1
            await released.wait()
            for item in range(3):
                yield item

        async def read() -> list[int]:
            return [item async for item in produce()]

        # Act
        first = asyncio.create_task(read())
        second = asyncio.create_task(read())
        await asyncio.sleep(0.02)
        released.set()
        # Assert
        assert await first == EXPECTED_ITEMS
        assert await second == EXPECTED_ITEMS
        assert calls == EXPECTED_CALLS_1

    async def test_a_stream_and_a_collect_fold_together(self) -> None:
        """The two reads share the lock, not only the key."""
        # Arrange
        cache = _make_cache()
        calls = 0
        released = asyncio.Event()

        @cached(cache, key="s")
        async def produce() -> AsyncIterator[int]:
            nonlocal calls
            calls += 1
            await released.wait()
            for item in range(3):
                yield item

        async def read() -> list[int]:
            return [item async for item in produce()]

        # Act
        streaming = asyncio.create_task(read())
        buffered = asyncio.create_task(produce.collect())
        await asyncio.sleep(0.02)
        released.set()
        # Assert
        assert await streaming == EXPECTED_ITEMS
        assert await buffered == EXPECTED_ITEMS
        assert calls == EXPECTED_CALLS_1

    async def test_lock_false_lets_both_run(self) -> None:
        """Opting out of folding is still available to a streaming producer."""
        # Arrange
        cache = _make_cache()
        calls = 0
        released = asyncio.Event()

        @cached(cache, key="s", lock=False)
        async def produce() -> AsyncIterator[int]:
            nonlocal calls
            calls += 1
            await released.wait()
            for item in range(3):
                yield item

        async def read() -> list[int]:
            return [item async for item in produce()]

        # Act
        first = asyncio.create_task(read())
        second = asyncio.create_task(read())
        await asyncio.sleep(0.02)
        released.set()
        await first
        await second
        # Assert
        assert calls == EXPECTED_CALLS_2

    async def test_two_replicas_fold_through_the_lock_backend(self) -> None:
        """lock=True serializes streaming misses across replicas."""
        # Arrange
        loop = asyncio.get_running_loop()
        backend = MemoryCacheAdapter()
        backend._loop = loop
        cache = TTLCache(backend=backend, serializer=PickleSerializer())
        micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])
        calls = 0
        released = asyncio.Event()

        async def impl() -> AsyncIterator[int]:
            nonlocal calls
            calls += 1
            await released.wait()
            for item in range(3):
                yield item

        replica_a = cached(cache, key="s", lock=True)(impl)
        replica_b = cached(cache, key="s", lock=True)(impl)

        # Act
        async with micro:

            async def read(replica) -> list[int]:  # noqa: ANN001
                return [item async for item in replica()]

            first = asyncio.create_task(read(replica_a))
            await asyncio.sleep(0.02)
            second = asyncio.create_task(read(replica_b))
            await asyncio.sleep(0.02)
            released.set()
            items_a = await first
            items_b = await second
        # Assert
        assert items_a == EXPECTED_ITEMS
        assert items_b == EXPECTED_ITEMS
        assert calls == EXPECTED_CALLS_1

    async def test_a_replica_arriving_after_the_store_replays(self) -> None:
        """The double-check inside the distributed lock serves the entry."""
        # Arrange
        loop = asyncio.get_running_loop()
        backend = MemoryCacheAdapter()
        backend._loop = loop
        cache = TTLCache(backend=backend, serializer=PickleSerializer())
        micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])
        calls = 0

        async def impl() -> AsyncIterator[int]:
            nonlocal calls
            calls += 1
            for item in range(3):
                yield item

        replica_a = cached(cache, key="s", lock=True)(impl)
        replica_b = cached(cache, key="s", lock=True)(impl)

        # Act
        async with micro:
            first = [item async for item in replica_a()]
            second = [item async for item in replica_b()]
        # Assert
        assert first == second == EXPECTED_ITEMS
        assert calls == EXPECTED_CALLS_1


class TestSkipAndTags:
    """The predicate and the tags see the assembled sequence."""

    async def test_skip_receives_the_whole_sequence(self) -> None:
        """A producer that yielded nothing worth keeping is not stored."""
        # Arrange
        cache = _make_cache()

        @cached(cache, key="s", skip=lambda items: not items)
        async def produce() -> AsyncIterator[int]:
            for item in ():
                yield item

        # Act
        streamed = [item async for item in produce()]
        # Assert
        assert streamed == []
        assert await cache.get("s") is None

    async def test_tags_invalidate_a_stored_sequence(self) -> None:
        """Tag invalidation reaches a streamed entry like any other."""
        # Arrange
        cache = _make_cache()

        @cached(cache, key="s:{n}", tags=["seq", "seq:{n}"])
        async def produce(n: int) -> AsyncIterator[int]:
            for item in range(n):
                yield item

        # Act
        [item async for item in produce(3)]
        await cache.delete_tags("seq:3")
        # Assert
        assert await cache.get("s:3") is None


class TestStaleOnError:
    """A reserve stands in only where it can stand in cleanly."""

    async def test_serves_stale_when_the_producer_fails_at_the_start(
        self,
    ) -> None:
        """Nothing was yielded, so the reserve replays without a seam."""
        # Arrange
        cache = _make_cache(ttl=0.05)
        fail = False

        @cached(cache, key="s", stale_ttl=60)
        async def produce() -> AsyncIterator[int]:
            if fail:
                msg = "upstream down"
                raise RuntimeError(msg)
            for item in range(3):
                yield item

        # Act
        [item async for item in produce()]
        await asyncio.sleep(0.1)
        fail = True
        # Assert
        assert [item async for item in produce()] == EXPECTED_ITEMS

    async def test_propagates_when_the_producer_fails_part_way(self) -> None:
        """The caller already holds live items, so a replay would repeat."""
        # Arrange
        cache = _make_cache(ttl=0.05)
        fail = False

        @cached(cache, key="s", stale_ttl=60)
        async def produce() -> AsyncIterator[int]:
            yield 0
            yield 1
            if fail:
                msg = "upstream died"
                raise RuntimeError(msg)
            yield 2

        # Act
        [item async for item in produce()]
        await asyncio.sleep(0.1)
        fail = True
        stream = produce()
        seen = [await anext(stream), await anext(stream)]
        # Assert
        with pytest.raises(RuntimeError, match="upstream died"):
            await anext(stream)
        assert seen == [0, 1]

    async def test_propagates_when_there_is_no_reserve(self) -> None:
        """A first call that fails has nothing to fall back to."""
        # Arrange
        cache = _make_cache()

        @cached(cache, key="s", stale_ttl=60)
        async def produce() -> AsyncIterator[int]:
            msg = "upstream down"
            raise RuntimeError(msg)
            yield 0

        # Act / Assert
        with pytest.raises(RuntimeError, match="upstream down"):
            [item async for item in produce()]

    async def test_cancellation_is_not_an_upstream_failure(self) -> None:
        """A cancelled reader must not be answered with the reserve.

        `CancelledError` derives from `BaseException`, so the stale
        handler cannot catch it. Widening that clause would turn
        cancellation into a stale serve and swallow the cancel.
        """
        # Arrange
        cache = _make_cache(ttl=0.05)
        hang = False

        @cached(cache, key="s", stale_ttl=60)
        async def produce() -> AsyncIterator[int]:
            if hang:
                await asyncio.sleep(10)
            for item in range(3):
                yield item

        async def read() -> list[int]:
            return [item async for item in produce()]

        # Act
        [item async for item in produce()]
        await asyncio.sleep(0.1)
        hang = True
        task = asyncio.create_task(read())
        await asyncio.sleep(0.02)
        task.cancel()
        # Assert
        with pytest.raises(asyncio.CancelledError):
            await task


class _Clock:
    """Mutable wall clock standing in for ``cached._now``."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


class TestEarlyRefresh:
    """The background recompute drains the producer with no reader."""

    async def test_a_hit_in_the_early_window_refreshes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refresh stores a new sequence without anyone streaming it."""
        # Arrange
        cached_mod = sys.modules["grelmicro.cache.cached"]
        clock = _Clock()
        monkeypatch.setattr(cached_mod, "_now", clock)
        monkeypatch.setattr(
            cached_mod, "_xfetch_should_refresh", lambda *_: True
        )
        cache = _make_cache(ttl=60)
        calls = 0

        @cached(cache, key="s", early=0.9)
        async def produce() -> AsyncIterator[int]:
            nonlocal calls
            calls += 1
            for item in range(3):
                yield item

        # Act
        [item async for item in produce()]
        clock.t = 1055  # remaining 5s, inside the 54s early window
        [item async for item in produce()]
        await asyncio.sleep(0.05)
        # Assert
        assert calls == EXPECTED_CALLS_2
        assert await cache.get("s") == EXPECTED_ITEMS


class TestSyncGeneratorIsRefused:
    """A sync generator would replay as empty, so it raises."""

    def test_decorating_a_sync_generator_raises(self) -> None:
        """The error names the two ways out."""
        # Arrange
        cache = TTLCache(
            backend=MemoryCacheAdapter(), serializer=JsonSerializer()
        )

        # Act / Assert
        with pytest.raises(TypeError, match="does not support the sync"):

            @cached(cache, key="s")
            def produce():  # noqa: ANN202
                yield 0
