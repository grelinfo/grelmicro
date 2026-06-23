"""Mutation-killing tests for the `@cached` decorator.

These pin behavior the broader suite left under-asserted: private-cache
config from `ttl=`/`maxsize=`, the `typed` and `key_maker` plumbing through
the wrapper builders, the early-refresh path actually storing a new value (not
just rerunning the function), the recompute-duration sign, the sync per-key
lock budget, and the distributed compute path's kwargs, skip, and tag
rendering. Values use a nonzero clock and non-unit TTLs so operator and sign
mutants diverge.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections import OrderedDict
from contextlib import suppress

import pytest

from grelmicro import Grelmicro
from grelmicro.cache.cached import _make_key, _read_meta, cached
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.cache.serializers import PickleSerializer
from grelmicro.cache.ttl import TTLCache
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import MemoryLockAdapter

pytestmark = [pytest.mark.timeout(10)]

_PRIVATE_TTL = 45.0
_PRIVATE_MAXSIZE = 7


def _make_cache(maxsize: int = 10, ttl: float = 60) -> TTLCache:
    """Create a TTLCache on a primed in-memory backend."""
    backend = MemoryCacheAdapter()
    with suppress(RuntimeError):
        backend._loop = asyncio.get_running_loop()
    return TTLCache(
        maxsize=maxsize, ttl=ttl, backend=backend, serializer=PickleSerializer()
    )


def _shared_cache(loop: asyncio.AbstractEventLoop) -> TTLCache:
    """Build a TTLCache on a backend whose loop is already primed."""
    backend = MemoryCacheAdapter()
    backend._loop = loop
    return TTLCache(backend=backend, serializer=PickleSerializer())


class _Clock:
    """Mutable wall clock standing in for ``cached._now``."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


def _private_cache(wrapper: object) -> TTLCache:
    """Return the private TTLCache the decorator bound to a wrapper.

    The wrapper's `cache_info` is the cache's bound method, so its
    `__self__` is the cache instance.
    """
    cache = wrapper.cache_info.__self__  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
    assert isinstance(cache, TTLCache)
    return cache


class TestPrivateCacheConfig:
    """Pin the private cache built from `ttl=`/`maxsize=`."""

    async def test_private_cache_uses_the_given_ttl(self) -> None:
        """`@cached(ttl=45)` builds a private cache with config.ttl == 45."""

        @cached(ttl=_PRIVATE_TTL)
        async def fetch(x: int) -> int:
            return x

        assert _private_cache(fetch).config.ttl == _PRIVATE_TTL

    async def test_private_cache_uses_the_given_maxsize(self) -> None:
        """`@cached(ttl=..., maxsize=7)` carries maxsize into the cache."""

        @cached(ttl=_PRIVATE_TTL, maxsize=_PRIVATE_MAXSIZE)
        async def fetch(x: int) -> int:
            return x

        assert _private_cache(fetch).config.maxsize == _PRIVATE_MAXSIZE

    async def test_private_cache_default_maxsize_is_zero(self) -> None:
        """The default `maxsize` is 0 (unlimited), not 1."""

        @cached(ttl=_PRIVATE_TTL)
        async def fetch(x: int) -> int:
            return x

        assert _private_cache(fetch).config.maxsize == 0


class _SameReprA:
    """Distinct type that shares a repr with `_SameReprB`."""

    def __repr__(self) -> str:
        return "X"


class _SameReprB:
    """Distinct type that shares a repr with `_SameReprA`."""

    def __repr__(self) -> str:
        return "X"


