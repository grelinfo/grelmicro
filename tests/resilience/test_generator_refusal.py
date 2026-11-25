"""Every resilience pattern answers a generator function the same way.

A generator runs its body while it is iterated, not when it is called,
so a pattern around the call would protect building the generator and
nothing else. The patterns that refuse one share one guard and one
message. `Retry` and `Fallback` warn until 0.47.0, then refuse.
"""

import re
import warnings
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest

from grelmicro._deprecation import (
    GENERATOR_FUNCTION,
    PENDING,
    generator_function,
)
from grelmicro._wrapping import (
    generator_message,
    is_generator,
    refuse_generator,
)
from grelmicro.errors import Grelmicro047Warning
from grelmicro.resilience import (
    Bulkhead,
    CircuitBreaker,
    Fallback,
    MemoryCircuitBreakerAdapter,
    MemoryRateLimiterAdapter,
    RateLimiter,
    Retry,
    Shield,
    Stack,
    Timeout,
    fallback,
    retry,
    shield,
)
from tests._future import assert_future_warning

pytestmark = [pytest.mark.timeout(5)]


async def async_gen() -> AsyncIterator[int]:
    """Yield one item, from an async generator function."""
    yield 1


def sync_gen() -> Iterator[int]:
    """Yield one item, from a generator function."""
    yield 1


class AsyncGenClient:
    """A callable object whose call builds an async generator."""

    async def __call__(self) -> AsyncIterator[int]:
        """Yield one item."""
        yield 1


class SyncGenClient:
    """A callable object whose call builds a generator."""

    def __call__(self) -> Iterator[int]:
        """Yield one item."""
        yield 1


def _generators() -> list[tuple[str, Callable[..., Any]]]:
    """Return every spelling of a generator function, with a label."""
    return [
        ("async-function", async_gen),
        ("sync-function", sync_gen),
        ("async-callable-object", AsyncGenClient()),
        ("sync-callable-object", SyncGenClient()),
    ]


def _limiter() -> RateLimiter:
    """Return a rate limiter on a memory backend."""
    return RateLimiter.token_bucket(
        "rl", capacity=1, refill_rate=1, backend=MemoryRateLimiterAdapter()
    )


def _refusing() -> list[tuple[str, str, Callable[[Any], Any]]]:
    """Return every pattern that refuses a generator function.

    Each entry is a label, the name the message opens with, and a way to
    apply the pattern.
    """
    bulkhead = Bulkhead("b", max_concurrent=1)
    timeout = Timeout("t", seconds=1)
    return [
        ("Stack", "Stack 's'", Stack("s", patterns=[Timeout("t", seconds=1)])),
        ("Bulkhead", "Bulkhead 'b'", bulkhead),
        ("Bulkhead._wrap", "Bulkhead 'b'", bulkhead._wrap),
        ("Timeout", "Timeout 't'", timeout),
        ("Timeout._wrap", "Timeout 't'", timeout._wrap),
        ("RateLimiter", "RateLimiter 'rl'", _limiter()),
        (
            "RateLimiterBinding",
            "RateLimiter 'rl'",
            _limiter()(key_template="k"),
        ),
        ("Shield", "Shield 'sh'", Shield.api("sh", when=TimeoutError)),
        ("@shield.api", "@shield.api", shield.api(when=TimeoutError)),
        (
            "@shield.internal",
            "@shield.internal",
            shield.internal(when=TimeoutError),
        ),
        ("@shield.slow", "@shield.slow", shield.slow(when=TimeoutError)),
    ]


@pytest.mark.parametrize(
    ("label", "by", "apply"),
    _refusing(),
    ids=[label for label, _, _ in _refusing()],
)
@pytest.mark.parametrize(
    ("kind", "target"),
    _generators(),
    ids=[kind for kind, _ in _generators()],
)
def test_every_refusing_pattern_names_the_generator(
    label: str,
    by: str,
    apply: Callable[[Any], Any],
    kind: str,
    target: Callable[..., Any],
) -> None:
    """The refusal opens with the pattern and explains the generator."""
    with pytest.raises(TypeError) as caught:
        apply(target)

    assert str(caught.value) == generator_message(target, by)

    assert label
    assert kind


