"""Tests for the Stack composition pattern."""

import asyncio
import gc
import weakref
from dataclasses import dataclass

import pytest

from grelmicro.cache import TTLCache
from grelmicro.coordination import LeaderElection, Lock, ReadWriteLock
from grelmicro.resilience import (
    Bulkhead,
    BulkheadFullError,
    CircuitBreaker,
    CircuitBreakerError,
    Fallback,
    MemoryCircuitBreakerAdapter,
    MemoryRateLimiterAdapter,
    RateLimiter,
    RateLimiterBinding,
    RateLimitExceededError,
    Retry,
    Shield,
    Stack,
    Timeout,
)

pytestmark = [pytest.mark.timeout(5)]

ATTEMPTS = 3
CAPACITY = 2
ERROR_THRESHOLD = 2
DELAY = 0.001
TIMEOUT = 5.0
DOUBLE = 21
DOUBLED = 42
SLOW_REFILL = 0.0001
RESET_TIMEOUT = 30.0


def a_retry(name: str = "recs") -> Retry:
    """Return a retry that retries anything, quickly."""
    return Retry.constant(name, when=Exception, attempts=ATTEMPTS, delay=DELAY)


def a_fallback(name: str = "recs") -> Fallback:
    """Return a fallback that catches anything."""
    return Fallback(name, when=Exception, default="degraded")


# --- Construction ---


def test_applies_the_order_whatever_the_listing_order() -> None:
    """Patterns listed in any order are applied outside-in."""
    listed = [
        Timeout("recs", seconds=TIMEOUT),
        a_fallback(),
        Bulkhead("recs", max_concurrent=1),
        a_retry(),
        CircuitBreaker("recs"),
    ]
    stack = Stack("recs", patterns=listed)
    assert [type(p).__name__ for p in stack.patterns] == [
        "Fallback",
        "Retry",
        "CircuitBreaker",
        "Bulkhead",
        "Timeout",
    ]


def test_repr_anchors_the_chain_on_the_call() -> None:
    """The repr names the direction and ends at the call."""
    stack = Stack("recs", patterns=[a_retry(), Timeout("recs", seconds=1.0)])
    assert repr(stack) == ("<Stack 'recs' outside-in: Retry → Timeout → call>")


def test_name_is_exposed() -> None:
    """`name` returns the name the stack was built with."""
    assert Stack("recs", patterns=[a_retry()]).name == "recs"


def test_none_entries_are_skipped() -> None:
    """A `None` entry is skipped, as in `Grelmicro(uses=[...])`."""
    stack = Stack("recs", patterns=[a_retry(), None])
    assert len(stack.patterns) == 1


def test_a_subclass_fills_its_parent_slot() -> None:
    """A pattern subclass is accepted in the slot its parent fills."""

    class SlowRetry(Retry):
        """A retry that is still a retry."""

    stack = Stack(
        "recs",
        patterns=[
            SlowRetry.constant(
                "recs", when=Exception, attempts=ATTEMPTS, delay=DELAY
            )
        ],
    )
    assert isinstance(stack.patterns[0], SlowRetry)


def test_two_of_one_pattern_are_refused() -> None:
    """Two patterns filling one slot need an order only the caller knows."""
    with pytest.raises(ValueError, match="at most one Retry"):
        Stack("recs", patterns=[a_retry("one"), a_retry("two")])


def test_a_bound_limiter_and_a_bare_one_are_two_limiters() -> None:
    """A binding fills the same slot as the limiter it wraps."""
    limiter = RateLimiter.token_bucket("recs", capacity=CAPACITY, refill_rate=1)
    with pytest.raises(ValueError, match="at most one RateLimiter"):
        Stack("recs", patterns=[limiter, limiter(key="u:{uid}")])


def test_an_empty_stack_is_refused() -> None:
    """A stack of nothing would silently return the function unchanged."""
    with pytest.raises(ValueError, match="has no patterns"):
        Stack("recs", patterns=[])


def test_a_stack_of_only_none_is_refused() -> None:
    """Every entry skipped leaves nothing to apply."""
    with pytest.raises(ValueError, match="has no patterns"):
        Stack("recs", patterns=[None, None])