class TestTypedPlumbing:
    """Pin that `typed` reaches the key builder for async and sync wrappers.

    Same-repr-different-type values are used so an untyped key (the result
    of dropping `typed` or flipping it to a falsy value) collides while a
    typed key separates them.
    """

    async def test_typed_true_separates_same_repr_async(self) -> None:
        """`@cached(typed=True)` on an async func separates same-repr types."""
        cache = _make_cache()
        calls = 0

        @cached(cache, typed=True)
        async def fetch(x: object) -> str:
            nonlocal calls
            calls += 1
            return type(x).__name__

        await fetch(_SameReprA())
        await fetch(_SameReprB())
        assert calls == 2  # noqa: PLR2004

    async def test_typed_true_separates_same_repr_sync(self) -> None:
        """`@cached(typed=True)` on a sync func separates same-repr types."""
        cache = _make_cache()
        calls = 0

        @cached(cache, typed=True)
        def fetch(x: object) -> str:
            nonlocal calls
            calls += 1
            return type(x).__name__

        await asyncio.to_thread(lambda: fetch(_SameReprA()))
        await asyncio.to_thread(lambda: fetch(_SameReprB()))
        assert calls == 2  # noqa: PLR2004

    async def test_typed_default_false_merges_same_repr_async(self) -> None:
        """The default `typed=False` merges same-repr types (async)."""
        cache = _make_cache()
        calls = 0

        @cached(cache)
        async def fetch(x: object) -> str:
            nonlocal calls
            calls += 1
            return type(x).__name__

        await fetch(_SameReprA())
        await fetch(_SameReprB())
        assert calls == 1


class TestKeyMakerPlumbing:
    """Pin that a custom key_maker reaches the async and sync wrappers."""

    async def test_custom_key_maker_collapses_entries_async(self) -> None:
        """A fixed-key key_maker folds all async calls to one entry."""
        cache = _make_cache()
        calls = 0

        @cached(cache, key_maker=lambda _func, _args, _kwargs: "fixed")
        async def fetch(x: int) -> int:
            nonlocal calls
            calls += 1
            return x

        first = await fetch(1)
        second = await fetch(2)  # different arg, same fixed key
        assert first == second == 1
        assert calls == 1

    async def test_custom_key_maker_collapses_entries_sync(self) -> None:
        """A fixed-key key_maker folds all sync calls to one entry."""
        cache = _make_cache()
        calls = 0

        @cached(cache, key_maker=lambda _func, _args, _kwargs: "fixed")
        def fetch(x: int) -> int:
            nonlocal calls
            calls += 1
            return x

        first = await asyncio.to_thread(lambda: fetch(1))
        second = await asyncio.to_thread(lambda: fetch(2))
        assert first == second == 1
        assert calls == 1

    async def test_key_maker_receives_function_args_and_kwargs(self) -> None:
        """The key_maker is called with `(func, args, kwargs)` intact.

        Asserting the kwargs reach the key_maker catches a mutation that
        passes `None` in place of the kwargs dict.
        """
        cache = _make_cache()
        seen: list[tuple[object, tuple, dict]] = []

        def key_maker(func, args, kwargs) -> str:  # noqa: ANN001
            seen.append((func, args, kwargs))
            return f"k:{args[0]}:{kwargs}"

        @cached(cache, key_maker=key_maker)
        async def fetch(x: int, *, label: str) -> int:  # noqa: ARG001
            return x

        await fetch(1, label="hot")
        assert seen
        func, args, kwargs = seen[0]
        assert getattr(func, "__name__", None) == "fetch"
        assert args == (1,)
        assert kwargs == {"label": "hot"}

    async def test_default_key_includes_function_identity(self) -> None:
        """Two functions with the same args do not share a default key."""
        cache = _make_cache()

        @cached(cache)
        async def fetch_a(x: int) -> str:  # noqa: ARG001
            return "a"

        @cached(cache)
        async def fetch_b(x: int) -> str:  # noqa: ARG001
            return "b"

        assert await fetch_a(1) == "a"
        assert await fetch_b(1) == "b"


