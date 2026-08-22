"""Stack resilience pattern."""

from __future__ import annotations

import functools
from inspect import (
    isasyncgenfunction,
    iscoroutinefunction,
    isgeneratorfunction,
)
from typing import TYPE_CHECKING, Annotated, Any, overload

from typing_extensions import Doc

from grelmicro._async import is_async_callable
from grelmicro._markers import is_scheduled
from grelmicro.resilience.bulkhead import Bulkhead
from grelmicro.resilience.circuitbreaker import (
    CircuitBreaker,
    _EventLoopEntryError,
)
from grelmicro.resilience.errors import CircuitBreakerError
from grelmicro.resilience.fallback import Fallback
from grelmicro.resilience.ratelimiter import RateLimiter, RateLimiterBinding
from grelmicro.resilience.retry import Retry
from grelmicro.resilience.timeout import Timeout

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

__all__ = ["Pattern", "Stack"]


type Pattern = (
    Bulkhead
    | CircuitBreaker
    | Fallback
    | RateLimiter
    | RateLimiterBinding
    | Retry
    | Timeout
)
"""One item `Stack(patterns=[...])` accepts.

Covers every resilience pattern that wraps a call, and the
`RateLimiterBinding` that `limiter(key=...)` returns. Name it to
annotate a list you build before passing it in:

```python
from grelmicro.resilience import Pattern, Retry, Stack

patterns: list[Pattern] = [Retry.exponential("recs", when=OSError)]
if settings.distributed:
    patterns.append(breaker)

recs = Stack("recs", patterns=patterns)
```
"""


_FALLBACK = "fallback"
_RETRY = "retry"
_CIRCUIT_BREAKER = "circuit_breaker"
_RATE_LIMITER = "rate_limiter"
_BULKHEAD = "bulkhead"
_TIMEOUT = "timeout"

_ORDER = (
    _FALLBACK,
    _RETRY,
    _CIRCUIT_BREAKER,
    _RATE_LIMITER,
    _BULKHEAD,
    _TIMEOUT,
)
"""The slots, outermost first. The call travels left to right."""

_SLOTS: dict[type, str] = {
    Fallback: _FALLBACK,
    Retry: _RETRY,
    CircuitBreaker: _CIRCUIT_BREAKER,
    RateLimiter: _RATE_LIMITER,
    RateLimiterBinding: _RATE_LIMITER,
    Bulkhead: _BULKHEAD,
    Timeout: _TIMEOUT,
}
"""Which slot each accepted type fills."""

_ASYNC_ONLY = (_RATE_LIMITER, _BULKHEAD, _TIMEOUT)
"""Slots whose pattern decorates async functions only."""

_LABELS = {
    _FALLBACK: "Fallback",
    _RETRY: "Retry",
    _CIRCUIT_BREAKER: "CircuitBreaker",
    _RATE_LIMITER: "RateLimiter",
    _BULKHEAD: "Bulkhead",
    _TIMEOUT: "Timeout",
}
"""What each slot is called in messages."""

_ELSEWHERE = {
    "grelmicro.resilience.shield._shield.Shield": (
        "Shield is a stack of its own: it already bundles a timeout, "
        "retries, and adaptive throttling behind one decorator. Use "
        "@shield on its own, or build a Stack from the patterns it "
        "would replace."
    ),
    "grelmicro.coordination.leaderelection.LeaderElection": (
        "LeaderElection runs as a service and decides which replica "
        "is leader. It does not wrap a call, so it has no place in "
        "the order. Gate the work on `leader.is_leader` instead."
    ),
    "grelmicro.coordination.lock.Lock": (
        "A Lock is held around a block and is acquired by key, so it "
        "does not wrap a call the way a pattern does. Take it inside "
        "the function the Stack decorates."
    ),
    "grelmicro.coordination.readwritelock.ReadWriteLock": (
        "A ReadWriteLock is held around a block and is acquired by "
        "key, so it does not wrap a call the way a pattern does. Take "
        "it inside the function the Stack decorates."
    ),
    "grelmicro.cache.ttl.TTLCache": (
        "A TTLCache stores results, it does not wrap a call. Put "
        "`@cached(cache)` above the Stack, so a hit answers without "
        "entering it."
    ),
}
"""Types users reach for that belong somewhere else, and where."""


