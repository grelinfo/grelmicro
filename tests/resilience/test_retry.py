"""Retry policy tests."""

import asyncio as _asyncio
import time as _time

import pytest
from pydantic import ValidationError

from grelmicro.resilience import (
    ConstantBackoffConfig,
    ExponentialBackoffConfig,
    Retry,
    RetryConfig,
    retry,
    retrying,
)

_FAST_DELAY = 0.001
_DEFAULT_ATTEMPTS = 3
_TWO = 2
_THREE = 3
_FOUR = 4
_FIVE = 5
_TEN = 10


@pytest.fixture
def fast_constant() -> ConstantBackoffConfig:
    """Build a constant backoff with negligible delay."""
    return ConstantBackoffConfig(delay=_FAST_DELAY)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch asyncio.sleep / time.sleep to no-ops to keep tests fast."""

    async def _async_noop(_seconds: float) -> None:
        return

    def _sync_noop(_seconds: float) -> None:
        return

    monkeypatch.setattr(_asyncio, "sleep", _async_noop)
    monkeypatch.setattr(_time, "sleep", _sync_noop)


# --- RetryConfig validation -----------------------------------------------


def test_config_requires_on() -> None:
    """`RetryConfig` raises when `on` is missing."""
    with pytest.raises(ValidationError):
        RetryConfig()  # type: ignore[call-arg]  # ty: ignore[missing-argument]


def test_config_default_attempts_and_backoff() -> None:
    """Defaults: 3 attempts, exponential backoff."""
    config = RetryConfig(on=(ValueError,))  # ty: ignore[missing-argument]
    assert config.attempts == _DEFAULT_ATTEMPTS
    assert isinstance(config.backoff, ExponentialBackoffConfig)


def test_config_frozen() -> None:
    """`RetryConfig` is frozen."""
    config = RetryConfig(on=(ValueError,))  # ty: ignore[missing-argument]
    with pytest.raises(ValidationError):
        config.attempts = _FIVE  # type: ignore[misc]  # ty: ignore[invalid-assignment]


# --- Class-form construction ----------------------------------------------


def test_retry_constructs_with_class_filter(
    fast_constant: ConstantBackoffConfig,
) -> None:
    """Construct accepts a single class filter."""
    policy = Retry("test", fast_constant, on=ValueError, attempts=_THREE)
    assert policy.name == "test"
    assert policy.config.attempts == _THREE


def test_retry_normalizes_single_class_to_tuple(
    fast_constant: ConstantBackoffConfig,
) -> None:
    """Single class is normalized to a tuple."""
    policy = Retry("test", fast_constant, on=ValueError)
    assert policy.config.on == (ValueError,)


def test_retry_accepts_tuple_filter(
    fast_constant: ConstantBackoffConfig,
) -> None:
    """Tuple of classes is preserved."""
    policy = Retry("test", fast_constant, on=(ValueError, KeyError))
    assert policy.config.on == (ValueError, KeyError)


def test_retry_accepts_callable_filter(
    fast_constant: ConstantBackoffConfig,
) -> None:
    """Callable predicate is accepted."""

    def predicate(exc: BaseException) -> bool:
        return isinstance(exc, ValueError)

    policy = Retry("test", fast_constant, on=predicate)
    assert callable(policy.config.on)


# --- Factory classmethods -------------------------------------------------


def test_exponential_factory() -> None:
    """`Retry.exponential` builds an exponential backoff."""
    expected_base, expected_max, expected_attempts = 0.5, 20.0, 5
    policy = Retry.exponential(
        "api",
        on=ValueError,
        attempts=expected_attempts,
        base_delay=expected_base,
        max_delay=expected_max,
    )
    backoff = policy.config.backoff
    assert isinstance(backoff, ExponentialBackoffConfig)
    assert backoff.base_delay == expected_base
    assert backoff.max_delay == expected_max
    assert policy.config.attempts == expected_attempts


def test_constant_factory() -> None:
    """`Retry.constant` builds a constant backoff."""
    expected_delay, expected_attempts = 2.0, 10
    policy = Retry.constant(
        "polling",
        on=ValueError,
        attempts=expected_attempts,
        delay=expected_delay,
    )
    backoff = policy.config.backoff
    assert isinstance(backoff, ConstantBackoffConfig)
    assert backoff.delay == expected_delay
    assert policy.config.attempts == expected_attempts


# --- Decorator behavior ---------------------------------------------------


async def test_decorator_succeeds_on_first_call(
    fast_constant: ConstantBackoffConfig,
) -> None:
    """No retry happens when the function succeeds."""
    calls: list[int] = []

    @retry(on=ValueError, backoff=fast_constant)
    async def fn() -> str:
        calls.append(1)
        return "ok"

    assert await fn() == "ok"
    assert len(calls) == 1


async def test_decorator_retries_until_success(
    fast_constant: ConstantBackoffConfig,
) -> None:
    """Retries continue until the function succeeds."""
    calls: list[int] = []
    succeed_after = _THREE

    @retry(on=ValueError, attempts=_FIVE, backoff=fast_constant)
    async def fn() -> str:
        calls.append(1)
        if len(calls) < succeed_after:
            msg = "transient"
            raise ValueError(msg)
        return "ok"

    assert await fn() == "ok"
    assert len(calls) == succeed_after


async def test_decorator_raises_after_exhaustion(
    fast_constant: ConstantBackoffConfig,
) -> None:
    """Re-raises the underlying exception with a PEP 678 note."""

    @retry(on=ValueError, attempts=_THREE, backoff=fast_constant)
    async def fn() -> None:
        msg = "persistent"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="persistent") as info:
        await fn()
    notes = getattr(info.value, "__notes__", [])
    assert any("3/3 attempts exhausted" in n for n in notes)


async def test_decorator_does_not_retry_on_unmatched_exception(
    fast_constant: ConstantBackoffConfig,
) -> None:
    """Unmatched exceptions escape immediately."""
    calls: list[int] = []

    @retry(on=ValueError, attempts=_FIVE, backoff=fast_constant)
    async def fn() -> None:
        calls.append(1)
        msg = "not retryable"
        raise KeyError(msg)

    with pytest.raises(KeyError, match="not retryable"):
        await fn()
    assert len(calls) == 1


async def test_decorator_with_callable_filter(
    fast_constant: ConstantBackoffConfig,
) -> None:
    """Callable predicate filters retries."""
    calls: list[int] = []

    @retry(
        on=lambda e: isinstance(e, ValueError) and "retry" in str(e),
        attempts=_THREE,
        backoff=fast_constant,
    )
    async def fn() -> None:
        calls.append(1)
        msg = "retry me" if len(calls) == 1 else "stop"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="stop"):
        await fn()
    assert len(calls) == _TWO


def test_decorator_on_sync_function(
    fast_constant: ConstantBackoffConfig,
) -> None:
    """Decorator auto-detects sync functions."""
    calls: list[int] = []

    @retry(on=ValueError, attempts=_THREE, backoff=fast_constant)
    def fn() -> str:
        calls.append(1)
        if len(calls) < _TWO:
            msg = "once"
            raise ValueError(msg)
        return "ok"

    assert fn() == "ok"
    assert len(calls) == _TWO


# --- Sub-factory decorators ------------------------------------------------


async def test_retry_constant_sub_factory() -> None:
    """`@retry.constant` is the explicit constant-backoff form."""
    calls: list[int] = []

    @retry.constant(on=ValueError, attempts=_THREE, delay=_FAST_DELAY)
    async def fn() -> str:
        calls.append(1)
        if len(calls) < _TWO:
            msg = "once"
            raise ValueError(msg)
        return "ok"

    assert await fn() == "ok"


async def test_retry_exponential_sub_factory() -> None:
    """`@retry.exponential` is the explicit exponential-backoff form."""
    calls: list[int] = []

    @retry.exponential(
        on=ValueError, attempts=_THREE, base_delay=_FAST_DELAY, jitter="none"
    )
    async def fn() -> str:
        calls.append(1)
        if len(calls) < _TWO:
            msg = "once"
            raise ValueError(msg)
        return "ok"

    assert await fn() == "ok"


# --- Block form -----------------------------------------------------------


async def test_retrying_block_form(
    fast_constant: ConstantBackoffConfig,
) -> None:
    """`async for attempt in retrying(...)` retries the block."""
    calls: list[int] = []
    succeed_after = _THREE
    async for attempt in retrying(
        on=ValueError, attempts=_FIVE, backoff=fast_constant
    ):
        async with attempt:
            calls.append(1)
            if len(calls) < succeed_after:
                msg = "transient"
                raise ValueError(msg)
    assert len(calls) == succeed_after


async def test_retrying_exhaustion_reraises(
    fast_constant: ConstantBackoffConfig,
) -> None:
    """Block form re-raises the underlying error."""
    with pytest.raises(ValueError, match="persistent"):  # noqa: PT012
        async for attempt in retrying(
            on=ValueError, attempts=_THREE, backoff=fast_constant
        ):
            async with attempt:
                msg = "persistent"
                raise ValueError(msg)


async def test_retrying_constant_sub_factory() -> None:
    """`retrying.constant` is the explicit constant block form."""
    calls: list[int] = []
    async for attempt in retrying.constant(
        on=ValueError, attempts=_THREE, delay=_FAST_DELAY
    ):
        async with attempt:
            calls.append(1)
            if len(calls) < _TWO:
                msg = "once"
                raise ValueError(msg)
    assert len(calls) == _TWO


async def test_retrying_exponential_sub_factory() -> None:
    """`retrying.exponential` is the explicit exponential block form."""
    calls: list[int] = []
    async for attempt in retrying.exponential(
        on=ValueError, attempts=_THREE, base_delay=_FAST_DELAY, jitter="none"
    ):
        async with attempt:
            calls.append(1)
            if len(calls) < _TWO:
                msg = "once"
                raise ValueError(msg)
    assert len(calls) == _TWO


# --- Class as iterator and decorator --------------------------------------


async def test_class_form_iterator() -> None:
    """An instance can be used as an async iterator."""
    policy = Retry.constant(
        "test", on=ValueError, attempts=_THREE, delay=_FAST_DELAY
    )
    calls: list[int] = []
    async for attempt in policy:
        async with attempt:
            calls.append(1)
            if len(calls) < _TWO:
                msg = "once"
                raise ValueError(msg)
    assert len(calls) == _TWO


async def test_class_form_as_decorator() -> None:
    """An instance can be called as a decorator."""
    policy = Retry.constant(
        "test", on=ValueError, attempts=_THREE, delay=_FAST_DELAY
    )
    calls: list[int] = []

    @policy
    async def fn() -> str:
        calls.append(1)
        if len(calls) < _TWO:
            msg = "once"
            raise ValueError(msg)
        return "ok"

    assert await fn() == "ok"


def test_class_form_decorator_on_sync_function() -> None:
    """A `Retry` instance can decorate a sync function."""
    policy = Retry.constant(
        "test", on=ValueError, attempts=_THREE, delay=_FAST_DELAY
    )
    calls: list[int] = []

    @policy
    def fn() -> str:
        calls.append(1)
        if len(calls) < _TWO:
            msg = "once"
            raise ValueError(msg)
        return "ok"

    assert fn() == "ok"
    assert len(calls) == _TWO


def test_class_form_sync_iterator() -> None:
    """An instance is also a sync iterator."""
    policy = Retry.constant(
        "test", on=ValueError, attempts=_THREE, delay=_FAST_DELAY
    )
    calls: list[int] = []
    for attempt in policy:
        with attempt:
            calls.append(1)
            if len(calls) < _TWO:
                msg = "once"
                raise ValueError(msg)
    assert len(calls) == _TWO


# --- Reconfigure -----------------------------------------------------------


async def test_reconfigure_changes_attempts() -> None:
    """Reconfigure publishes the new config to future loops."""
    policy = Retry.constant(
        "test", on=ValueError, attempts=_TWO, delay=_FAST_DELAY
    )
    new = policy.config.model_copy(update={"attempts": _FIVE})
    await policy.reconfigure(new)
    assert policy.config.attempts == _FIVE

    calls: list[int] = []
    succeed_after = _FOUR

    @policy
    async def fn() -> str:
        calls.append(1)
        if len(calls) < succeed_after:
            msg = "transient"
            raise ValueError(msg)
        return "ok"

    assert await fn() == "ok"
    assert len(calls) == succeed_after


async def test_reconfigure_does_not_affect_in_flight_loop() -> None:
    """An in-flight iterator keeps its snapshot of the config."""
    policy = Retry.constant(
        "test", on=ValueError, attempts=_TWO, delay=_FAST_DELAY
    )
    new = policy.config.model_copy(update={"attempts": _TEN})
    seen: list[int] = []
    with pytest.raises(ValueError, match="transient"):  # noqa: PT012
        async for attempt in policy:
            async with attempt:
                seen.append(attempt.number)
                if attempt.number == 1:
                    await policy.reconfigure(new)
                msg = "transient"
                raise ValueError(msg)
    assert seen == [1, _TWO]


# --- attempts=1 means no retry --------------------------------------------


async def test_attempts_one_runs_once_no_retry(
    fast_constant: ConstantBackoffConfig,
) -> None:
    """`attempts=1` means a single call with no retry."""
    calls: list[int] = []

    @retry(on=ValueError, attempts=1, backoff=fast_constant)
    async def fn() -> None:
        calls.append(1)
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        await fn()
    assert len(calls) == 1


# --- Env-driven configuration ---------------------------------------------


async def test_env_populates_attempts_and_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GREL_RETRY_{NAME}_ATTEMPTS` and `_ON` populate unset fields."""
    monkeypatch.setenv("GREL_RETRY_PAYMENTS_ATTEMPTS", "7")
    monkeypatch.setenv("GREL_RETRY_PAYMENTS_ON", "builtins.ValueError")
    policy = Retry("payments")  # type: ignore[call-arg]
    expected_attempts = 7
    assert policy.config.attempts == expected_attempts
    assert policy.config.on == (ValueError,)