class TestSyncLockBudget:
    """Pin the sync per-key lock eviction budget and trim-in-one-call."""

    def test_sync_eviction_keeps_exactly_the_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dict at exactly the budget is not trimmed (`>` not `>=`)."""
        cached_mod = sys.modules["grelmicro.cache.cached"]
        monkeypatch.setattr(cached_mod, "_PER_KEY_LOCK_BUDGET", 3)
        locks: OrderedDict[str, threading.Lock] = OrderedDict(
            (f"k{i}", threading.Lock()) for i in range(3)
        )

        cached_mod._evict_idle_locks_sync(locks)

        assert len(locks) == 3  # noqa: PLR2004

    def test_sync_eviction_trims_oversize_in_one_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One call trims a far-oversize dict down to the budget."""
        cached_mod = sys.modules["grelmicro.cache.cached"]
        monkeypatch.setattr(cached_mod, "_PER_KEY_LOCK_BUDGET", 3)
        locks: OrderedDict[str, threading.Lock] = OrderedDict(
            (f"k{i}", threading.Lock()) for i in range(10)
        )

        cached_mod._evict_idle_locks_sync(locks)

        assert len(locks) == 3  # noqa: PLR2004
        assert list(locks) == ["k7", "k8", "k9"]


class TestEarlyRefreshStoresValue:
    """Pin that the early refresh actually updates the cached value."""

    async def test_async_refresh_stores_the_new_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A background refresh writes the recomputed value into the cache.

        The function returns an incrementing counter, so after the refresh
        a fresh read (outside the early window) must see the second value.
        A refresh that runs the function but fails to store would leave the
        first value in place.
        """
        cached_mod = sys.modules["grelmicro.cache.cached"]
        clock = _Clock()
        monkeypatch.setattr(cached_mod, "_now", clock)
        monkeypatch.setattr(
            cached_mod, "_xfetch_should_refresh", lambda *_: True
        )
        cache = _make_cache(ttl=60)
        counter = 0

        async def impl(x: int) -> int:  # noqa: ARG001
            nonlocal counter
            counter += 1
            return counter

        fetch = cached(cache, early=0.5)(impl)

        assert await fetch(5) == 1  # miss at t=1000, stores 1
        clock.t = 1040  # inside the early window (remaining 20 <= 30)
        await fetch(5)  # hit, schedules a background refresh storing 2

        for _ in range(100):
            await asyncio.sleep(0.005)
            key = _make_key(impl, (5,), {}, None, typed=False)
            stored = await cache._peek(key)
            if stored == 2:  # noqa: PLR2004
                break
        assert stored == 2  # noqa: PLR2004

    async def test_refresh_records_a_small_recompute_delta(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stored XFetch delta is the elapsed time, not a sum.

        `perf_counter() - started` is a tiny positive duration. A `+ started`
        mutation yields a value in the thousands, so a sub-second bound
        catches it.
        """
        cached_mod = sys.modules["grelmicro.cache.cached"]
        clock = _Clock()
        monkeypatch.setattr(cached_mod, "_now", clock)
        cache = _make_cache(ttl=60)

        async def impl(x: int) -> int:
            return x * 2

        fetch = cached(cache, early=0.5)(impl)
        await fetch(5)  # cold miss writes meta with the recompute delta

        key = _make_key(impl, (5,), {}, None, typed=False)
        meta = await _read_meta(cache, key)
        assert meta is not None
        _written, delta = meta
        assert 0 <= delta < 1.0


