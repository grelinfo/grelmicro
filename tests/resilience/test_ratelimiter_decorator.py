"""Tests for the RateLimiter decorator and its binding."""

from typing import Any

import pytest

from grelmicro.resilience import (
    MemoryRateLimiterAdapter,
    RateLimiter,
    RateLimiterBinding,
    RateLimitExceededError,
)

pytestmark = [pytest.mark.timeout(5)]

CAPACITY = 2
SLOW_REFILL = 0.0001
FAST_REFILL = 1000.0
MAX_WAIT = 1.0
USER = 7
OTHER_USER = 8


async def test_bare_decorator_meters_the_whole_function() -> None:
    """`@limiter` meters every call under the limiter's default bucket."""
    async with MemoryRateLimiterAdapter() as backend:
        limiter = RateLimiter.token_bucket(
            "api", capacity=1, refill_rate=SLOW_REFILL, backend=backend
        )

        @limiter
        async def work() -> str:
            return "ok"

        assert await work() == "ok"
        with pytest.raises(RateLimitExceededError):
            await work()


async def test_a_key_template_meters_per_argument() -> None:
    """A template renders the bucket key from the call's arguments."""
    async with MemoryRateLimiterAdapter() as backend:
        limiter = RateLimiter.token_bucket(
            "api", capacity=1, refill_rate=SLOW_REFILL, backend=backend
        )

        @limiter(key="user:{user_id}")
        async def work(user_id: int) -> int:
            return user_id

        assert await work(USER) == USER
        assert await work(OTHER_USER) == OTHER_USER
        with pytest.raises(RateLimitExceededError):
            await work(USER)


async def test_a_key_template_reads_a_default_argument() -> None:
    """A parameter left at its default still renders."""
    async with MemoryRateLimiterAdapter() as backend:
        limiter = RateLimiter.token_bucket(
            "api", capacity=1, refill_rate=SLOW_REFILL, backend=backend
        )

        @limiter(key="user:{user_id}")
        async def work(user_id: int = USER) -> int:
            return user_id

        assert await work() == USER
        with pytest.raises(RateLimitExceededError):
            await work(USER)


async def test_a_literal_key_meters_every_call_together() -> None:
    """A key with no placeholder is used as it is written."""
    async with MemoryRateLimiterAdapter() as backend:
        limiter = RateLimiter.token_bucket(
            "api", capacity=1, refill_rate=SLOW_REFILL, backend=backend
        )

        @limiter(key="shared")
        async def work(user_id: int) -> int:
            return user_id

        assert await work(USER) == USER
        with pytest.raises(RateLimitExceededError):
            await work(OTHER_USER)


async def test_a_key_maker_computes_the_key() -> None:
    """`key_maker` receives the function and the call's arguments."""
    async with MemoryRateLimiterAdapter() as backend:
        limiter = RateLimiter.token_bucket(
            "api", capacity=1, refill_rate=SLOW_REFILL, backend=backend
        )
        seen: list[str] = []

        def make_key(
            fn: Any,  # noqa: ANN401
            args: tuple[Any, ...],
            _kwargs: dict[str, Any],
        ) -> str:
            seen.append(fn.__name__)
            return f"made:{args[0]}"

        @limiter(key_maker=make_key)
        async def work(user_id: int) -> int:
            return user_id

        assert await work(USER) == USER
        assert seen == ["work"]
        with pytest.raises(RateLimitExceededError):
            await work(USER)


async def test_a_cost_consumes_several_tokens() -> None:
    """`cost` spends more than one token per call."""
    async with MemoryRateLimiterAdapter() as backend:
        limiter = RateLimiter.token_bucket(
            "api",
            capacity=CAPACITY,
            refill_rate=SLOW_REFILL,
            backend=backend,
        )

        @limiter(cost=CAPACITY)
        async def work() -> str:
            return "ok"

        assert await work() == "ok"
        with pytest.raises(RateLimitExceededError):
            await work()


async def test_a_wait_budget_waits_for_tokens() -> None:
    """A positive `max_wait` waits instead of raising at once."""
    async with MemoryRateLimiterAdapter() as backend:
        limiter = RateLimiter.token_bucket(
            "api", capacity=1, refill_rate=FAST_REFILL, backend=backend
        )

        @limiter(max_wait=MAX_WAIT)
        async def work() -> str:
            return "ok"

        assert await work() == "ok"
        assert await work() == "ok"


async def test_a_wait_budget_still_gives_up() -> None:
    """A wait that would exceed the budget raises."""
    async with MemoryRateLimiterAdapter() as backend:
        limiter = RateLimiter.token_bucket(
            "api", capacity=1, refill_rate=SLOW_REFILL, backend=backend
        )

        @limiter(max_wait=0.01)
        async def work() -> str:
            return "ok"

        assert await work() == "ok"
        with pytest.raises(RateLimitExceededError):
            await work()


