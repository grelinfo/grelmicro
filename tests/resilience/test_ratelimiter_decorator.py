"""Tests for the RateLimiter decorator and its binding."""

import functools
import gc
import weakref
from typing import Any

import pytest

from grelmicro.resilience import (
    MemoryRateLimiterAdapter,
    RateLimiter,
    RateLimiterBinding,
    RateLimitExceededError,
    Stack,
)
from grelmicro.task import Tasks

pytestmark = [pytest.mark.timeout(5)]

CAPACITY = 2
SLOW_REFILL = 0.0001
FAST_REFILL = 1000.0
MAX_WAIT = 1.0
USER = 7
OTHER_USER = 8


async def metered_job() -> None:
    """Do nothing, from the module level the scheduler requires."""


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

        @limiter(key_template="user:{user_id}")
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

        @limiter(key_template="user:{user_id}")
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

        @limiter(key_template="shared")
        async def work(user_id: int) -> int:
            return user_id

        assert await work(USER) == USER
        with pytest.raises(RateLimitExceededError):
            await work(OTHER_USER)


async def test_rate_limiter_key_function_receives_the_call() -> None:
    """A `key` function receives the function and the call's arguments."""
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

        @limiter(key=make_key)
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
    binding = limiter(key_template="shared")
    assert isinstance(binding, RateLimiterBinding)
    assert binding.limiter is limiter


def test_the_binding_names_its_key_in_its_repr() -> None:
    """The repr says which limiter and which key."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)
    assert repr(limiter(key_template="user:{uid}")) == (
        "<RateLimiterBinding 'api' key_template='user:{uid}'>"
    )
    assert repr(limiter()) == "<RateLimiterBinding 'api' key='default'>"

    def make_key(_fn: Any, _args: Any, _kwargs: Any) -> str:  # noqa: ANN401
        return "x"

    assert repr(limiter(key=make_key)).startswith(
        "<RateLimiterBinding 'api' key='"
    )
    assert "make_key'>" in repr(limiter(key=make_key))
    assert repr(limiter(key=functools.partial(make_key))) == (
        "<RateLimiterBinding 'api' key='partial'>"
    )


async def test_rate_limiter_key_function_derives_the_bucket() -> None:
    """A `key=` function receives `(func, args, kwargs)` and names the bucket."""
    async with MemoryRateLimiterAdapter() as backend:
        # Arrange
        limiter = RateLimiter.token_bucket(
            "api", capacity=1, refill_rate=SLOW_REFILL, backend=backend
        )

        @limiter(key=lambda _fn, args, _kwargs: f"user:{args[0]}")
        async def work(user_id: int) -> int:
            return user_id

        # Act
        await work(USER)

        # Assert
        assert await work(OTHER_USER) == OTHER_USER
        with pytest.raises(RateLimitExceededError):
            await work(USER)


async def test_rate_limiter_key_template_renders_the_bucket() -> None:
    """A `key_template=` string renders the bucket from the call."""
    async with MemoryRateLimiterAdapter() as backend:
        # Arrange
        limiter = RateLimiter.token_bucket(
            "api", capacity=1, refill_rate=SLOW_REFILL, backend=backend
        )

        @limiter(key_template="user:{user_id}")
        async def work(user_id: int) -> int:
            return user_id

        # Act
        await work(user_id=USER)

        # Assert
        assert await work(OTHER_USER) == OTHER_USER
        with pytest.raises(RateLimitExceededError):
            await work(USER)


def test_rate_limiter_key_string_is_refused_with_a_template_hint() -> None:
    """A string passed to `key=` is refused at decoration."""
    # Arrange
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)
    template: Any = "user:{user_id}"

    # Act / Assert
    with pytest.raises(
        TypeError,
        match="key must be a function, use key_template= for a template",
    ):
        limiter(key=template)


def test_rate_limiter_binding_key_string_is_refused_with_a_template_hint() -> (
    None
):
    """A `RateLimiterBinding` refuses a string `key=` the same way."""
    # Arrange
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)
    template: Any = "user:{user_id}"

    # Act / Assert
    with pytest.raises(TypeError, match="use key_template= for a template"):
        RateLimiterBinding(limiter, key=template)


def test_rate_limiter_key_and_key_template_together_are_refused() -> None:
    """One key source, not two."""
    # Arrange
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)

    # Act / Assert
    with pytest.raises(TypeError, match="not both"):
        limiter(key=lambda _fn, _args, _kwargs: "b", key_template="a")


def test_rate_limiter_key_maker_is_an_unknown_argument() -> None:
    """`key_maker=` is no longer accepted."""
    # Arrange
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)
    legacy: dict[str, Any] = {"key_maker": lambda _fn, _args, _kwargs: "b"}

    # Act / Assert
    with pytest.raises(TypeError, match="key_maker"):
        limiter(**legacy)


def test_a_negative_wait_budget_is_refused() -> None:
    """A wait budget is a number of seconds."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)
    with pytest.raises(ValueError, match="finite and not negative"):
        limiter(max_wait=-1.0)