def test_something_that_is_not_a_pattern_is_refused() -> None:
    """An unknown item names what a stack composes."""
    with pytest.raises(TypeError, match="does not take str"):
        Stack("recs", patterns=["nope"])  # type: ignore[list-item]  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    ("item", "match"),
    [
        (Shield("sh"), "Shield is a stack of its own"),
        (LeaderElection("le"), "runs as a service"),
        (Lock("lk"), "held around a block"),
        (ReadWriteLock("rw"), "held around a block"),
        (TTLCache(), "stores results"),
    ],
    ids=["shield", "leader", "lock", "readwritelock", "cache"],
)
def test_something_that_belongs_elsewhere_says_where(
    item: object, match: str
) -> None:
    """A near miss is refused with where the thing actually goes."""
    with pytest.raises(TypeError, match=match):
        Stack("recs", patterns=[item])  # type: ignore[list-item]  # ty: ignore[invalid-argument-type]


# --- Decoration ---


async def test_decorates_an_async_function() -> None:
    """The decorator wraps and keeps the function's identity."""
    stack = Stack("recs", patterns=[a_retry()])

    @stack
    async def work(value: int) -> int:
        return value * 2

    assert work.__name__ == "work"
    assert await work(DOUBLE) == DOUBLED


def test_decorates_a_sync_function_when_every_pattern_can() -> None:
    """A stack of sync-capable patterns decorates a sync function."""
    stack = Stack("recs", patterns=[a_retry(), a_fallback()])
    calls = 0

    @stack
    def work() -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError

    assert work() == "degraded"
    assert calls == ATTEMPTS


@pytest.mark.parametrize(
    ("pattern", "label"),
    [
        (Timeout("recs", seconds=TIMEOUT), "Timeout"),
        (Bulkhead("recs", max_concurrent=1), "Bulkhead"),
        (
            RateLimiter.token_bucket("recs", capacity=CAPACITY, refill_rate=1),
            "RateLimiter",
        ),
    ],
    ids=["timeout", "bulkhead", "ratelimiter"],
)
def test_a_sync_function_is_refused_at_decoration(
    pattern: object, label: str
) -> None:
    """An async-only pattern refuses a sync function where it is written."""
    stack = Stack("recs", patterns=[pattern])  # type: ignore[list-item]  # ty: ignore[invalid-argument-type]

    with pytest.raises(TypeError, match=f"because {label} does"):

        @stack
        def work() -> str:
            return "never"


# --- Imperative form ---


async def test_run_calls_under_the_same_patterns() -> None:
    """`run` applies the stack to a call that cannot be decorated."""
    stack = Stack("recs", patterns=[a_retry()])

    async def work(value: int) -> int:
        return value * 2

    assert await stack.run(work, DOUBLE) == DOUBLED


async def test_run_refuses_a_sync_function() -> None:
    """`run` is the async form, and says so."""
    stack = Stack("recs", patterns=[a_retry()])

    with pytest.raises(TypeError, match="only calls async functions"):
        await stack.run(lambda: None)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


# --- The coordination contract ---


async def test_its_open_breaker_is_never_retried() -> None:
    """An open circuit is a decision, so the retry above stops at once."""
    async with MemoryCircuitBreakerAdapter() as backend:
        breaker = CircuitBreaker.consecutive_count(
            "recs",
            error_threshold=ERROR_THRESHOLD,
            backend=backend,
            reset_timeout=RESET_TIMEOUT,
        )
        stack = Stack("recs", patterns=[a_retry(), breaker])
        calls = 0

        @stack
        async def work() -> str:
            nonlocal calls
            calls += 1
            raise RuntimeError

        with pytest.raises(CircuitBreakerError):
            await work()
        assert calls == ERROR_THRESHOLD

        calls = 0
        with pytest.raises(CircuitBreakerError):
            await work()
        assert calls == 0


async def test_the_fallback_still_sees_the_open_breaker() -> None:
    """The refusal reaches the fallback as itself, not as a carrier."""
    async with MemoryCircuitBreakerAdapter() as backend:
        breaker = CircuitBreaker.consecutive_count(
            "recs",
            error_threshold=1,
            backend=backend,
            reset_timeout=RESET_TIMEOUT,
        )
        seen: list[BaseException] = []
        stack = Stack(
            "recs",
            patterns=[
                a_retry(),
                breaker,
                Fallback(
                    "recs",
                    when=Exception,
                    factory=lambda error: seen.append(error) or "degraded",
                ),
            ],
        )

        @stack
        async def work() -> str:
            raise RuntimeError

        assert await work() == "degraded"
        assert await work() == "degraded"
        assert isinstance(seen[-1], CircuitBreakerError)