class TestDistributedComputePath:
    """Pin the sync distributed orchestrate path's kwargs, skip, and tags.

    The cross-replica `_distributed_orchestrate` runs only for the sync
    wrapper, so these decorate sync functions and drive them from a worker
    thread under an app with a lock backend.
    """

    async def test_distributed_compute_passes_kwargs(self) -> None:
        """The cross-replica compute forwards keyword arguments."""
        loop = asyncio.get_running_loop()
        cache = _shared_cache(loop)
        micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])

        def impl(x: int, *, factor: int) -> int:
            return x * factor

        fetch = cached(cache, lock=True)(impl)
        async with micro:
            result = await asyncio.to_thread(lambda: fetch(5, factor=3))
        assert result == 15  # noqa: PLR2004

    async def test_distributed_compute_renders_tags_from_args(self) -> None:
        """The distributed path tags the entry from the call arguments."""
        loop = asyncio.get_running_loop()
        cache = _shared_cache(loop)
        backend = cache._backend
        assert isinstance(backend, MemoryCacheAdapter)
        micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])

        def impl(user_id: int) -> int:
            return user_id

        fetch = cached(cache, lock=True, tags=["user:{user_id}"])(impl)
        async with micro:
            await asyncio.to_thread(lambda: fetch(42))
            members = backend._tag_keys.get("user:42")
        assert members is not None
        assert len(members) == 1

    async def test_distributed_compute_skip_passes_the_result(self) -> None:
        """The skip predicate sees the real result in the distributed path."""
        loop = asyncio.get_running_loop()
        cache = _shared_cache(loop)
        micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])
        calls = 0

        def impl(x: int) -> int:
            nonlocal calls
            calls += 1
            return x * 2

        # skip when the result equals 10, so the value-5 call is never cached.
        fetch = cached(cache, lock=True, skip=lambda result: result == 10)(impl)  # noqa: PLR2004
        async with micro:
            assert await asyncio.to_thread(lambda: fetch(5)) == 10  # noqa: PLR2004
            assert await asyncio.to_thread(lambda: fetch(5)) == 10  # noqa: PLR2004
        assert calls == 2  # noqa: PLR2004


class TestLockDefault:
    """Pin that `lock` defaults to `"local"` (in-process folding)."""

    async def test_default_lock_folds_concurrent_misses(self) -> None:
        """With no `lock` argument, concurrent misses fold to one call.

        The default is `"local"`, an in-process lock that coalesces a
        burst of misses within the worker, so the count is one.
        """
        cache = _make_cache()
        calls = 0
        barrier = asyncio.Event()

        @cached(cache)
        async def fetch(x: int) -> int:
            nonlocal calls
            calls += 1
            await barrier.wait()
            return x * 2

        task_a = asyncio.create_task(fetch(5))
        task_b = asyncio.create_task(fetch(5))
        await asyncio.sleep(0.05)
        barrier.set()
        await task_a
        await task_b

        assert calls == 1

    async def test_lock_false_does_not_fold_concurrent_misses(self) -> None:
        """`lock=False` opts out: every concurrent miss runs the function."""
        cache = _make_cache()
        calls = 0
        barrier = asyncio.Event()

        @cached(cache, lock=False)
        async def fetch(x: int) -> int:
            nonlocal calls
            calls += 1
            await barrier.wait()
            return x * 2

        task_a = asyncio.create_task(fetch(5))
        task_b = asyncio.create_task(fetch(5))
        await asyncio.sleep(0.05)
        barrier.set()
        await task_a
        await task_b

        assert calls == 2  # noqa: PLR2004


class TestSyncEarlyRefresh:
    """Pin the sync early-refresh value store and recompute-delta sign."""

    async def test_sync_refresh_stores_the_new_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sync background refresh writes the recomputed value."""
        cached_mod = sys.modules["grelmicro.cache.cached"]
        clock = _Clock()
        monkeypatch.setattr(cached_mod, "_now", clock)
        monkeypatch.setattr(
            cached_mod, "_xfetch_should_refresh", lambda *_: True
        )
        cache = _make_cache(ttl=60)
        counter = 0

        def impl(x: int) -> int:  # noqa: ARG001
            nonlocal counter
            counter += 1
            return counter

        fetch = cached(cache, early=0.5)(impl)

        assert await asyncio.to_thread(lambda: fetch(5)) == 1  # miss, stores 1
        clock.t = 1040  # inside the early window
        await asyncio.to_thread(lambda: fetch(5))  # hit, refresh stores 2

        key = _make_key(impl, (5,), {}, None, typed=False)
        stored = None
        for _ in range(100):
            await asyncio.sleep(0.005)
            stored = await cache._peek(key)
            if stored == 2:  # noqa: PLR2004
                break
        assert stored == 2  # noqa: PLR2004

    async def test_sync_cold_miss_records_small_delta(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sync cold miss with early= stores a sub-second recompute delta.

        Pins the `perf_counter() - started` sign in the sync compute path.
        """
        cached_mod = sys.modules["grelmicro.cache.cached"]
        clock = _Clock()
        monkeypatch.setattr(cached_mod, "_now", clock)
        cache = _make_cache(ttl=60)

        def impl(x: int) -> int:
            return x * 2

        fetch = cached(cache, early=0.5)(impl)
        await asyncio.to_thread(lambda: fetch(5))

        key = _make_key(impl, (5,), {}, None, typed=False)
        meta = await _read_meta(cache, key)
        assert meta is not None
        _written, delta = meta
        assert 0 <= delta < 1.0


