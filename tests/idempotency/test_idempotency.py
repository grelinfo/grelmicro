"""Test Idempotency."""

from __future__ import annotations

import asyncio
import logging
from time import monotonic
from unittest.mock import patch

import pytest

from grelmicro import Grelmicro
from grelmicro._app import NoActiveAppError
from grelmicro._config import reconfigurable_instances, reconfigure_all
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.cache.serializers import JsonSerializer
from grelmicro.cache.ttl import TTLCache
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.idempotency import (
    Idempotency,
    IdempotencyConfig,
    IdempotencyConflictError,
    idempotent,
)

pytestmark = [pytest.mark.timeout(10)]

EXPECTED_CALLS_2 = 2


@pytest.fixture
def backend() -> MemoryCacheAdapter:
    """Provide an isolated in-memory cache backend."""
    return MemoryCacheAdapter()


@pytest.fixture
def cache(backend: MemoryCacheAdapter) -> TTLCache:
    """Provide a TTLCache bound to the in-memory backend with JSON."""
    return TTLCache(ttl=3600, backend=backend, serializer=JsonSerializer())


# ---------------------------------------------------------------------------
# First execution and replay
# ---------------------------------------------------------------------------


class TestFirstExecutionAndReplay:
    """Test first execution stores and replay within ttl."""

    async def test_first_execution_stores(self, cache: TTLCache) -> None:
        """The first call runs the work and stores the response."""
        idem = Idempotency("charge", ttl=3600, cache=cache)
        calls = 0

        async with idem("key-1") as op:
            assert op.replayed is False
            assert op.response is None
            calls += 1
            op.store({"status": "ok"})

        assert calls == 1

    async def test_replay_within_ttl(self, cache: TTLCache) -> None:
        """A repeated key replays the stored response without executing."""
        idem = Idempotency("charge", ttl=3600, cache=cache)
        calls = 0

        async with idem("key-1") as op:
            calls += 1
            op.store({"status": "ok"})

        async with idem("key-1") as op:
            assert op.replayed is True
            assert op.response == {"status": "ok"}

        assert calls == 1

    async def test_expiry_after_ttl(self, backend: MemoryCacheAdapter) -> None:
        """After ttl elapses, the same key executes fresh."""
        cache = TTLCache(ttl=5, backend=backend, serializer=JsonSerializer())
        idem = Idempotency("charge", ttl=5, cache=cache)
        now = monotonic()
        calls = 0

        with patch("grelmicro.cache.memory.monotonic", return_value=now):
            async with idem("key-1") as op:
                calls += 1
                op.store({"n": calls})

        with patch("grelmicro.cache.memory.monotonic", return_value=now + 6):
            async with idem("key-1") as op:
                assert op.replayed is False
                calls += 1
                op.store({"n": calls})

        assert calls == EXPECTED_CALLS_2

    async def test_no_store_opt_out(self, cache: TTLCache) -> None:
        """Exiting a first execution without store persists nothing."""
        idem = Idempotency("charge", ttl=3600, cache=cache)

        async with idem("key-1") as op:
            assert op.replayed is False
            # No op.store(...) call.

        async with idem("key-1") as op:
            assert op.replayed is False


# ---------------------------------------------------------------------------
# Failure semantics
# ---------------------------------------------------------------------------


class TestFailure:
    """Test that a failed operation stores nothing and releases the marker."""

    async def test_failure_stores_nothing(self, cache: TTLCache) -> None:
        """An exception in the block stores nothing and a retry runs fresh."""
        idem = Idempotency("charge", ttl=3600, cache=cache)
        calls = 0

        async def first() -> None:
            nonlocal calls
            async with idem("key-1") as op:
                calls += 1
                op.store({"status": "ok"})
                msg = "boom"
                raise RuntimeError(msg)

        with pytest.raises(RuntimeError, match="boom"):
            await first()

        async with idem("key-1") as op:
            assert op.replayed is False
            calls += 1
            op.store({"status": "ok"})

        assert calls == EXPECTED_CALLS_2

    async def test_failure_releases_marker(self, cache: TTLCache) -> None:
        """A failed first execution leaves the key free for a fresh retry."""
        idem = Idempotency("charge", ttl=3600, cache=cache)

        async def first() -> None:
            async with idem("key-1"):
                msg = "bad"
                raise ValueError(msg)

        with pytest.raises(ValueError, match="bad"):
            await first()

        async with idem("key-1") as op:
            calls = 1
            op.store({"n": calls})

        async with idem("key-1") as op:
            assert op.replayed is True
            assert op.response == {"n": 1}