async def test_its_rate_limit_refusal_is_not_a_breaker_failure() -> None:
    """A call that never happened cannot have failed."""
    async with (
        MemoryCircuitBreakerAdapter() as cb_backend,
        MemoryRateLimiterAdapter() as rl_backend,
    ):
        breaker = CircuitBreaker.consecutive_count(
            "recs", error_threshold=1, backend=cb_backend
        )
        limiter = RateLimiter.token_bucket(
            "recs",
            capacity=CAPACITY,
            refill_rate=SLOW_REFILL,
            backend=rl_backend,
        )
        stack = Stack("recs", patterns=[breaker, limiter])

        @stack
        async def work() -> str:
            return "ok"

        assert await work() == "ok"
        assert await work() == "ok"
        with pytest.raises(RateLimitExceededError):
            await work()

        metrics = breaker.metrics()
        assert metrics.total_success_count == CAPACITY
        assert metrics.total_error_count == 0


async def test_its_bulkhead_refusal_is_not_a_breaker_failure() -> None:
    """A permit that was never free cannot have failed the dependency."""
    async with MemoryCircuitBreakerAdapter() as backend:
        breaker = CircuitBreaker.consecutive_count(
            "recs", error_threshold=1, backend=backend
        )
        stack = Stack(
            "recs",
            patterns=[breaker, Bulkhead("recs", max_concurrent=1)],
        )
        release = asyncio.Event()

        @stack
        async def work() -> str:
            await release.wait()
            return "ok"

        holding = asyncio.create_task(work())
        await asyncio.sleep(0)
        with pytest.raises(BulkheadFullError):
            await work()
        release.set()
        assert await holding == "ok"

        metrics = breaker.metrics()
        assert metrics.total_error_count == 0


async def test_the_retry_still_sees_a_rate_limit_refusal() -> None:
    """The refusal is hidden from the breaker, not from the retry."""
    async with (
        MemoryCircuitBreakerAdapter() as cb_backend,
        MemoryRateLimiterAdapter() as rl_backend,
    ):
        limiter = RateLimiter.token_bucket(
            "recs", capacity=1, refill_rate=SLOW_REFILL, backend=rl_backend
        )
        stack = Stack(
            "recs",
            patterns=[
                a_retry(),
                CircuitBreaker.consecutive_count(
                    "recs", error_threshold=ERROR_THRESHOLD, backend=cb_backend
                ),
                limiter,
            ],
        )
        calls = 0

        @stack
        async def work() -> str:
            nonlocal calls
            calls += 1
            return "ok"

        assert await work() == "ok"
        with pytest.raises(RateLimitExceededError):
            await work()
        assert calls == 1


async def test_without_a_breaker_a_refusal_travels_plainly() -> None:
    """Nothing is carried when there is no breaker to hide it from."""
    async with MemoryRateLimiterAdapter() as backend:
        limiter = RateLimiter.token_bucket(
            "recs", capacity=1, refill_rate=SLOW_REFILL, backend=backend
        )
        stack = Stack("recs", patterns=[limiter])

        @stack
        async def work() -> str:
            return "ok"

        assert await work() == "ok"
        with pytest.raises(RateLimitExceededError):
            await work()


async def test_without_a_breaker_a_full_bulkhead_travels_plainly() -> None:
    """The bulkhead layer stays a plain wrapper without a breaker."""
    stack = Stack("recs", patterns=[Bulkhead("recs", max_concurrent=1)])
    release = asyncio.Event()

    @stack
    async def work() -> str:
        await release.wait()
        return "ok"

    holding = asyncio.create_task(work())
    await asyncio.sleep(0)
    with pytest.raises(BulkheadFullError):
        await work()
    release.set()
    assert await holding == "ok"


async def test_a_refusal_raised_by_the_call_is_the_calls_own() -> None:
    """A refusal from inside the function is a dependency outcome."""
    async with MemoryCircuitBreakerAdapter() as backend:
        breaker = CircuitBreaker.consecutive_count(
            "recs", error_threshold=1, backend=backend
        )
        stack = Stack(
            "recs",
            patterns=[breaker, Bulkhead("recs", max_concurrent=1)],
        )

        @stack
        async def work() -> str:
            raise BulkheadFullError(name="somewhere-else", max_concurrent=1)

        with pytest.raises(BulkheadFullError):
            await work()
        assert breaker.metrics().total_error_count == 1


async def test_a_breaker_error_raised_by_the_call_reaches_the_retry() -> None:
    """A breaker deeper in the call is not this stack's breaker."""
    async with MemoryCircuitBreakerAdapter() as backend:
        breaker = CircuitBreaker.consecutive_count(
            "recs",
            error_threshold=ERROR_THRESHOLD * ATTEMPTS,
            backend=backend,
        )
        stack = Stack("recs", patterns=[a_retry(), breaker])
        calls = 0

        @stack
        async def work() -> str:
            nonlocal calls
            calls += 1
            raise CircuitBreakerError(name="somewhere-else")

        with pytest.raises(CircuitBreakerError):
            await work()
        assert calls == ATTEMPTS