async def test_stack_run_refuses_with_the_shared_message() -> None:
    """`Stack.run` refuses what the decorator refuses."""
    stack = Stack("s", patterns=[Timeout("t", seconds=1)])

    with pytest.raises(TypeError) as caught:
        await stack.run(async_gen)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    assert str(caught.value) == generator_message(async_gen, "Stack 's'")


@pytest.mark.parametrize(
    ("kind", "target"),
    _generators(),
    ids=[kind for kind, _ in _generators()],
)
async def test_shield_run_refuses_with_the_shared_message(
    kind: str, target: Callable[..., Any]
) -> None:
    """`Shield.run` refuses a generator rather than calling it."""
    instance = Shield.api("sh", when=TimeoutError)

    with pytest.raises(TypeError) as caught:
        await instance.run(target)

    assert str(caught.value) == generator_message(target, "Shield 'sh'")

    assert kind


def test_the_message_tells_what_to_wrap_instead() -> None:
    """The refusal names the function and where the pattern belongs."""
    with pytest.raises(TypeError) as caught:
        refuse_generator(async_gen, "Retry 'db'")

    assert str(caught.value) == (
        "Retry 'db' does not wrap async_gen, because a generator runs its "
        "body while it is iterated and not when it is called, so Retry "
        "'db' would only wrap building the generator. Wrap the function "
        "that consumes it, or the call inside the body."
    )


async def coroutine_function() -> int:
    """Return one item, from a coroutine function."""
    return 1


class AsyncClient:
    """A callable object whose call is a coroutine."""

    async def __call__(self) -> int:
        """Return one item."""
        return 1


@pytest.mark.parametrize(
    "target",
    [coroutine_function, AsyncClient(), len, object()],
    ids=["coroutine-function", "async-callable-object", "builtin", "object"],
)
def test_a_function_that_runs_when_called_is_left_alone(
    target: object,
) -> None:
    """Only a generator is refused."""
    assert not is_generator(target)
    refuse_generator(target, "Retry 'db'")


@pytest.mark.parametrize(
    ("kind", "target"),
    _generators(),
    ids=[kind for kind, _ in _generators()],
)
def test_every_spelling_of_a_generator_is_detected(
    kind: str, target: Callable[..., Any]
) -> None:
    """A plain function and a callable object read the same."""
    assert is_generator(target)
    assert kind


def _async_generators() -> list[tuple[str, Callable[..., Any]]]:
    """Return every spelling of an async generator function, with a label."""
    return [(kind, target) for kind, target in _generators() if "async" in kind]


def _sync_generators() -> list[tuple[str, Callable[..., Any]]]:
    """Return every spelling of a sync generator function, with a label."""
    return [
        (kind, target)
        for kind, target in _generators()
        if kind.startswith("sync-")
    ]


def _breakers() -> list[tuple[str, str, Callable[[Any], Any]]]:
    """Return a circuit breaker and one of its keyed circuits."""
    breaker = CircuitBreaker("cb", backend=MemoryCircuitBreakerAdapter())
    return [
        ("CircuitBreaker", "CircuitBreaker 'cb'", breaker),
        ("CircuitBreaker.keyed", "CircuitBreaker 'cb:k'", breaker.keyed("k")),
    ]


@pytest.mark.parametrize(
    ("label", "by", "apply"),
    _breakers(),
    ids=[label for label, _, _ in _breakers()],
)
@pytest.mark.parametrize(
    ("kind", "target"),
    _async_generators(),
    ids=[kind for kind, _ in _async_generators()],
)
def test_a_circuit_breaker_refuses_an_async_generator(
    label: str,
    by: str,
    apply: Callable[[Any], Any],
    kind: str,
    target: Callable[..., Any],
) -> None:
    """Every call to one already failed, so it is refused where decorated."""
    with pytest.raises(TypeError) as caught:
        apply(target)

    assert str(caught.value) == generator_message(target, by)
    assert label
    assert kind


