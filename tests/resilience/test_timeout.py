"""Timeout policy tests."""

import asyncio

import pytest
from pydantic import ValidationError

from grelmicro.resilience import Retry, Timeout, TimeoutConfig

_TWO = 2.0
_THREE_HALF = 3.5
_ONE_QUARTER = 1.25
_ONE = 1.0

# --- TimeoutConfig validation ---------------------------------------------


def test_config_requires_seconds() -> None:
    """`TimeoutConfig` raises when `seconds` is missing."""
    with pytest.raises(ValidationError):
        TimeoutConfig()  # type: ignore[call-arg]  # ty: ignore[missing-argument]


def test_config_rejects_zero_seconds() -> None:
    """`seconds` must be strictly positive."""
    with pytest.raises(ValidationError):
        TimeoutConfig(seconds=0)


def test_config_rejects_negative_seconds() -> None:
    """`seconds` must be strictly positive."""
    with pytest.raises(ValidationError):
        TimeoutConfig(seconds=-1.0)


def test_config_frozen() -> None:
    """`TimeoutConfig` is frozen."""
    cfg = TimeoutConfig(seconds=1.0)
    with pytest.raises(ValidationError):
        cfg.seconds = 2.0  # type: ignore[misc]  # ty: ignore[invalid-assignment]


def test_config_forbids_extra() -> None:
    """`TimeoutConfig` rejects unknown fields."""
    with pytest.raises(ValidationError):
        TimeoutConfig(seconds=1.0, policy="raise")  # type: ignore[call-arg]  # ty: ignore[unknown-argument]


# --- Class-form construction ----------------------------------------------


def test_constructs_with_seconds_kwarg() -> None:
    """The simple `seconds=` path builds a valid policy."""
    policy = Timeout("db", seconds=2.0)
    assert policy.name == "db"
    assert policy.config.seconds == _TWO


def test_constructs_from_config() -> None:
    """`from_config` accepts a pre-built `TimeoutConfig`."""
    cfg = TimeoutConfig(seconds=0.5)
    policy = Timeout.from_config("db", cfg)
    assert policy.config is cfg


def test_rejects_seconds_and_config_together() -> None:
    """Mixing kwargs with a pre-built config is rejected."""
    with pytest.raises(TypeError):
        Timeout(
            "db",
            seconds=1.0,
            config=TimeoutConfig(seconds=2.0),
        )


def test_requires_seconds_when_env_off() -> None:
    """No kwargs and no env path means construction fails."""
    with pytest.raises(ValidationError):
        Timeout("db")


def test_env_load_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env path reads `GREL_TIMEOUT_{NAME}_SECONDS`."""
    monkeypatch.setenv("GREL_TIMEOUT_DB_SECONDS", "3.5")
    policy = Timeout("db", env_load=True)
    assert policy.config.seconds == _THREE_HALF


def test_env_segment_normalises_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Names with dashes map to underscored env segments."""
    monkeypatch.setenv("GREL_TIMEOUT_PAYMENTS_EU_SECONDS", "1.25")
    policy = Timeout("payments-eu", env_load=True)
    assert policy.config.seconds == _ONE_QUARTER


# --- Async context manager behavior ---------------------------------------


async def test_context_manager_success() -> None:
    """A fast inner block completes without raising."""
    policy = Timeout("t", seconds=1.0)
    async with policy:
        await asyncio.sleep(0)


async def test_context_manager_times_out() -> None:
    """A slow inner block hits the deadline and raises `TimeoutError`."""
    policy = Timeout("t", seconds=0.01)
    with pytest.raises(TimeoutError):
        async with policy:
            await asyncio.sleep(0.5)


async def test_context_manager_concurrent_tasks() -> None:
    """The same policy can be entered concurrently by multiple tasks."""
    policy = Timeout("t", seconds=0.1)

    async def task() -> bool:
        try:
            async with policy:
                await asyncio.sleep(0.5)
        except TimeoutError:
            return True
        return False

    results = await asyncio.gather(task(), task(), task())
    assert results == [True, True, True]


async def test_context_manager_nested() -> None:
    """Nested entries of the same policy each get their own deadline."""
    policy = Timeout("t", seconds=1.0)
    async with policy, policy:
        await asyncio.sleep(0)


# --- Decorator behavior ---------------------------------------------------


async def test_decorator_returns_function_value_on_success() -> None:
    """Fast async function returns its value."""
    policy = Timeout("t", seconds=1.0)

    @policy
    async def fn() -> str:
        return "ok"

    assert await fn() == "ok"


async def test_decorator_raises_on_timeout() -> None:
    """A slow decorated function raises `TimeoutError`."""
    policy = Timeout("t", seconds=0.01)

    @policy
    async def fn() -> None:
        await asyncio.sleep(0.5)

    with pytest.raises(TimeoutError):
        await fn()


def test_decorator_rejects_sync_function() -> None:
    """Sync functions cannot be timed out via asyncio."""
    policy = Timeout("t", seconds=1.0)

    def sync_fn() -> None: ...

    with pytest.raises(TypeError):
        policy(sync_fn)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


# --- Live reconfiguration -------------------------------------------------


async def test_reconfigure_updates_deadline() -> None:
    """`reconfigure` swaps the snapshot for future entries."""
    policy = Timeout("t", seconds=0.01)

    new_config = policy.config.model_copy(update={"seconds": 1.0})
    await policy.reconfigure(new_config)
    assert policy.config.seconds == _ONE

    async with policy:
        await asyncio.sleep(0)


async def test_reconfigure_requires_same_type() -> None:
    """`reconfigure` rejects a different config type."""
    policy = Timeout("t", seconds=1.0)
    with pytest.raises(TypeError):
        await policy.reconfigure("not a config")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


async def test_reconfigure_does_not_affect_in_flight_scope() -> None:
    """In-flight scopes keep their original deadline across `reconfigure`."""
    policy = Timeout("t", seconds=1.0)

    started = asyncio.Event()
    swap_done = asyncio.Event()

    async def long_call() -> None:
        async with policy:
            started.set()
            await swap_done.wait()
            await asyncio.sleep(0)

    task = asyncio.create_task(long_call())
    await started.wait()
    tight = policy.config.model_copy(update={"seconds": 0.001})
    await policy.reconfigure(tight)
    swap_done.set()
    await task


# --- Cancellation and composition -----------------------------------------


async def test_outer_cancellation_propagates_as_cancelled_error() -> None:
    """Cancelling the outer task surfaces `CancelledError`, not `TimeoutError`."""
    policy = Timeout("t", seconds=10.0)

    started = asyncio.Event()

    async def runner() -> None:
        async with policy:
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(runner())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


_THREE = 3


async def test_composes_under_retry() -> None:
    """A `Timeout` placed inside a `Retry` retries each timed-out attempt."""
    policy = Timeout("t", seconds=0.01)
    retrier = Retry.constant("t", when=TimeoutError, attempts=3, delay=0.001)

    attempts = 0

    @retrier
    @policy
    async def call() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < _THREE:
            await asyncio.sleep(0.5)
        return "ok"

    assert await call() == "ok"
    assert attempts == _THREE