class _Control(BaseException):
    """Carries one of the stack's own refusals past the pattern above it.

    A refusal by this stack's rate limiter, bulkhead, or circuit
    breaker is not an outcome of the call, because the call never
    happened. `CircuitBreaker` records no outcome for an exception that
    is not an `Exception`, and `Retry` never retries one, so wrapping
    the refusal in this carrier is what tells them so. It travels
    between two of the stack's own layers and never reaches user code.
    """

    def __init__(self, error: Exception) -> None:
        """Wrap the refusal to carry."""
        super().__init__(error)
        self.error = error
        """The refusal, re-raised once the pattern above has passed."""


def _carried(control: _Control) -> Exception:
    """Return the refusal the carrier held, and detach the carrier.

    The caller raises the result after its `except` block, so the
    carrier is no longer the exception being handled and the
    interpreter does not attach it as `__context__`.
    """
    error = control.error
    control.error = None  # type: ignore[assignment]  # ty: ignore[invalid-assignment]
    return error


def _as_coroutine_function[**P, R](
    fn: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Return `fn` as a coroutine function, adapting a callable object.

    Every pattern below picks its wrapper with `iscoroutinefunction`,
    which reads `False` for an object whose `__call__` is async.
    """
    if iscoroutinefunction(fn):
        return fn

    async def target(*args: P.args, **kwargs: P.kwargs) -> R:
        return await fn(*args, **kwargs)

    return target


def _is_generator(fn: object) -> bool:
    """Return whether calling `fn` builds a generator instead of running."""
    if isasyncgenfunction(fn) or isgeneratorfunction(fn):
        return True
    call = type(fn).__dict__.get("__call__")
    return call is not None and (
        isasyncgenfunction(call) or isgeneratorfunction(call)
    )


def _refuse_generator(fn: object, name: str) -> None:
    """Refuse a generator function, whose body no pattern would wrap.

    Raises:
        TypeError: If `fn` is a generator or async generator function.
    """
    if not _is_generator(fn):
        return
    msg = (
        f"Stack {name!r} does not wrap {_named(fn)}, because a "
        "generator runs its body while it is iterated and not when it "
        "is called, so every pattern would wrap building the generator "
        "and nothing else. Wrap the function that consumes it, or the "
        "call inside the body."
    )
    raise TypeError(msg)


def _named(fn: object) -> str:
    """Return the name of a decorated function, for a message."""
    return getattr(fn, "__qualname__", None) or repr(fn)


def _describe(item: Pattern) -> str:
    """Return the pattern as its type and name, for a message."""
    inner = item.limiter if isinstance(item, RateLimiterBinding) else item
    return f"{type(item).__name__}({inner.name!r})"


def _slot_of(item: object) -> str:
    """Return the slot `item` fills.

    Raises:
        TypeError: If `item` is not a pattern a Stack composes.
    """
    slot = _SLOTS.get(type(item))
    if slot is not None:
        return slot
    for accepted, name in _SLOTS.items():
        if isinstance(item, accepted):
            return name
    kind = item if isinstance(item, type) else type(item)
    known = _ELSEWHERE.get(f"{kind.__module__}.{kind.__qualname__}")
    detail = (
        known
        or "Stack composes patterns, and takes each one built: "
        "Fallback, Retry, CircuitBreaker, RateLimiter, Bulkhead, "
        "Timeout. Pass the pattern rather than its config, its class, "
        "or an already-decorated function."
    )
    msg = f"Stack(patterns=[...]) does not take {kind.__qualname__}. {detail}"
    raise TypeError(msg)


class Stack:
    """Resilience patterns applied in the safe order.

    Listed outside-in. The call travels left to right, and the result
    or the error travels back right to left:

    ```text
    Fallback → Retry → CircuitBreaker → RateLimiter → Bulkhead → Timeout → call
    ```

    A Stack takes the patterns in any order and applies them in that
    one, so the order is no longer something to get right at each call
    site. Stack the decorators by hand when you want a different order.

    Inside a Stack, a refusal by its own rate limiter, bulkhead, or
    circuit breaker is treated as what it is, a call that never
    happened:

    | Refusal | Inside a Stack |
    |---|---|
    | its `CircuitBreakerError` | its `Retry` never retries it |
    | its `RateLimitExceededError` | its `CircuitBreaker` records no outcome |
    | its `BulkheadFullError` | its `CircuitBreaker` records no outcome |

    Every other error reaches each pattern exactly as its own `when=`
    or `ignore_exceptions` decides.

    A stack whose patterns all take sync functions decorates one. A
    sync stack holding a `CircuitBreaker` runs from a worker thread,
    because the breaker reaches its backend through the event loop.

    Read more in the [Composing patterns](../resilience/composition.md)
    docs.
    """

    __slots__ = ("_binding", "_guard", "_members", "_name")

    def __init__(
        self,
        name: Annotated[
            str,
            Doc("The name of the stack. Used in its errors and its repr."),
        ],
        *,
        patterns: Annotated[
            Iterable[Pattern | None],
            Doc(
                """
                The patterns to apply, in any order. At most one of
                each, and at least one in total. A `None` entry is
                skipped, as in `Grelmicro(uses=[...])`, so a pattern
                that only applies to one deployment stays a plain
                expression: `patterns=[retrier, breaker if shared else
                None]`.
                """
            ),
        ],
    ) -> None:
        """Build a stack from the patterns it applies.

        Raises:
            TypeError: If `patterns` is one pattern rather than a list
                of them, or an item is not a pattern a Stack composes.
            ValueError: If two items fill the same slot, or no item is
                given.
        """
        if isinstance(patterns, (str, bytes, *_SLOTS)):
            msg = (
                f"Stack {name!r} takes a list of patterns, got "
                f"{type(patterns).__name__}. Wrap it: "
                "patterns=[the_pattern]."
            )
            raise TypeError(msg)
        members: dict[str, Pattern] = {}
        for item in patterns:
            if item is None:
                continue
            slot = _slot_of(item)
            if slot in members:
                msg = (
                    f"Stack {name!r} takes at most one {_LABELS[slot]}, "
                    f"got {_describe(members[slot])} and "
                    f"{_describe(item)}. Two of one pattern need an "
                    "order only you know, so stack them by hand."
                )
                raise ValueError(msg)
            members[slot] = item
        if not members:
            msg = (
                f"Stack {name!r} has no patterns. A stack of nothing "
                "would return the function unchanged, which a "
                "resilience stack must never do silently."
            )
            raise ValueError(msg)
        self._name = name
        self._members = members
        limiter = members.get(_RATE_LIMITER)
        self._binding = (
            RateLimiterBinding(limiter)
            if isinstance(limiter, RateLimiter)
            else limiter
            if isinstance(limiter, RateLimiterBinding)
            else None
        )
        self._guard = (
            _CIRCUIT_BREAKER in members
            and (_RATE_LIMITER in members or _BULKHEAD in members),
            _CIRCUIT_BREAKER in members and _RETRY in members,
        )

    @property
    def name(self) -> str:
        """The name of the stack."""
        return self._name

    @property
    def patterns(self) -> tuple[Pattern, ...]:
        """The patterns it applies, outermost first."""
        return tuple(
            self._members[slot] for slot in _ORDER if slot in self._members
        )

    def __repr__(self) -> str:
        """Return the stack as the chain a call travels."""
        chain = " → ".join(
            [_LABELS[slot] for slot in _ORDER if slot in self._members]
            + ["call"]
        )
        return f"<Stack {self._name!r} outside-in: {chain}>"

    @overload
    def __call__[**P, R](
        self, fn: Callable[P, Awaitable[R]], /
    ) -> Callable[P, Awaitable[R]]: ...

    @overload
    def __call__[**P, R](self, fn: Callable[P, R], /) -> Callable[P, R]: ...

    def __call__(
        self,
        fn: Annotated[
            Callable[..., Any],
            Doc("The function every pattern wraps."),
        ],
        /,
    ) -> Callable[..., Any]:
        """Decorate `fn` with every pattern, in the safe order.

        Raises:
            TypeError: If `fn` is a generator function, `fn` is sync
                and a pattern in the stack decorates async functions
                only, or `fn` is already registered as a task.
            ValueError: If a rate limiter key template names a
                parameter `fn` does not take.
        """
        _refuse_generator(fn, self._name)
        if is_scheduled(fn):
            msg = (
                f"{_named(fn)} is already registered as a task, so the "
                f"schedule holds it as it is and Stack {self._name!r} "
                "would only wrap direct calls. Put the task decorator "
                "on top, above the stack, so it registers the wrapped "
                "function."
            )
            raise TypeError(msg)
        if is_async_callable(fn):
            return functools.wraps(fn)(self._build_async(fn))
        blocking = self._async_only()
        if blocking:
            names = ", ".join(blocking)
            plural = len(blocking) > 1
            msg = (
                f"Stack {self._name!r} only decorates async functions, "
                f"because {names} {'do' if plural else 'does'}. Make "
                f"{_named(fn)} async, or drop "
                f"{'those patterns' if plural else 'that pattern'} "
                "from the stack."
            )
            raise TypeError(msg)
        return functools.wraps(fn)(self._build_sync(fn))

    async def run[R](
        self,
        fn: Annotated[
            Callable[..., Awaitable[R]],
            Doc("The async function to call under the stack."),
        ],
        /,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> R:
        """Call `fn(*args, **kwargs)` under every pattern, in the safe order.

        The imperative form of the decorator, for a call site that
        cannot be decorated. It runs under the same patterns, so the
        same breaker, bucket, and permits are shared.

        Raises:
            TypeError: If `fn` is a generator function or is not async.
            ValueError: If a rate limiter key template names a
                parameter `fn` does not take.
        """
        _refuse_generator(fn, self._name)
        if not is_async_callable(fn):
            blocking = self._async_only()
            makes = "make" if len(blocking) > 1 else "makes"
            advice = (
                f"{', '.join(blocking)} {makes} this stack async only, "
                "so make the call async."
                if blocking
                else "Use the decorator for a sync function."
            )
            msg = f"Stack.run only calls async functions, got {fn!r}. {advice}"
            raise TypeError(msg)
        return await self._build_async(fn)(*args, **kwargs)

    def _async_only(self) -> list[str]:
        """Return the patterns in this stack that refuse a sync function."""
        return [_LABELS[slot] for slot in _ASYNC_ONLY if slot in self._members]

    def _build_async[**P, R](
        self, fn: Callable[P, Awaitable[R]]
    ) -> Callable[P, Awaitable[R]]:
        """Wrap `fn` in every pattern, innermost first."""
        guard_refusal, guard_breaker = self._guard
        members = self._members
        call = _as_coroutine_function(fn)
        timeout = members.get(_TIMEOUT)
        if isinstance(timeout, Timeout):
            call = timeout(call)
        bulkhead = members.get(_BULKHEAD)
        if isinstance(bulkhead, Bulkhead):
            call = _bulkhead_layer(bulkhead, call, guard=guard_refusal)
        binding = self._binding
        if binding is not None:
            call = _limiter_layer(
                binding._admitter(fn),  # noqa: SLF001
                call,
                guard=guard_refusal,
            )
        breaker = members.get(_CIRCUIT_BREAKER)
        if isinstance(breaker, CircuitBreaker):
            call = _breaker_layer(
                breaker,
                call,
                unwrap=guard_refusal,
                guard=guard_breaker,
            )
        retry = members.get(_RETRY)
        if isinstance(retry, Retry):
            call = retry(call)
            if guard_breaker:
                call = _unwrap_layer(call)
        fallback = members.get(_FALLBACK)
        if isinstance(fallback, Fallback):
            call = fallback(call)
        return call

    def _build_sync[**P, R](self, fn: Callable[P, R]) -> Callable[P, R]:
        """Wrap a sync `fn` in every pattern, innermost first.

        Only `Fallback`, `Retry`, and `CircuitBreaker` reach here. The
        async-only patterns are refused before the chain is built.
        """
        _, guard_breaker = self._guard
        members = self._members
        call = fn
        breaker = members.get(_CIRCUIT_BREAKER)
        if isinstance(breaker, CircuitBreaker):
            call = _sync_breaker_layer(breaker, call, guard=guard_breaker)
        retry = members.get(_RETRY)
        if isinstance(retry, Retry):
            call = retry(call)
            if guard_breaker:
                call = _sync_unwrap_layer(call)
        fallback = members.get(_FALLBACK)
        if isinstance(fallback, Fallback):
            call = fallback(call)
        return call


def _bulkhead_layer[**P, R](
    bulkhead: Bulkhead,
    inner: Callable[P, Awaitable[R]],
    *,
    guard: bool,
) -> Callable[P, Awaitable[R]]:
    """Wrap `inner` in the bulkhead, hiding its own refusal when guarded."""
    if not guard:
        return bulkhead(inner)

    async def layer(*args: P.args, **kwargs: P.kwargs) -> R:
        admitted = False
        try:
            async with bulkhead:
                admitted = True
                return await inner(*args, **kwargs)
        except Exception as error:
            if admitted:
                raise
            raise _Control(error) from None

    return layer


def _limiter_layer[**P, R](
    admit: Callable[[tuple[Any, ...], dict[str, Any]], Awaitable[object]],
    inner: Callable[P, Awaitable[R]],
    *,
    guard: bool,
) -> Callable[P, Awaitable[R]]:
    """Consume tokens before `inner`, hiding the refusal when guarded."""

    async def layer(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            await admit(args, kwargs)
        except Exception as error:
            if not guard:
                raise
            raise _Control(error) from None
        return await inner(*args, **kwargs)

    return layer


def _breaker_layer[**P, R](
    breaker: CircuitBreaker,
    inner: Callable[P, Awaitable[R]],
    *,
    unwrap: bool,
    guard: bool,
) -> Callable[P, Awaitable[R]]:
    """Wrap `inner` in the breaker, and settle both sides of the guard.

    A refusal carried up from the rate limiter or the bulkhead has
    already passed the breaker unrecorded, so it is unwrapped here. The
    breaker's own refusal is wrapped, so the retry above never loops on
    an open circuit. Only a stack that holds one of those builds this
    layer, so a carrier reaching it is always one of its own.
    """
    if not (unwrap or guard):
        return breaker(inner)

    async def layer(*args: P.args, **kwargs: P.kwargs) -> R:
        admitted = False
        try:
            async with breaker:
                admitted = True
                return await inner(*args, **kwargs)
        except (CircuitBreakerError, _EventLoopEntryError) as error:
            if admitted or not guard:
                raise
            raise _Control(error) from None
        except _Control as control:
            refusal = _carried(control)
        raise refusal

    return layer


def _unwrap_layer[**P, R](
    inner: Callable[P, Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Re-raise a carried refusal once the retry above has passed it."""

    async def layer(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await inner(*args, **kwargs)
        except _Control as control:
            refusal = _carried(control)
        raise refusal

    return layer


def _sync_breaker_layer[**P, R](
    breaker: CircuitBreaker,
    inner: Callable[P, R],
    *,
    guard: bool,
) -> Callable[P, R]:
    """Wrap a sync `inner` in the breaker, hiding its own refusal."""
    if not guard:
        return breaker(inner)

    def layer(*args: P.args, **kwargs: P.kwargs) -> R:
        admitted = False
        try:
            with breaker.from_thread:
                admitted = True
                return inner(*args, **kwargs)
        except (CircuitBreakerError, _EventLoopEntryError) as error:
            if admitted:
                raise
            raise _Control(error) from None

    return layer


def _sync_unwrap_layer[**P, R](inner: Callable[P, R]) -> Callable[P, R]:
    """Re-raise a carried refusal once the sync retry above has passed it."""

    def layer(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return inner(*args, **kwargs)
        except _Control as control:
            refusal = _carried(control)
        raise refusal

    return layer