# ---------------------------------------------------------------------------
# Single-flight
# ---------------------------------------------------------------------------


class TestSingleFlight:
    """Test concurrent duplicates fold to a single execution."""

    async def test_concurrent_duplicates_in_process(
        self, cache: TTLCache
    ) -> None:
        """In-process duplicates wait and replay the stored response."""
        idem = Idempotency("charge", ttl=3600, cache=cache)
        calls = 0
        barrier = asyncio.Event()

        async def run() -> dict:
            async with idem("key-1") as op:
                if op.replayed:
                    assert op.response is not None
                    return op.response
                nonlocal calls
                calls += 1
                await barrier.wait()
                op.store({"n": calls})
                return {"n": calls}

        async with asyncio.TaskGroup() as tg:
            task_a = tg.create_task(run())
            await asyncio.sleep(0.02)
            task_b = tg.create_task(run())
            await asyncio.sleep(0.02)
            barrier.set()

        assert calls == 1
        assert task_a.result() == {"n": 1}
        assert task_b.result() == {"n": 1}

    async def test_concurrent_duplicates_distributed(self) -> None:
        """Duplicates fold across replicas through the lock backend."""
        loop = asyncio.get_running_loop()
        backend = MemoryCacheAdapter()
        backend._loop = loop
        cache = TTLCache(ttl=3600, backend=backend, serializer=JsonSerializer())
        micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])
        calls = 0
        barrier = asyncio.Event()

        idem_a = Idempotency("charge", ttl=3600, cache=cache)
        idem_b = Idempotency("charge", ttl=3600, cache=cache)

        async def run(idem: Idempotency) -> dict:
            async with idem("key-1") as op:
                if op.replayed:
                    assert op.response is not None
                    return op.response
                nonlocal calls
                calls += 1
                await barrier.wait()
                op.store({"n": calls})
                return {"n": calls}

        async with micro, asyncio.TaskGroup() as tg:
            task_a = tg.create_task(run(idem_a))
            await asyncio.sleep(0.02)
            task_b = tg.create_task(run(idem_b))
            await asyncio.sleep(0.02)
            barrier.set()

        assert calls == 1
        assert task_a.result() == {"n": 1}
        assert task_b.result() == {"n": 1}


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


class TestFingerprint:
    """Test payload fingerprint match and conflict."""

    async def test_fingerprint_match_replays(self, cache: TTLCache) -> None:
        """A replay with the same fingerprint returns the stored response."""
        idem = Idempotency("charge", ttl=3600, cache=cache)

        async with idem("key-1", fingerprint="abc") as op:
            op.store({"status": "ok"})

        async with idem("key-1", fingerprint="abc") as op:
            assert op.replayed is True
            assert op.response == {"status": "ok"}

    async def test_fingerprint_conflict_raises(self, cache: TTLCache) -> None:
        """A replay with a different fingerprint raises a conflict."""
        idem = Idempotency("charge", ttl=3600, cache=cache)

        async with idem("key-1", fingerprint="abc") as op:
            op.store({"status": "ok"})

        with pytest.raises(IdempotencyConflictError):
            async with idem("key-1", fingerprint="xyz"):
                pass

    async def test_no_fingerprint_no_check(self, cache: TTLCache) -> None:
        """Without a fingerprint, no conflict check runs."""
        idem = Idempotency("charge", ttl=3600, cache=cache)

        async with idem("key-1") as op:
            op.store({"status": "ok"})

        async with idem("key-1") as op:
            assert op.replayed is True

    async def test_instance_level_fingerprint(self, cache: TTLCache) -> None:
        """An instance-level fingerprint applies to every call."""
        idem = Idempotency("charge", ttl=3600, cache=cache, fingerprint="abc")

        async with idem("key-1") as op:
            op.store({"status": "ok"})

        with pytest.raises(IdempotencyConflictError):
            async with idem("key-1", fingerprint="xyz"):
                pass


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