class TestEarlyRefreshRewritesMeta:
    """Pin that the async refresh re-arms XFetch metadata (early= reaches it)."""

    async def test_refresh_rewrites_meta_at_the_new_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After a refresh, the stored meta carries the refresh timestamp.

        The refresh calls the compute helper with `early=` so it rewrites
        the XFetch sidecar. A mutation dropping `early` would leave the
        meta at the original cold-miss timestamp.
        """
        cached_mod = sys.modules["grelmicro.cache.cached"]
        clock = _Clock()
        monkeypatch.setattr(cached_mod, "_now", clock)
        monkeypatch.setattr(
            cached_mod, "_xfetch_should_refresh", lambda *_: True
        )
        cache = _make_cache(ttl=60)

        async def impl(x: int) -> int:
            return x * 2

        fetch = cached(cache, early=0.5)(impl)
        await fetch(5)  # cold miss at t=1000, meta written at 1000
        clock.t = 1040  # inside the early window
        await fetch(5)  # hit, schedules refresh writing meta at 1040

        key = _make_key(impl, (5,), {}, None, typed=False)
        written = 1000.0
        for _ in range(100):
            await asyncio.sleep(0.005)
            meta = await _read_meta(cache, key)
            if meta is not None and meta[0] == 1040.0:  # noqa: PLR2004
                written = meta[0]
                break
        assert written == 1040.0  # noqa: PLR2004


class TestEarlyRefreshRespectsStaleTtl:
    """Pin that the refresh forwards `stale_ttl` so the reserve re-arms."""

    async def test_refresh_writes_a_stale_reserve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A refresh under stale_ttl rewrites the stale reserve copy.

        After the refresh stores value 2, the stale reserve must also hold
        2, so a dropped `stale_ttl` (no reserve written) is caught.
        """
        cached_mod = sys.modules["grelmicro.cache.cached"]
        clock = _Clock()
        monkeypatch.setattr(cached_mod, "_now", clock)
        monkeypatch.setattr(
            cached_mod, "_xfetch_should_refresh", lambda *_: True
        )
        cache = _make_cache(ttl=60)
        counter = 0

        async def impl(x: int) -> int:  # noqa: ARG001
            nonlocal counter
            counter += 1
            return counter

        fetch = cached(cache, early=0.5, stale_ttl=30)(impl)
        await fetch(5)  # cold miss stores value 1 and reserve 1
        clock.t = 1040  # inside the early window
        await fetch(5)  # hit, refresh stores value 2 and reserve 2

        key = _make_key(impl, (5,), {}, None, typed=False)
        reserve = None
        for _ in range(100):
            await asyncio.sleep(0.005)
            reserve = await cache._read_stale(key)
            if reserve == 2:  # noqa: PLR2004
                break
        assert reserve == 2  # noqa: PLR2004