async def test_env_backoff_via_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """`GREL_RETRY_{NAME}_BACKOFF` accepts a JSON object."""
    monkeypatch.setenv("GREL_RETRY_FOO_ATTEMPTS", "3")
    monkeypatch.setenv("GREL_RETRY_FOO_ON", "builtins.ValueError")
    monkeypatch.setenv(
        "GREL_RETRY_FOO_BACKOFF", '{"type":"constant","delay":2.5}'
    )
    policy = Retry("foo")  # type: ignore[call-arg]
    assert isinstance(policy.config.backoff, ConstantBackoffConfig)
    expected_delay = 2.5
    assert policy.config.backoff.delay == expected_delay


async def test_kwargs_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller kwargs win over env."""
    monkeypatch.setenv("GREL_RETRY_BAR_ATTEMPTS", "9")
    policy = Retry("bar", on=ValueError, attempts=_TWO)
    assert policy.config.attempts == _TWO


async def test_from_config_bypasses_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Retry.from_config()` ignores env even when set."""
    monkeypatch.setenv("GREL_RETRY_BAZ_ATTEMPTS", "9")
    cfg = RetryConfig(attempts=_TWO, on=(ValueError,))  # ty: ignore[missing-argument]
    policy = Retry.from_config("baz", cfg)
    assert policy.config.attempts == _TWO


# --- BaseException safety -------------------------------------------------


async def test_cancellederror_propagates_even_with_broad_filter(
    fast_constant: ConstantBackoffConfig,
) -> None:
    """`asyncio.CancelledError` propagates regardless of the filter."""
    calls: list[int] = []

    @retry(
        on=lambda exc: True,  # noqa: ARG005  # would match anything
        attempts=_FIVE,
        backoff=fast_constant,
    )
    async def fn() -> None:
        calls.append(1)
        raise _asyncio.CancelledError

    with pytest.raises(_asyncio.CancelledError):
        await fn()
    assert len(calls) == 1