class TestDecorator:
    """Test the @idempotent decorator form."""

    async def test_decorator_replays(self, cache: TTLCache) -> None:
        """The decorator runs once and replays on a repeated key."""
        idem = Idempotency("charge", ttl=3600, cache=cache)
        calls = 0

        @idempotent(idem, key=lambda **kw: kw["idempotency_key"])
        async def charge(amount: int, *, idempotency_key: str) -> dict:  # noqa: ARG001
            nonlocal calls
            calls += 1
            return {"amount": amount}

        first = await charge(amount=100, idempotency_key="k1")
        second = await charge(amount=100, idempotency_key="k1")

        assert first == {"amount": 100}
        assert second == {"amount": 100}
        assert calls == 1

    async def test_decorator_failure_stores_nothing(
        self, cache: TTLCache
    ) -> None:
        """A failing decorated call stores nothing and retries fresh."""
        idem = Idempotency("charge", ttl=3600, cache=cache)
        calls = 0

        @idempotent(idem, key=lambda **kw: kw["idempotency_key"])
        async def charge(*, idempotency_key: str) -> dict:  # noqa: ARG001
            nonlocal calls
            calls += 1
            if calls == 1:
                msg = "boom"
                raise RuntimeError(msg)
            return {"ok": True}

        with pytest.raises(RuntimeError, match="boom"):
            await charge(idempotency_key="k1")
        assert await charge(idempotency_key="k1") == {"ok": True}
        assert calls == EXPECTED_CALLS_2

    async def test_decorator_fingerprint_conflict(
        self, cache: TTLCache
    ) -> None:
        """A decorator fingerprint mismatch raises a conflict."""
        idem = Idempotency("charge", ttl=3600, cache=cache)

        @idempotent(
            idem,
            key=lambda **kw: kw["idempotency_key"],
            fingerprint=lambda **kw: str(kw["amount"]),
        )
        async def charge(*, amount: int, idempotency_key: str) -> dict:  # noqa: ARG001
            return {"amount": amount}

        await charge(amount=100, idempotency_key="k1")
        with pytest.raises(IdempotencyConflictError):
            await charge(amount=200, idempotency_key="k1")


# ---------------------------------------------------------------------------
# Backend resolution
# ---------------------------------------------------------------------------


class TestBackendResolution:
    """Test cache backend resolution rules."""

    async def test_out_of_context_without_cache_or_component(self) -> None:
        """No explicit cache and no active app raises out of context."""
        idem = Idempotency("charge", ttl=3600)

        with pytest.raises(NoActiveAppError):
            async with idem("key-1"):
                pass


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


class TestConfig:
    """Test env config, from_config, and live reconfigure."""

    def test_env_config_resolution(
        self, monkeypatch: pytest.MonkeyPatch, cache: TTLCache
    ) -> None:
        """Env vars under GREL_IDEMPOTENCY_{NAME}_ populate unset fields."""
        monkeypatch.setenv("GREL_IDEMPOTENCY_CHARGE_TTL", "120")
        idem = Idempotency("charge", cache=cache)
        expected_ttl = 120
        assert idem.config.ttl == expected_ttl

    def test_env_prefix_override(
        self, monkeypatch: pytest.MonkeyPatch, cache: TTLCache
    ) -> None:
        """env_prefix replaces the auto-derived prefix."""
        monkeypatch.setenv("MYAPP_IDEM_TTL", "200")
        idem = Idempotency("charge", cache=cache, env_prefix="MYAPP_IDEM_")
        expected_ttl = 200
        assert idem.config.ttl == expected_ttl

    def test_env_load_false_ignores_env(
        self, monkeypatch: pytest.MonkeyPatch, cache: TTLCache
    ) -> None:
        """env_load=False skips env reads entirely."""
        monkeypatch.setenv("GREL_IDEMPOTENCY_CHARGE_TTL", "120")
        idem = Idempotency("charge", cache=cache, env_load=False)
        expected_ttl = 86400
        assert idem.config.ttl == expected_ttl

    def test_from_config_static(
        self, monkeypatch: pytest.MonkeyPatch, cache: TTLCache
    ) -> None:
        """from_config uses the config as-is and bypasses env."""
        monkeypatch.setenv("GREL_IDEMPOTENCY_CHARGE_TTL", "120")
        config = IdempotencyConfig(ttl=30)
        idem = Idempotency.from_config("charge", config, cache=cache)
        expected_ttl = 30
        assert idem.config.ttl == expected_ttl

    def test_from_config_not_tracked(self, cache: TTLCache) -> None:
        """The from_config instances opt out of live reload."""
        config = IdempotencyConfig(ttl=30)
        idem = Idempotency.from_config("charge-static", config, cache=cache)
        assert idem not in reconfigurable_instances()

    async def test_live_reconfigure_ttl(self, cache: TTLCache) -> None:
        """Reconfigure swaps the ttl for later operations."""
        idem = Idempotency("charge", ttl=3600, cache=cache)
        await idem.reconfigure(idem.config.model_copy(update={"ttl": 10}))
        expected_ttl = 10
        assert idem.config.ttl == expected_ttl

    async def test_reconfigure_via_mapping(self, cache: TTLCache) -> None:
        """A ConfigMap-style mapping reconfigures the tracked instance."""
        idem = Idempotency("charge-reload", ttl=3600, cache=cache)
        await reconfigure_all({"GREL_IDEMPOTENCY_CHARGE_RELOAD_TTL": "42"})
        expected_ttl = 42
        assert idem.config.ttl == expected_ttl