def test_a_template_naming_an_unknown_parameter_is_refused() -> None:
    """The template is checked against the function it decorates."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)

    with pytest.raises(ValueError, match=r"which .* does not take"):

        @limiter(key_template="user:{missing}")
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

        @limiter(key_template=template)
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

        @limiter(key_template="user:{user_id:{width}}")
        async def work(user_id: int) -> int:
            return user_id


async def test_a_nested_template_field_renders() -> None:
    """A format spec that names a real parameter still renders."""
    async with MemoryRateLimiterAdapter() as backend:
        limiter = RateLimiter.token_bucket(
            "api", capacity=1, refill_rate=SLOW_REFILL, backend=backend
        )

        @limiter(key_template="user:{user_id:{width}}")
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

        @limiter(key_template="user:{user_id}-v1")
        async def work(user_id: int) -> int:
            return user_id

        assert await work(USER) == USER
        with pytest.raises(RateLimitExceededError):
            await work(USER)


async def test_a_resolver_is_reused_without_holding_the_target() -> None:
    """The template resolver is shared, and it pins nothing."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)
    binding = limiter(key_template="user:{user_id}")
    assert isinstance(binding, RateLimiterBinding)

    async def work(user_id: int) -> int:
        return user_id

    first = binding._resolver(work)
    assert binding._resolver(work) is first

    reference = weakref.ref(work)
    del work, first
    gc.collect()
    assert reference() is None


def test_a_resolver_is_built_for_a_target_that_takes_no_weak_reference() -> (
    None
):
    """A `__slots__` client cannot be a weak key, and still resolves."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)
    binding = limiter(key_template="user:{user_id}")
    assert isinstance(binding, RateLimiterBinding)

    class Client:
        """A callable that cannot be weakly referenced."""

        __slots__ = ()

        async def __call__(self, user_id: int) -> int:
            return user_id

    resolver = binding._resolver(Client())
    assert resolver((USER,), {}) == f"user:{USER}"


async def test_rate_limiter_key_function_resolver_is_not_kept() -> None:
    """A key function closes over the target, so its resolver is not kept."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)
    binding = limiter(key=lambda _fn, _args, _kwargs: "made")
    assert isinstance(binding, RateLimiterBinding)

    async def work(user_id: int) -> int:
        return user_id

    binding._resolver(work)
    reference = weakref.ref(work)
    del work
    gc.collect()

    assert reference() is None
    assert len(binding._resolvers) == 0


async def test_rate_limiter_key_function_stack_does_not_grow_per_call() -> None:
    """`run` on a fresh target each call keeps nothing behind."""
    async with MemoryRateLimiterAdapter() as backend:
        limiter = RateLimiter.token_bucket(
            "api", capacity=10**6, refill_rate=10**6, backend=backend
        )
        binding = limiter(key=lambda _fn, _args, _kwargs: "made")
        assert isinstance(binding, RateLimiterBinding)
        stack = Stack("api", patterns=[binding])

        async def base(value: int) -> int:
            return value

        for value in range(5):
            await stack.run(functools.partial(base), value)

        gc.collect()
        assert len(binding._resolvers) == 0


async def test_a_template_of_escaped_braces_reads_no_parameter() -> None:
    """Escaped braces render once, they do not bind the call."""
    async with MemoryRateLimiterAdapter() as backend:
        limiter = RateLimiter.token_bucket(
            "api", capacity=1, refill_rate=SLOW_REFILL, backend=backend
        )
        binding = limiter(key_template="a{{b}}")
        assert isinstance(binding, RateLimiterBinding)

        @binding
        async def work(user_id: int) -> int:
            return user_id

        assert await work(USER) == USER
        with pytest.raises(RateLimitExceededError):
            await work(OTHER_USER)
        assert binding._reads_signature is False


