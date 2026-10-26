"""How far `@cached` and `get_or_set` fold concurrent misses: `lock=`.

`"process"` folds in the process and never touches the lock backend.
`"host"` and `"cluster"` fold in the process, then through the lock backend
of the app's `Coordination`. `None` turns folding off.
"""

from __future__ import annotations

import asyncio
import re
import warnings
from typing import TYPE_CHECKING

import pytest

from grelmicro import Grelmicro
from grelmicro._environment import recorded_bindings
from grelmicro.cache.cached import cached
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.cache.serializers import PickleSerializer
from grelmicro.cache.ttl import TTLCache
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.errors import (
    BackendScopeError,
    BackendScopeWarning,
    GrelmicroFutureWarning,
    OutOfContextError,
    SettingsValidationError,
)
from grelmicro.metrics._naming import callable_name
from tests._logs import records_of

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

    from grelmicro.types import BackendScope, Environment

pytestmark = [pytest.mark.timeout(5)]

VALUE = 10


def _cache() -> TTLCache:
    """Return a cache on its own in-memory backend."""
    return TTLCache(
        ttl=60, backend=MemoryCacheAdapter(), serializer=PickleSerializer()
    )


async def _double(x: int) -> int:
    return x * 2


async def test_cached_folds_in_the_process_by_default(
    mocker: MockerFixture,
) -> None:
    """The default never asks the lock backend, even when the app has one."""
    backend = MemoryLockAdapter()
    acquire = mocker.spy(backend, "acquire")
    fetch = cached(_cache())(_double)

    async with Grelmicro(uses=[Coordination(lock=backend)]):
        assert await fetch(5) == VALUE

    acquire.assert_not_called()


@pytest.mark.parametrize("scope", ["host", "cluster"])
async def test_cached_folds_through_the_lock_backend(
    mocker: MockerFixture, scope: BackendScope
) -> None:
    """A scope past the process takes the app's lock on a miss."""
    backend = MemoryLockAdapter()
    acquire = mocker.spy(backend, "acquire")
    fetch = cached(_cache(), lock=scope)(_double)

    async with Grelmicro(uses=[Coordination(lock=backend, requires="process")]):
        assert await fetch(5) == VALUE

    acquire.assert_called_once()


async def test_cached_without_a_lock_backend_raises() -> None:
    """A scope past the process with no lock backend is a setup error."""
    fetch = cached(_cache(), lock="cluster")(_double)

    with pytest.raises(OutOfContextError):
        await fetch(5)


async def test_cached_none_never_folds() -> None:
    """`lock=None` runs the function for every concurrent miss."""
    calls = 0
    gate = asyncio.Event()

    async def slow(x: int) -> int:
        nonlocal calls
        calls += 1
        await gate.wait()
        return x * 2

    fetch = cached(_cache(), lock=None)(slow)
    tasks = [asyncio.create_task(fetch(5)) for _ in range(3)]
    await asyncio.sleep(0.01)
    gate.set()
    await asyncio.gather(*tasks)

    assert calls == len(tasks)


@pytest.mark.parametrize("value", [True, False, "local", "everywhere"])
def test_cached_refuses_a_value_that_is_not_a_scope(value: object) -> None:
    """Only a backend scope or `None` is a fold."""
    with pytest.raises(SettingsValidationError, match="lock="):
        cached(_cache(), lock=value)  # type: ignore[arg-type] # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("scope", ["host", "cluster"])
def test_cached_private_cache_refuses_a_scope_past_the_process(
    scope: BackendScope,
) -> None:
    """The `ttl=` cache lives in one process, so a shared fold buys nothing."""
    with pytest.raises(SettingsValidationError, match="ttl="):
        cached(ttl=60, lock=scope)


async def test_cached_lock_backend_short_of_the_scope_is_refused() -> None:
    """A deployed app refuses a lock backend that reaches less far than asked."""
    fetch = cached(_cache(), lock="host")(_double)
    micro = Grelmicro(
        uses=[Coordination(lock=MemoryLockAdapter(), requires="process")],
        environment="production",
    )

    with pytest.warns(BackendScopeWarning, match="MemoryLockAdapter"):
        await micro.__aenter__()
    try:
        with pytest.raises(BackendScopeError, match="lock="):
            await fetch(5)
    finally:
        await micro.__aexit__(None, None, None)