class TestDistributedComputeDeltaAndStale:
    """Pin the distributed sync compute path's delta sign and stale reserve."""

    async def test_distributed_sync_records_small_delta(self) -> None:
        """A distributed sync cold miss with early= records a sub-second delta.

        Pins the `perf_counter() - started` sign inside
        `_distributed_orchestrate`.
        """
        loop = asyncio.get_running_loop()
        cache = _shared_cache(loop)
        micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])

        def impl(x: int) -> int:
            return x * 2

        fetch = cached(cache, lock=True, early=0.5)(impl)
        async with micro:
            await asyncio.to_thread(lambda: fetch(5))
            key = _make_key(impl, (5,), {}, None, typed=False)
            meta = await _read_meta(cache, key)
        assert meta is not None
        _written, delta = meta
        assert 0 <= delta < 1.0

    async def test_distributed_sync_writes_stale_reserve(self) -> None:
        """A distributed sync miss with stale_ttl writes the stale reserve.

        Pins that `_distributed_orchestrate` forwards `stale_ttl` to `set`.
        """
        loop = asyncio.get_running_loop()
        cache = _shared_cache(loop)
        micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])

        def impl(x: int) -> int:
            return x * 2

        fetch = cached(cache, lock=True, stale_ttl=30)(impl)
        async with micro:
            await asyncio.to_thread(lambda: fetch(5))
            key = _make_key(impl, (5,), {}, None, typed=False)
            reserve = await cache._read_stale(key)
        assert reserve == 10  # noqa: PLR2004


class TestSyncRefreshMetaAndStale:
    """Pin that the sync refresh re-arms XFetch meta and the stale reserve."""

    async def test_sync_refresh_rewrites_meta_at_the_new_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sync refresh rewrites the XFetch sidecar at the refresh time.

        Pins that `early` reaches the sync refresh compute call.
        """
        cached_mod = sys.modules["grelmicro.cache.cached"]
        clock = _Clock()
        monkeypatch.setattr(cached_mod, "_now", clock)
        monkeypatch.setattr(
            cached_mod, "_xfetch_should_refresh", lambda *_: True
        )
        cache = _make_cache(ttl=60)

        def impl(x: int) -> int:
            return x * 2

        fetch = cached(cache, early=0.5)(impl)
        await asyncio.to_thread(lambda: fetch(5))  # cold miss, meta at 1000
        clock.t = 1040  # inside the early window
        await asyncio.to_thread(lambda: fetch(5))  # hit, refresh meta at 1040

        key = _make_key(impl, (5,), {}, None, typed=False)
        written = 1000.0
        for _ in range(100):
            await asyncio.sleep(0.005)
            meta = await _read_meta(cache, key)
            if meta is not None and meta[0] == 1040.0:  # noqa: PLR2004
                written = meta[0]
                break
        assert written == 1040.0  # noqa: PLR2004

    async def test_sync_refresh_writes_stale_reserve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sync refresh under stale_ttl rewrites the stale reserve copy.

        Pins that `stale_ttl` reaches the sync refresh compute call.
        """
        cached_mod = sys.modules["grelmicro.cache.cached"]
        clock = _Clock()
        monkeypatch.setattr(cached_mod, "_now", clock)
        monkeypatch.setattr(
            cached_mod, "_xfetch_should_refresh", lambda *_: True
        )
        cache = _make_cache(ttl=60)
        counter = 0

        def impl(x: int) -> int:  # noqa: ARG001
            nonlocal counter
            counter += 1
            return counter

        fetch = cached(cache, early=0.5, stale_ttl=30)(impl)
        await asyncio.to_thread(lambda: fetch(5))  # cold miss, reserve 1
        clock.t = 1040  # inside the early window
        await asyncio.to_thread(lambda: fetch(5))  # hit, refresh reserve 2

        key = _make_key(impl, (5,), {}, None, typed=False)
        reserve = None
        for _ in range(100):
            await asyncio.sleep(0.005)
            reserve = await cache._read_stale(key)
            if reserve == 2:  # noqa: PLR2004
                break
        assert reserve == 2  # noqa: PLR2004