async def test_a_mis_called_function_is_named_in_the_error() -> None:
    """Binding the call must not hide which function was called."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)

    @limiter(key_template="user:{user_id}")
    async def work(user_id: int) -> int:
        return user_id

    with pytest.raises(TypeError, match=r"work\(\) missing a required"):
        await work()  # type: ignore[call-arg]  # ty: ignore[missing-argument]


async def test_a_bound_method_reuses_one_resolver() -> None:
    """A fresh bound method per lookup must not miss the cache."""
    async with MemoryRateLimiterAdapter() as backend:
        limiter = RateLimiter.token_bucket(
            "api", capacity=10**6, refill_rate=10**6, backend=backend
        )
        binding = limiter(key_template="user:{user_id}")
        assert isinstance(binding, RateLimiterBinding)

        class Service:
            """A service whose method is metered per user."""

            async def fetch(self, user_id: int) -> int:
                return user_id

        service = Service()
        stack = Stack("api", patterns=[binding])

        assert await stack.run(service.fetch, USER) == USER
        assert await stack.run(service.fetch, OTHER_USER) == OTHER_USER
        assert len(binding._bound_resolvers) == 1


async def test_a_bound_method_and_its_function_keep_separate_resolvers() -> (
    None
):
    """The two forms differ by `self`, so one signature cannot serve both."""
    async with MemoryRateLimiterAdapter() as backend:
        limiter = RateLimiter.token_bucket(
            "api", capacity=10**6, refill_rate=10**6, backend=backend
        )
        binding = limiter(key_template="user:{user_id}")
        assert isinstance(binding, RateLimiterBinding)

        class Service:
            """A service metered through both forms of its method."""

            async def fetch(self, user_id: str) -> str:
                return user_id

        service = Service()
        unbound = binding(Service.fetch)
        stack = Stack("api", patterns=[binding])

        assert await unbound(service, "u1") == "u1"
        assert await stack.run(service.fetch, "u2") == "u2"
        assert await unbound(service, "u3") == "u3"


def test_a_key_passed_positionally_points_at_the_keyword() -> None:
    """The first argument is the function, so a key must be named."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)
    with pytest.raises(TypeError, match=r"write `@limiter\(key_template="):
        limiter("user:{user_id}")  # type: ignore[call-overload]  # ty: ignore[no-matching-overload]


def test_a_template_that_cannot_be_read_names_itself() -> None:
    """A malformed template says which template, like every other refusal."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)
    with pytest.raises(ValueError, match=r"'u:\{oops' cannot be read"):
        limiter(key_template="u:{oops")


@pytest.mark.parametrize(
    "budget", [float("inf"), float("nan")], ids=["infinite", "nan"]
)
def test_a_wait_budget_that_is_not_finite_is_refused(budget: float) -> None:
    """An unbounded wait has nothing above it to stop it inside a stack."""
    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)
    with pytest.raises(ValueError, match="finite"):
        limiter(max_wait=budget)


def test_a_function_already_registered_as_a_task_is_refused() -> None:
    """The schedule holds what it registered, so the limiter goes above."""
    tasks = Tasks()
    registered = tasks.every(interval=60, name="metered")(metered_job)

    limiter = RateLimiter.token_bucket("api", capacity=1, refill_rate=1)

    with pytest.raises(TypeError, match="already registered as a task"):
        limiter(registered)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"max_wait": "soon"}, "number of seconds"),
        ({"cost": "two"}, "whole number"),
        ({"cost": True}, "whole number"),
    ],
    ids=["wait-text", "cost-text", "cost-bool"],
)
def test_a_budget_that_is_not_a_number_says_so(
    kwargs: dict[str, Any], match: str
) -> None:
    """A wrong type gets the same guiding message as a wrong value."""
    limiter = RateLimiter.token_bucket("api", capacity=CAPACITY, refill_rate=1)
    with pytest.raises(TypeError, match=match):
        limiter(**kwargs)