async def test_a_breaker_without_a_retry_needs_no_carrier() -> None:
    """The breaker layer stays a plain wrapper when nothing guards it."""
    async with MemoryCircuitBreakerAdapter() as backend:
        breaker = CircuitBreaker.consecutive_count(
            "recs", error_threshold=1, backend=backend
        )
        stack = Stack("recs", patterns=[breaker])

        @stack
        async def work() -> str:
            raise RuntimeError

        with pytest.raises(RuntimeError):
            await work()
        with pytest.raises(CircuitBreakerError):
            await work()


def test_the_sync_open_breaker_is_never_retried() -> None:
    """The contract holds on the sync path too."""

    async def scenario() -> tuple[int, int]:
        async with MemoryCircuitBreakerAdapter() as backend:
            breaker = CircuitBreaker.consecutive_count(
                "recs",
                error_threshold=ERROR_THRESHOLD,
                backend=backend,
                reset_timeout=RESET_TIMEOUT,
            )
            stack = Stack("recs", patterns=[a_retry(), breaker])
            calls = 0

            @stack
            def work() -> str:
                nonlocal calls
                calls += 1
                raise RuntimeError

            def first() -> int:
                nonlocal calls
                calls = 0
                with pytest.raises(CircuitBreakerError):
                    work()
                return calls

            def second() -> int:
                nonlocal calls
                calls = 0
                with pytest.raises(CircuitBreakerError):
                    work()
                return calls

            return await asyncio.to_thread(first), await asyncio.to_thread(
                second
            )

    opened, refused = asyncio.run(scenario())
    assert opened == ERROR_THRESHOLD
    assert refused == 0


def test_a_sync_breaker_error_from_the_call_reaches_the_retry() -> None:
    """A sync refusal from inside the function is not this stack's."""

    async def scenario() -> int:
        async with MemoryCircuitBreakerAdapter() as backend:
            breaker = CircuitBreaker.consecutive_count(
                "recs",
                error_threshold=ERROR_THRESHOLD * ATTEMPTS,
                backend=backend,
            )
            stack = Stack("recs", patterns=[a_retry(), breaker])
            calls = 0

            @stack
            def work() -> str:
                nonlocal calls
                calls += 1
                raise CircuitBreakerError(name="somewhere-else")

            def run() -> int:
                with pytest.raises(CircuitBreakerError):
                    work()
                return calls

            return await asyncio.to_thread(run)

    assert asyncio.run(scenario()) == ATTEMPTS


def test_a_sync_breaker_without_a_retry_needs_no_carrier() -> None:
    """The sync breaker layer stays a plain wrapper when nothing guards it."""

    async def scenario() -> None:
        async with MemoryCircuitBreakerAdapter() as backend:
            breaker = CircuitBreaker.consecutive_count(
                "recs", error_threshold=1, backend=backend
            )
            stack = Stack("recs", patterns=[breaker])

            @stack
            def work() -> str:
                raise RuntimeError

            def run() -> None:
                with pytest.raises(RuntimeError):
                    work()
                with pytest.raises(CircuitBreakerError):
                    work()

            await asyncio.to_thread(run)

    asyncio.run(scenario())


# --- Order semantics ---


async def test_the_timeout_bounds_one_attempt_not_the_whole_retry() -> None:
    """The timeout is innermost, so each attempt gets its own deadline."""
    stack = Stack(
        "recs",
        patterns=[a_retry(), Timeout("recs", seconds=DELAY)],
    )
    calls = 0

    @stack
    async def work() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(TIMEOUT)
        return "never"

    with pytest.raises(TimeoutError):
        await work()
    assert calls == ATTEMPTS


async def test_a_bound_limiter_meters_by_its_key() -> None:
    """A binding in the stack keys the bucket from the call's arguments."""
    async with MemoryRateLimiterAdapter() as backend:
        limiter = RateLimiter.token_bucket(
            "recs", capacity=1, refill_rate=SLOW_REFILL, backend=backend
        )
        binding = limiter(key="u:{user_id}")
        assert isinstance(binding, RateLimiterBinding)
        stack = Stack("recs", patterns=[binding])

        @stack
        async def work(user_id: int) -> int:
            return user_id

        assert await work(1) == 1
        assert await work(2) == 2  # noqa: PLR2004
        with pytest.raises(RateLimitExceededError):
            await work(1)