async def test_cached_failing_lock_backend_still_answers(
    mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    """A lock backend that fails folds in the process only, and says so."""
    backend = MemoryLockAdapter()
    mocker.patch.object(backend, "acquire", side_effect=ConnectionError)
    fetch = cached(_cache(), lock="cluster")(_double)

    async with Grelmicro(uses=[Coordination(lock=backend, requires="process")]):
        assert await fetch(5) == VALUE

    assert records_of(caplog, "grelmicro.cache")


async def test_cached_lease_lost_after_the_store_still_answers(
    mocker: MockerFixture,
) -> None:
    """A miss that outlived its lock still returns the value it stored."""
    backend = MemoryLockAdapter()
    mocker.patch.object(backend, "release", return_value=False)
    cache = _cache()
    fetch = cached(cache, lock="cluster")(_double)

    async with Grelmicro(uses=[Coordination(lock=backend, requires="process")]):
        assert await fetch(5) == VALUE
        assert await fetch(5) == VALUE


async def test_get_or_set_folds_in_the_process_by_default(
    mocker: MockerFixture,
) -> None:
    """`get_or_set` shares the default of `@cached`."""
    backend = MemoryLockAdapter()
    acquire = mocker.spy(backend, "acquire")
    cache = _cache()

    async with Grelmicro(uses=[Coordination(lock=backend)]):
        assert await cache.get_or_set("k", lambda: VALUE) == VALUE

    acquire.assert_not_called()


async def test_get_or_set_folds_through_the_lock_backend(
    mocker: MockerFixture,
) -> None:
    """`get_or_set(lock="cluster")` takes the app's lock on a miss."""
    backend = MemoryLockAdapter()
    acquire = mocker.spy(backend, "acquire")
    cache = _cache()

    async with Grelmicro(uses=[Coordination(lock=backend, requires="process")]):
        assert (
            await cache.get_or_set("k", lambda: VALUE, lock="cluster") == VALUE
        )

    acquire.assert_called_once()


async def test_get_or_set_without_a_lock_backend_raises() -> None:
    """A scope past the process with no lock backend is a setup error."""
    with pytest.raises(OutOfContextError):
        await _cache().get_or_set("k", lambda: VALUE, lock="cluster")


@pytest.mark.parametrize("value", [True, "local"])
async def test_get_or_set_refuses_a_value_that_is_not_a_scope(
    value: object,
) -> None:
    """Only a backend scope or `None` is a fold."""
    with pytest.raises(SettingsValidationError, match="lock="):
        await _cache().get_or_set(
            "k",
            lambda: VALUE,
            lock=value,  # type: ignore[arg-type] # ty: ignore[invalid-argument-type]
        )


async def test_cached_short_lock_backend_is_refused_on_every_miss() -> None:
    """The refusal holds for every miss, not only the first."""
    fetch = cached(_cache(), lock="host")(_double)
    micro = Grelmicro(
        uses=[Coordination(lock=MemoryLockAdapter(), requires="process")],
        environment="production",
    )

    with pytest.warns(BackendScopeWarning, match="MemoryLockAdapter"):
        await micro.__aenter__()
    try:
        for x in (5, 6):
            with pytest.raises(BackendScopeError):
                await fetch(x)
    finally:
        await micro.__aexit__(None, None, None)


async def test_cached_failing_lock_backend_warns_once(
    mocker: MockerFixture, caplog: pytest.LogCaptureFixture
) -> None:
    """A lock backend that stays down is reported once, not on every miss."""
    backend = MemoryLockAdapter()
    mocker.patch.object(backend, "acquire", side_effect=ConnectionError)
    fetch = cached(_cache(), lock="cluster")(_double)

    async with Grelmicro(uses=[Coordination(lock=backend, requires="process")]):
        for x in (5, 6, 7):
            await fetch(x)

    assert len(records_of(caplog, "grelmicro.cache")) == 1


async def test_cached_error_under_a_failing_lock_backend_stands_alone(
    mocker: MockerFixture,
) -> None:
    """The function's own error is not chained to the lock backend's."""
    backend = MemoryLockAdapter()
    mocker.patch.object(backend, "acquire", side_effect=ConnectionError)

    async def broken(x: int) -> int:
        raise ValueError(x)

    fetch = cached(_cache(), lock="cluster")(broken)

    async with Grelmicro(uses=[Coordination(lock=backend, requires="process")]):
        with pytest.raises(ValueError, match="5") as caught:
            await fetch(5)

    assert caught.value.__context__ is None


async def test_cached_short_lock_backend_warns_with_no_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no environment declared, a short lock backend only warns."""
    monkeypatch.delenv("GREL_ENVIRONMENT")
    fetch = cached(_cache(), lock="host")(_double)
    micro = Grelmicro(
        uses=[Coordination(lock=MemoryLockAdapter(), requires="process")]
    )

    with pytest.warns(BackendScopeWarning, match="lock="):
        await micro.__aenter__()
    try:
        with pytest.warns(BackendScopeWarning, match="lock="):
            assert await fetch(5) == VALUE
    finally:
        await micro.__aexit__(None, None, None)


@pytest.mark.parametrize("environment", ["development", "test"])
async def test_cached_short_lock_backend_is_quiet_in_development(
    environment: Environment,
) -> None:
    """A development or test app accepts a short lock backend quietly."""
    fetch = cached(_cache(), lock="host")(_double)

    async with Grelmicro(
        uses=[Coordination(lock=MemoryLockAdapter(), requires="process")],
        environment=environment,
    ):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert await fetch(5) == VALUE


class _ClusterLockAdapter(MemoryLockAdapter):
    """An in-memory lock backend that claims to reach the whole cluster."""

    scope = "cluster"


async def test_cached_lock_backend_that_reaches_far_enough_is_accepted() -> (
    None
):
    """A lock backend reaching as far as the scope folds without a word."""
    fetch = cached(_cache(), lock="cluster")(_double)

    async with Grelmicro(
        uses=[Coordination(lock=_ClusterLockAdapter())],
        environment="production",
    ):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert await fetch(5) == VALUE


async def test_get_or_set_none_never_folds() -> None:
    """`get_or_set(lock=None)` calls the factory for every concurrent miss."""
    cache = _cache()
    calls = 0
    gate = asyncio.Event()

    async def factory() -> int:
        nonlocal calls
        calls += 1
        await gate.wait()
        return VALUE

    tasks = [
        asyncio.create_task(cache.get_or_set("k", factory, lock=None))
        for _ in range(3)
    ]
    await asyncio.sleep(0.01)
    gate.set()
    await asyncio.gather(*tasks)

    assert calls == len(tasks)


def _scope_checks(micro: Grelmicro) -> list[tuple[str, str]]:
    """Return the status and detail of each `backend-scope` check."""
    return [
        (check.status, check.detail)
        for check in micro.describe().checks
        if check.name == "backend-scope"
    ]


def _no_lock_backend(name: str) -> str:
    """Return the finding for a fold of `name` with no `Coordination`."""
    return (
        f"Fold({name!r}) is bound to InProcessLock, which provides scope "
        "'process', but requires scope 'cluster'"
    )


@pytest.mark.parametrize("environment", ["staging", "production"])
async def test_cached_fold_with_no_lock_backend_warns_at_open(
    environment: Environment,
) -> None:
    """A strict tier opens with a `BackendScopeWarning`, and the miss raises."""
    fetch = cached(_cache(), lock="cluster")(_double)
    micro = Grelmicro(environment=environment)
    finding = _no_lock_backend(callable_name(_double))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await micro.__aenter__()
    try:
        categories = [warning.category for warning in caught]
        assert categories == [BackendScopeWarning]
        message = str(caught[0].message)
        assert f"{finding} in environment {environment!r}." in message
        assert "A call that misses the cache raises." in message
        assert _scope_checks(micro) == [("warn", finding)]
        with pytest.raises(OutOfContextError):
            await fetch(5)
    finally:
        await micro.__aexit__(None, None, None)


@pytest.mark.parametrize("environment", ["staging", "production"])
async def test_cached_fold_never_raises_at_open(
    environment: Environment,
) -> None:
    """An unmet fold never stops the app from opening, nor warns of a future raise."""
    fetch = cached(_cache(), lock="host")(_double)
    micro = Grelmicro(
        uses=[Coordination(lock=MemoryLockAdapter(), requires="process")],
        environment=environment,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error", GrelmicroFutureWarning)
        warnings.simplefilter("ignore", BackendScopeWarning)
        async with micro:
            assert [status for status, _ in _scope_checks(micro)] == ["warn"]
    del fetch


async def test_cached_fold_with_no_lock_backend_warns_with_no_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no tier declared, the open warns as for any other backend."""
    monkeypatch.delenv("GREL_ENVIRONMENT")
    fetch = cached(_cache(), lock="cluster")(_double)
    micro = Grelmicro()

    with pytest.warns(BackendScopeWarning, match="InProcessLock"):
        await micro.__aenter__()
    try:
        assert [status for status, _ in _scope_checks(micro)] == ["warn"]
    finally:
        await micro.__aexit__(None, None, None)
    del fetch


def test_cached_fold_with_no_lock_backend_warns_in_check_backends() -> None:
    """`check_backends()` warns for the fold and raises nothing."""
    fetch = cached(_cache(), lock="cluster")(_double)

    with pytest.warns(
        BackendScopeWarning,
        match=re.escape(_no_lock_backend(callable_name(_double))),
    ):
        Grelmicro().check_backends()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        Grelmicro(
            uses=[Coordination(lock=_ClusterLockAdapter())]
        ).check_backends()
    del fetch


async def test_cached_fold_on_a_lock_backend_that_reaches_far_enough_is_ok() -> (
    None
):
    """A `Coordination` on a cluster backend passes the check at open."""
    fetch = cached(_cache(), lock="cluster")(_double)
    micro = Grelmicro(
        uses=[Coordination(lock=_ClusterLockAdapter())],
        environment="production",
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        async with micro:
            assert [status for status, _ in _scope_checks(micro)] == ["ok"]
    del fetch


@pytest.mark.parametrize("environment", ["development", "test"])
async def test_cached_fold_with_no_lock_backend_is_quiet_in_development(
    environment: Environment,
) -> None:
    """A development or test app opens quietly, and the miss raises as before."""
    fetch = cached(_cache(), lock="cluster")(_double)
    micro = Grelmicro(environment=environment)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        async with micro:
            assert [status for status, _ in _scope_checks(micro)] == ["ok"]
            with pytest.raises(OutOfContextError):
                await fetch(5)


def test_cached_process_fold_records_nothing() -> None:
    """Only a fold past the process rides the lock backend."""
    fetch = cached(_cache())(_double)
    unfolded = cached(_cache(), lock=None)(_double)

    assert recorded_bindings() == []
    del fetch, unfolded