def test_the_binding_is_reusable_across_functions() -> None:
    """One binding decorates more than one function."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)
    binding = limiter(key="shared")
    assert isinstance(binding, RateLimiterBinding)
    assert binding.limiter is limiter


def test_the_binding_names_its_key_in_its_repr() -> None:
    """The repr says which limiter and which key."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)
    assert repr(limiter(key="user:{uid}")) == (
        "<RateLimiterBinding 'api' key='user:{uid}'>"
    )
    assert repr(limiter()) == "<RateLimiterBinding 'api' key='default'>"

    def make_key(_fn: Any, _args: Any, _kwargs: Any) -> str:  # noqa: ANN401
        return "x"

    assert "make_key" in repr(limiter(key_maker=make_key))


def test_a_key_and_a_key_maker_together_are_refused() -> None:
    """One key source, not two."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)
    with pytest.raises(TypeError, match="not both"):
        limiter(key="a", key_maker=lambda _fn, _args, _kwargs: "b")


def test_a_negative_wait_budget_is_refused() -> None:
    """A wait budget is a number of seconds."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)
    with pytest.raises(ValueError, match="cannot be negative"):
        limiter(max_wait=-1.0)


def test_a_template_naming_an_unknown_parameter_is_refused() -> None:
    """The template is checked against the function it decorates."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)

    with pytest.raises(ValueError, match=r"which .* does not take"):

        @limiter(key="user:{missing}")
        async def work(user_id: int) -> int:
            return user_id


def test_a_sync_function_is_refused() -> None:
    """Consuming tokens needs the event loop."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)

    with pytest.raises(TypeError, match="only decorates async functions"):

        @limiter  # ty: ignore[no-matching-overload]
        def work() -> str:  # type: ignore[arg-type]
            return "never"


async def test_decorates_a_callable_object_with_an_async_call() -> None:
    """An object whose `__call__` is async is metered like a function."""
    async with MemoryRateLimiterAdapter() as backend:
        limiter = RateLimiter.token_bucket(
            "api", capacity=1, refill_rate=SLOW_REFILL, backend=backend
        )

        class Client:
            """A callable that is async without being a coroutine function."""

            async def __call__(self) -> str:
                return "ok"

        work = limiter(Client())

        assert await work() == "ok"
        with pytest.raises(RateLimitExceededError):
            await work()


def test_a_none_wait_budget_is_refused() -> None:
    """`None` means wait forever on `wait`, and never a decorator budget."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)
    with pytest.raises(TypeError, match="never None"):
        limiter(max_wait=None)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    "template", ["user:{}", "user:{0}"], ids=["auto", "index"]
)
def test_a_positional_template_field_is_refused(template: str) -> None:
    """A positional field names no parameter, so it is refused early."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)

    with pytest.raises(ValueError, match="positional field"):

        @limiter(key=template)
        async def work(user_id: int) -> int:
            return user_id


@pytest.mark.parametrize("cost", [0, -1], ids=["zero", "negative"])
def test_a_cost_below_one_is_refused(cost: int) -> None:
    """A cost is a number of tokens, refused where it is written."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)
    with pytest.raises(ValueError, match="at least 1"):
        limiter(cost=cost)


def test_a_nested_template_field_is_validated_too() -> None:
    """A field inside a format spec names a parameter like any other."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)

    with pytest.raises(ValueError, match=r"which .* does not take"):

        @limiter(key="user:{user_id:{width}}")
        async def work(user_id: int) -> int:
            return user_id


async def test_a_nested_template_field_renders() -> None:
    """A format spec that names a real parameter still renders."""
    async with MemoryRateLimiterAdapter() as backend:
        limiter = RateLimiter.token_bucket(
            "api", capacity=1, refill_rate=SLOW_REFILL, backend=backend
        )

        @limiter(key="user:{user_id:{width}}")
        async def work(user_id: int, width: int) -> int:  # noqa: ARG001
            return user_id

        assert await work(USER, 3) == USER
        with pytest.raises(RateLimitExceededError):
            await work(USER, 3)


async def test_a_template_with_a_trailing_literal_renders() -> None:
    """Text after the last field is part of the key."""
    async with MemoryRateLimiterAdapter() as backend:
        limiter = RateLimiter.token_bucket(
            "api", capacity=1, refill_rate=SLOW_REFILL, backend=backend
        )

        @limiter(key="user:{user_id}-v1")
        async def work(user_id: int) -> int:
            return user_id

        assert await work(USER) == USER
        with pytest.raises(RateLimitExceededError):
            await work(USER)