# --- Callable objects and reuse ---


async def test_decorates_a_callable_object_with_an_async_call() -> None:
    """An object whose `__call__` is async is an async function here."""

    class Client:
        """A callable that is async without being a coroutine function."""

        async def __call__(self, value: int) -> int:
            return value * 2

    stack = Stack(
        "recs", patterns=[a_retry(), Timeout("recs", seconds=TIMEOUT)]
    )
    wrapped = stack(Client())

    assert await wrapped(DOUBLE) == DOUBLED


async def test_every_pattern_sees_a_callable_object_as_async() -> None:
    """The patterns below wrap the call, they do not skip it."""

    class Client:
        """A callable object that fails until its last attempt."""

        def __init__(self) -> None:
            self.calls = 0

        async def __call__(self) -> str:
            self.calls += 1
            if self.calls < ATTEMPTS:
                raise RuntimeError
            return "ok"

    client = Client()
    stack = Stack("recs", patterns=[a_retry()])

    assert await stack(client)() == "ok"
    assert client.calls == ATTEMPTS


async def test_run_calls_a_callable_object() -> None:
    """`run` accepts what the decorator accepts."""

    class Client:
        """A callable that is async without being a coroutine function."""

        async def __call__(self, value: int) -> int:
            return value * 2

    stack = Stack("recs", patterns=[a_retry()])

    assert await stack.run(Client(), DOUBLE) == DOUBLED


async def test_run_calls_the_object_it_was_given() -> None:
    """Two equal but distinct clients are two different calls."""

    @dataclass(frozen=True)
    class Client:
        """A client that compares equal to another with the same url."""

        url: str

        async def __call__(self) -> int:
            return id(self)

    stack = Stack("recs", patterns=[a_retry()])
    first, second = Client("http://a"), Client("http://a")

    assert first == second
    assert await stack.run(first) == id(first)
    assert await stack.run(second) == id(second)


async def test_run_calls_a_client_that_takes_no_weak_reference() -> None:
    """A `__slots__` client is callable, so `run` calls it."""

    class Client:
        """A callable that cannot be weakly referenced."""

        __slots__ = ()

        async def __call__(self, value: int) -> int:
            return value * 2

    stack = Stack("recs", patterns=[a_retry()])

    assert await stack.run(Client(), DOUBLE) == DOUBLED


async def test_run_holds_on_to_nothing() -> None:
    """A per-call target is collected once the call is over."""
    stack = Stack("recs", patterns=[a_retry()])
    refs = []

    for offset in range(3):

        async def work(value: int, offset: int = offset) -> int:
            return value + offset

        refs.append(weakref.ref(work))
        await stack.run(work, 1)
        del work

    gc.collect()
    assert [ref() for ref in refs] == [None, None, None]


def test_a_sync_breaker_entered_from_the_event_loop_is_refused() -> None:
    """A sync stack with a breaker cannot run on the loop thread."""

    async def scenario() -> None:
        async with MemoryCircuitBreakerAdapter() as backend:
            stack = Stack(
                "recs",
                patterns=[
                    a_retry(),
                    CircuitBreaker.consecutive_count(
                        "recs", error_threshold=1, backend=backend
                    ),
                ],
            )

            @stack
            def work() -> str:
                return "never"

            with pytest.raises(
                RuntimeError, match="the event loop its backend runs on"
            ):
                work()

    asyncio.run(scenario())


def test_the_sync_refusal_counts_the_patterns_it_names() -> None:
    """One pattern reads as one, several read as several."""
    one = Stack("recs", patterns=[Timeout("recs", seconds=TIMEOUT)])
    with pytest.raises(TypeError, match=r"Timeout does\. .* that pattern"):
        one(lambda: None)

    several = Stack(
        "recs",
        patterns=[
            Bulkhead("recs", max_concurrent=1),
            Timeout("recs", seconds=TIMEOUT),
        ],
    )
    with pytest.raises(
        TypeError, match=r"Bulkhead, Timeout do\. .* those patterns"
    ):
        several(lambda: None)


async def test_run_does_not_advise_a_decorator_that_would_refuse_too() -> None:
    """The advice holds for the stack it is given."""
    async_only = Stack("recs", patterns=[Timeout("recs", seconds=TIMEOUT)])
    with pytest.raises(TypeError, match="make the call async"):
        await async_only.run(lambda: None)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    sync_capable = Stack("recs", patterns=[a_retry()])
    with pytest.raises(TypeError, match="Use the decorator"):
        await sync_capable.run(lambda: None)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