@pytest.mark.parametrize(
    ("label", "by", "apply"),
    _breakers(),
    ids=[label for label, _, _ in _breakers()],
)
@pytest.mark.parametrize(
    ("kind", "target"),
    _sync_generators(),
    ids=[kind for kind, _ in _sync_generators()],
)
def test_a_circuit_breaker_warns_on_a_sync_generator_until_0_47(
    label: str,
    by: str,
    apply: Callable[[Any], Any],
    kind: str,
    target: Callable[..., Any],
) -> None:
    """A sync generator is still wrapped, and the decorator warns once."""
    assert_future_warning(
        generator_function(target, by),
        default=lambda: apply(target),
        explicit=lambda: apply(sync_function),
    )
    assert label
    assert kind


def sync_function() -> int:
    """Return one item, from a plain function."""
    return 1


def _warning() -> list[tuple[str, str, Callable[[Any], Any]]]:
    """Return every pattern that warns on a generator function until 0.47.0.

    Each entry is a label, the name the message opens with, and a way to
    apply the pattern.
    """
    return [
        ("Retry", "Retry 'r'", Retry("r", when=Exception)),
        ("@retry", "@retry", retry(when=Exception)),
        ("@retry.constant", "@retry", retry.constant(when=Exception)),
        ("@retry.exponential", "@retry", retry.exponential(when=Exception)),
        ("Fallback", "Fallback 'f'", Fallback("f", when=Exception, default=0)),
        ("@fallback", "@fallback", fallback(when=Exception, default=0)),
    ]


@pytest.mark.parametrize(
    ("label", "by", "apply"),
    _warning(),
    ids=[label for label, _, _ in _warning()],
)
@pytest.mark.parametrize(
    ("kind", "target"),
    _generators(),
    ids=[kind for kind, _ in _generators()],
)
def test_retry_and_fallback_warn_on_a_generator_until_0_47(
    label: str,
    by: str,
    apply: Callable[[Any], Any],
    kind: str,
    target: Callable[..., Any],
) -> None:
    """The generator is still wrapped, and the decorator warns once."""
    assert_future_warning(
        generator_function(target, by),
        default=lambda: apply(target),
        explicit=lambda: apply(coroutine_function),
    )
    assert label
    assert kind


@pytest.mark.parametrize(
    ("label", "by", "apply"),
    _warning(),
    ids=[label for label, _, _ in _warning()],
)
def test_the_warning_points_at_the_decorated_line(
    label: str, by: str, apply: Callable[[Any], Any]
) -> None:
    """The warning reports the line that applied the decorator."""
    with pytest.warns(Grelmicro047Warning) as caught:
        apply(async_gen)

    assert caught[0].filename == __file__
    assert by in str(caught[0].message)
    assert label


def test_the_warning_says_what_changes_and_what_to_wrap() -> None:
    """The message names the pattern, the function, the release and the fix."""
    assert generator_function(async_gen, "Retry 'db'").message == (
        "Retry 'db' refuses the generator function async_gen with "
        "`TypeError` from 0.47.0. A generator runs its body while it is "
        "iterated and not when it is called, so Retry 'db' only wraps "
        "building the generator and protects nothing. Wrap the function "
        "that consumes it, or the call inside the body."
    )


def test_the_change_is_pending_for_0_47() -> None:
    """The warning is listed, and its category is the 0.47.0 one."""
    assert GENERATOR_FUNCTION in PENDING
    assert GENERATOR_FUNCTION.switch_in == "0.47.0"
    assert GENERATOR_FUNCTION.category is Grelmicro047Warning
    assert Grelmicro047Warning.switch_in == "0.47.0"


def test_a_stack_with_retry_refuses_rather_than_warns() -> None:
    """A Stack refuses a generator, whatever patterns it holds."""
    stack = Stack("s", patterns=[Retry("r", when=Exception)])

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(TypeError, match=re.escape("Stack 's' does not")):
            stack(async_gen)