# ---------------------------------------------------------------------------
# Coverage gap tests
# ---------------------------------------------------------------------------


class TestCoverageGaps:
    """Tests that exercise previously uncovered lines."""

    def test_name_property(self, cache: TTLCache) -> None:
        """The `name` property returns the idempotency namespace."""
        idem = Idempotency("billing", ttl=3600, cache=cache)
        assert idem.name == "billing"

    async def test_store_on_replay_is_noop(self, cache: TTLCache) -> None:
        """Calling `op.store()` during a replay is silently ignored."""
        idem = Idempotency("charge", ttl=3600, cache=cache)

        async with idem("key-1") as op:
            op.store({"status": "ok"})

        async with idem("key-1") as op:
            assert op.replayed is True
            op.store({"status": "overwrite"})  # must be a no-op

        async with idem("key-1") as op:
            assert op.replayed is True
            assert op.response == {"status": "ok"}

    async def test_exception_inside_try_releases_locks(self) -> None:
        """An exception inside the try block releases both the in-process and distributed locks."""
        loop = asyncio.get_running_loop()
        backend = MemoryCacheAdapter()
        backend._loop = loop
        idem_cache = TTLCache(
            ttl=3600, backend=backend, serializer=JsonSerializer()
        )
        micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])
        idem = Idempotency("charge", ttl=3600, cache=idem_cache)

        # Patch _replay to raise on the third call (first two return _SENTINEL;
        # the third happens after the distributed lock is acquired).
        # Three calls: before try, inside try before distributed lock, after lock.
        _RAISE_AFTER = 3  # noqa: N806
        call_count = 0
        original_replay = idem._replay  # type: ignore[attr-defined]

        async def patched_replay(key: str, fp: str | None) -> object:
            nonlocal call_count
            call_count += 1
            if call_count >= _RAISE_AFTER:
                msg = "cache error after lock"
                raise RuntimeError(msg)
            return await original_replay(key, fp)

        idem._replay = patched_replay  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]

        async with micro:
            with pytest.raises(RuntimeError, match="cache error after lock"):
                async with idem("key-err"):
                    pass  # never reached; enter raises

    async def test_reconfigure_all_type_error_is_logged_not_raised(
        self, cache: TTLCache, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A TypeError from reconfigure is logged and swallowed by reconfigure_all."""
        idem = Idempotency("charge-bad", ttl=3600, cache=cache)

        async def bad_reconfigure(new_config: object) -> None:  # noqa: ARG001
            msg = "cannot change worker at runtime"
            raise TypeError(msg)

        with (
            patch.object(idem, "reconfigure", bad_reconfigure),
            caplog.at_level(logging.WARNING, logger="grelmicro"),
        ):
            await reconfigure_all({"GREL_IDEMPOTENCY_CHARGE_BAD_TTL": "42"})

        assert any("rejected" in record.message for record in caplog.records)
