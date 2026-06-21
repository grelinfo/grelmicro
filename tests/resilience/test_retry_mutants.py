"""Exact-value retry tests that pin formula and boundary behavior.

These tests target mutation survivors in the retry run loops and
factories. They assert the exact delay sequence, the exact attempt
count, the exact returned result, and the exact factory defaults, so
operator flips (``>=`` to ``>``, ``-`` to ``+``, ``or`` to ``and``),
off-by-one range changes, and dropped arguments all diverge.
"""

import time as _time

import pytest
from pytest_mock import MockerFixture

from grelmicro.resilience import (
    ConstantBackoff,
    LinearBackoff,
    Match,
    Outcome,
    Retry,
    retry,
    retrying,
)
from grelmicro.resilience.backoffs import (
    ConstantBackoff as _Constant,
)
from grelmicro.resilience.backoffs import (
    ExponentialBackoff as _Exponential,
)

pytestmark = [pytest.mark.timeout(5)]

_DELAY = 7.0
_ATTEMPTS = 3
_FIRST = 1
_SECOND = 2
_FIVE = 5
_BUDGET = 50.0
_SUM = 7
_BASE_DELAY = 0.1
_MAX_DELAY = 30.0


@pytest.fixture
def record_async_sleep(mocker: MockerFixture) -> list[float]:
    """Record the delay passed to each async sleep, never wait."""
    delays: list[float] = []

    async def _spy(seconds: float) -> None:
        delays.append(seconds)

    mocker.patch("grelmicro.resilience.retry.clock_sleep", side_effect=_spy)
    return delays


@pytest.fixture
def record_sync_sleep(mocker: MockerFixture) -> list[float]:
    """Record the delay passed to each sync sleep, never wait."""
    delays: list[float] = []

    def _spy(seconds: float) -> None:
        delays.append(seconds)

    mocker.patch.object(_time, "sleep", side_effect=_spy)
    return delays


# --- Delay sequence: first attempt never sleeps, others use the backoff ---


async def test_async_delay_sequence_is_exact(
    record_async_sleep: list[float],
) -> None:
    """Three failing attempts sleep exactly the constant delay twice."""
    policy = Retry(
        "delay-async",
        ConstantBackoff(delay=_DELAY),
        when=ValueError,
        attempts=_ATTEMPTS,
    )

    @policy
    async def boom() -> None:
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        await boom()

    assert record_async_sleep == [_DELAY, _DELAY]


def test_sync_delay_sequence_is_exact(
    record_sync_sleep: list[float],
) -> None:
    """Three failing attempts sleep exactly the constant delay twice (sync)."""
    policy = Retry(
        "delay-sync",
        ConstantBackoff(delay=_DELAY),
        when=ValueError,
        attempts=_ATTEMPTS,
    )

    @policy
    def boom() -> None:
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        boom()

    assert record_sync_sleep == [_DELAY, _DELAY]


async def test_block_form_delay_sequence_is_exact(
    record_async_sleep: list[float],
) -> None:
    """The async block form sleeps exactly the constant delay between tries."""
    policy = Retry(
        "delay-block",
        ConstantBackoff(delay=_DELAY),
        when=ValueError,
        attempts=_ATTEMPTS,
    )

    with pytest.raises(ValueError, match="boom"):  # noqa: PT012
        async for attempt in policy:
            async with attempt:
                msg = "boom"
                raise ValueError(msg)

    assert record_async_sleep == [_DELAY, _DELAY]


def test_sync_block_form_delay_sequence_is_exact(
    record_sync_sleep: list[float],
) -> None:
    """The sync block form sleeps exactly the constant delay between tries."""
    policy = Retry(
        "delay-block-sync",
        ConstantBackoff(delay=_DELAY),
        when=ValueError,
        attempts=_ATTEMPTS,
    )

    with pytest.raises(ValueError, match="boom"):  # noqa: PT012
        for attempt in policy:
            with attempt:
                msg = "boom"
                raise ValueError(msg)

    assert record_sync_sleep == [_DELAY, _DELAY]


# --- delay_before exposed on the first attempt is exactly zero ------------


async def test_first_attempt_delay_before_is_zero(
    record_async_sleep: list[float],
) -> None:
    """The first yielded attempt reports a zero pre-delay and never sleeps."""
    policy = Retry(
        "first-delay",
        ConstantBackoff(delay=_DELAY),
        when=ValueError,
        attempts=_ATTEMPTS,
    )
    seen: list[float] = []

    async for attempt in policy:
        seen.append(attempt.delay_before)
        async with attempt:
            if attempt.number == _SECOND:
                break
            msg = "boom"
            raise ValueError(msg)

    assert seen == [0.0, _DELAY]
    assert record_async_sleep == [_DELAY]


# --- Result-based retry returns the exact last result --------------------


async def test_async_result_retry_returns_exact_last_result(
    record_async_sleep: list[float],
) -> None:
    """When every result still matches, the exact last result is returned."""
    policy = Retry(
        "result-async",
        ConstantBackoff(delay=_DELAY),
        when=Match.result(lambda value: value is not None and value < 0),
        attempts=_ATTEMPTS,
    )
    seen: list[int] = []

    @policy
    async def always_negative() -> int:
        seen.append(len(seen))
        return -len(seen)

    # Every result keeps matching, so the loop exhausts and returns the
    # last produced value rather than None or the first one.
    assert await always_negative() == -_ATTEMPTS
    assert len(seen) == _ATTEMPTS
    assert record_async_sleep == [_DELAY, _DELAY]


def test_sync_result_retry_returns_exact_last_result(
    record_sync_sleep: list[float],
) -> None:
    """The sync path returns the exact last matching result on exhaustion."""
    policy = Retry(
        "result-sync",
        ConstantBackoff(delay=_DELAY),
        when=Match.result(lambda value: value is not None and value < 0),
        attempts=_ATTEMPTS,
    )
    seen: list[int] = []

    @policy
    def always_negative() -> int:
        seen.append(len(seen))
        return -len(seen)

    assert always_negative() == -_ATTEMPTS
    assert len(seen) == _ATTEMPTS
    assert record_sync_sleep == [_DELAY, _DELAY]


@pytest.mark.usefixtures("record_async_sleep")
async def test_async_result_retry_stops_on_budget_before_attempts(
    mocker: MockerFixture,
) -> None:
    """A matching result stops on the budget even with attempts left."""
    # started_at=100, first result check at 110 (elapsed 10, under budget),
    # second at 200 (elapsed 100, over the 50 budget) so it stops at call 2.
    reads = iter([100.0, 110.0, 200.0])
    mocker.patch(
        "grelmicro.resilience.retry.clock_monotonic",
        side_effect=lambda: next(reads, 200.0),
    )
    policy = Retry(
        "result-budget-async",
        ConstantBackoff(delay=_DELAY),
        when=Match.result(lambda value: value is not None and value < 0),
        attempts=_FIVE,
        max_seconds=_BUDGET,
    )
    calls = 0

    @policy
    async def always_negative() -> int:
        nonlocal calls
        calls += 1
        return -calls

    assert await always_negative() == -_SECOND
    assert calls == _SECOND


@pytest.mark.usefixtures("record_sync_sleep")
def test_sync_result_retry_stops_on_budget_before_attempts(
    mocker: MockerFixture,
) -> None:
    """A matching result stops on the budget even with attempts left (sync)."""
    reads = iter([100.0, 110.0, 200.0])
    mocker.patch(
        "grelmicro.resilience.retry.clock_monotonic",
        side_effect=lambda: next(reads, 200.0),
    )
    policy = Retry(
        "result-budget-sync",
        ConstantBackoff(delay=_DELAY),
        when=Match.result(lambda value: value is not None and value < 0),
        attempts=_FIVE,
        max_seconds=_BUDGET,
    )
    calls = 0

    @policy
    def always_negative() -> int:
        nonlocal calls
        calls += 1
        return -calls

    assert always_negative() == -_SECOND
    assert calls == _SECOND


# --- args and kwargs are both forwarded to the wrapped function ----------


async def test_async_forwards_args_and_kwargs() -> None:
    """Both positional and keyword arguments reach the wrapped coroutine."""
    policy = Retry("args-async", ConstantBackoff(delay=_DELAY), when=ValueError)
    received: dict[str, object] = {}

    @policy
    async def echo(a: int, *, b: int) -> int:
        received["a"] = a
        received["b"] = b
        return a + b

    assert await echo(3, b=4) == _SUM
    assert received == {"a": 3, "b": 4}


def test_sync_forwards_args_and_kwargs() -> None:
    """Both positional and keyword arguments reach the wrapped function."""
    policy = Retry("args-sync", ConstantBackoff(delay=_DELAY), when=ValueError)
    received: dict[str, object] = {}

    @policy
    def echo(a: int, *, b: int) -> int:
        received["a"] = a
        received["b"] = b
        return a + b

    assert echo(3, b=4) == _SUM
    assert received == {"a": 3, "b": 4}


# --- max_seconds budget: elapsed formula and boundary --------------------

# The clock starts at a nonzero value so an `elapsed = now - started_at`
# mutated to `now + started_at` diverges (one stays under budget, the
# other shoots far past it). The budget is checked with `>=`, so an
# elapsed exactly equal to the budget must stop.
_START = 100.0
_UNDER = 110.0
_AT_BUDGET = _START + _BUDGET


def _fixed_clock(mocker: MockerFixture, first: float, rest: float) -> None:
    """Patch the retry clock: return `first` once, then `rest` forever."""
    reads = iter([first])

    def _read() -> float:
        return next(reads, rest)

    mocker.patch(
        "grelmicro.resilience.retry.clock_monotonic", side_effect=_read
    )


@pytest.mark.usefixtures("record_async_sleep")
async def test_async_budget_under_continues_to_exhaustion(
    mocker: MockerFixture,
) -> None:
    """Elapsed under the budget keeps retrying until attempts run out."""
    _fixed_clock(mocker, _START, _UNDER)
    policy = Retry(
        "budget-under-async",
        ConstantBackoff(delay=_DELAY),
        when=ValueError,
        attempts=_ATTEMPTS,
        max_seconds=_BUDGET,
    )
    calls = 0

    @policy
    async def boom() -> None:
        nonlocal calls
        calls += 1
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom") as info:
        await boom()

    assert calls == _ATTEMPTS
    note = "\n".join(info.value.__notes__)
    assert "attempts exhausted" in note
    assert "budget elapsed" not in note


@pytest.mark.usefixtures("record_sync_sleep")
def test_sync_budget_under_continues_to_exhaustion(
    mocker: MockerFixture,
) -> None:
    """Elapsed under the budget keeps retrying until attempts run out (sync)."""
    _fixed_clock(mocker, _START, _UNDER)
    policy = Retry(
        "budget-under-sync",
        ConstantBackoff(delay=_DELAY),
        when=ValueError,
        attempts=_ATTEMPTS,
        max_seconds=_BUDGET,
    )
    calls = 0

    @policy
    def boom() -> None:
        nonlocal calls
        calls += 1
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom") as info:
        boom()

    assert calls == _ATTEMPTS
    note = "\n".join(info.value.__notes__)
    assert "attempts exhausted" in note
    assert "budget elapsed" not in note


@pytest.mark.usefixtures("record_async_sleep")
async def test_block_form_budget_under_continues_to_exhaustion(
    mocker: MockerFixture,
) -> None:
    """The block form keeps retrying while elapsed stays under the budget."""
    _fixed_clock(mocker, _START, _UNDER)
    policy = Retry(
        "budget-under-block",
        ConstantBackoff(delay=_DELAY),
        when=ValueError,
        attempts=_ATTEMPTS,
        max_seconds=_BUDGET,
    )
    calls = 0

    with pytest.raises(ValueError, match="boom") as info:  # noqa: PT012
        async for attempt in policy:
            async with attempt:
                calls += 1
                msg = "boom"
                raise ValueError(msg)

    assert calls == _ATTEMPTS
    note = "\n".join(info.value.__notes__)
    assert "attempts exhausted" in note
    assert "budget elapsed" not in note


async def test_async_budget_at_boundary_stops(
    mocker: MockerFixture,
    record_async_sleep: list[float],
) -> None:
    """Elapsed exactly equal to the budget stops retrying (>= boundary)."""
    _fixed_clock(mocker, _START, _AT_BUDGET)
    policy = Retry(
        "budget-at-async",
        ConstantBackoff(delay=_DELAY),
        when=ValueError,
        attempts=_ATTEMPTS,
        max_seconds=_BUDGET,
    )
    calls = 0

    @policy
    async def boom() -> None:
        nonlocal calls
        calls += 1
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom") as info:
        await boom()

    assert calls == _FIRST
    assert record_async_sleep == []
    assert any("budget elapsed" in note for note in info.value.__notes__)


def test_sync_budget_at_boundary_stops(
    mocker: MockerFixture,
    record_sync_sleep: list[float],
) -> None:
    """Elapsed exactly equal to the budget stops retrying (sync >= boundary)."""
    _fixed_clock(mocker, _START, _AT_BUDGET)
    policy = Retry(
        "budget-at-sync",
        ConstantBackoff(delay=_DELAY),
        when=ValueError,
        attempts=_ATTEMPTS,
        max_seconds=_BUDGET,
    )
    calls = 0

    @policy
    def boom() -> None:
        nonlocal calls
        calls += 1
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom") as info:
        boom()

    assert calls == _FIRST
    assert record_sync_sleep == []
    assert any("budget elapsed" in note for note in info.value.__notes__)


async def test_block_form_budget_at_boundary_stops(
    mocker: MockerFixture,
    record_async_sleep: list[float],
) -> None:
    """The block form stops when elapsed exactly equals the budget."""
    _fixed_clock(mocker, _START, _AT_BUDGET)
    policy = Retry(
        "budget-at-block",
        ConstantBackoff(delay=_DELAY),
        when=ValueError,
        attempts=_ATTEMPTS,
        max_seconds=_BUDGET,
    )
    calls = 0

    with pytest.raises(ValueError, match="boom") as info:  # noqa: PT012
        async for attempt in policy:
            async with attempt:
                calls += 1
                msg = "boom"
                raise ValueError(msg)

    assert calls == _FIRST
    assert record_async_sleep == []
    assert any("budget elapsed" in note for note in info.value.__notes__)


# --- Exhaustion note names the backoff and the attempt count -------------


async def test_async_exhaustion_note_names_backoff_and_attempts() -> None:
    """The exhaustion note carries the backoff name and the attempt total."""
    policy = Retry(
        "note-async",
        ConstantBackoff(delay=0.0001),
        when=ValueError,
        attempts=_ATTEMPTS,
    )

    @policy
    async def boom() -> None:
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom") as info:
        await boom()

    note = "\n".join(info.value.__notes__)
    assert "constant backoff" in note
    assert f"{_ATTEMPTS}/{_ATTEMPTS} attempts" in note


async def test_block_form_exhaustion_note_names_backoff() -> None:
    """The block-form exhaustion note carries the backoff name."""
    policy = Retry(
        "note-block",
        ConstantBackoff(delay=0.0001),
        when=ValueError,
        attempts=_ATTEMPTS,
    )

    with pytest.raises(ValueError, match="boom") as info:  # noqa: PT012
        async for attempt in policy:
            async with attempt:
                msg = "boom"
                raise ValueError(msg)

    note = "\n".join(info.value.__notes__)
    assert "constant backoff" in note
    assert f"{_ATTEMPTS}/{_ATTEMPTS} attempts" in note


# --- A clean exit suppresses nothing and stops the loop ------------------


async def test_successful_attempt_stops_and_does_not_suppress() -> None:
    """A body that does not raise yields exactly one attempt."""
    policy = Retry(
        "success-stop",
        ConstantBackoff(delay=_DELAY),
        when=ValueError,
        attempts=_ATTEMPTS,
    )
    numbers: list[int] = []

    async for attempt in policy:
        numbers.append(attempt.number)
        async with attempt:
            pass

    assert numbers == [_FIRST]


# --- Factory default values are exact ------------------------------------


def test_exponential_factory_defaults_are_exact() -> None:
    """`Retry.exponential` defaults to 3 attempts and a 0.1/30.0 backoff."""
    policy = Retry.exponential("exp-defaults", when=ValueError)
    config = policy.config
    backoff = config.backoff
    assert config.attempts == _ATTEMPTS
    assert isinstance(backoff, _Exponential)
    assert backoff.base_delay == _BASE_DELAY
    assert backoff.max_delay == _MAX_DELAY


def test_constant_factory_defaults_are_exact() -> None:
    """`Retry.constant` defaults to 3 attempts and a 1.0 second delay."""
    policy = Retry.constant("const-defaults", when=ValueError)
    config = policy.config
    backoff = config.backoff
    assert config.attempts == _ATTEMPTS
    assert isinstance(backoff, _Constant)
    assert backoff.delay == 1.0


@pytest.mark.usefixtures("record_async_sleep")
async def test_retry_decorator_default_attempts_is_three() -> None:
    """`@retry(when=...)` runs exactly three attempts by default."""
    calls = 0

    @retry(when=ValueError)
    async def boom() -> None:
        nonlocal calls
        calls += 1
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        await boom()

    assert calls == _ATTEMPTS


async def test_retry_exponential_default_base_delay_is_point_one(
    mocker: MockerFixture,
    record_async_sleep: list[float],
) -> None:
    """`@retry.exponential` defaults the first backoff to base_delay 0.1."""
    # Force full jitter to take its upper bound so the raw delay is observable.
    mocker.patch(
        "grelmicro.resilience.backoffs.exponential.random.uniform",
        side_effect=lambda _low, high: high,
    )
    calls = 0

    @retry.exponential(when=ValueError, attempts=_SECOND)
    async def boom() -> None:
        nonlocal calls
        calls += 1
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        await boom()

    assert calls == _SECOND
    assert record_async_sleep == pytest.approx([0.1])


async def test_retrying_constant_default_delay_is_one(
    record_async_sleep: list[float],
) -> None:
    """`retrying.constant` defaults the delay to 1.0 second."""
    calls = 0

    with pytest.raises(ValueError, match="boom"):  # noqa: PT012
        async for attempt in retrying.constant(when=ValueError):
            async with attempt:
                calls += 1
                msg = "boom"
                raise ValueError(msg)

    assert calls == _ATTEMPTS
    assert record_async_sleep == [1.0, 1.0]


async def test_retrying_exponential_default_base_delay_is_point_one(
    mocker: MockerFixture,
    record_async_sleep: list[float],
) -> None:
    """`retrying.exponential` defaults the first backoff to base_delay 0.1."""
    mocker.patch(
        "grelmicro.resilience.backoffs.exponential.random.uniform",
        side_effect=lambda _low, high: high,
    )
    calls = 0

    with pytest.raises(ValueError, match="boom"):  # noqa: PT012
        async for attempt in retrying.exponential(
            when=ValueError, attempts=_SECOND
        ):
            async with attempt:
                calls += 1
                msg = "boom"
                raise ValueError(msg)

    assert calls == _SECOND
    assert record_async_sleep == pytest.approx([0.1])


async def test_retry_constant_default_attempts_and_delay(
    record_async_sleep: list[float],
) -> None:
    """`@retry.constant(when=...)` defaults to 3 attempts and a 1.0 delay."""
    calls = 0

    @retry.constant(when=ValueError)
    async def boom() -> None:
        nonlocal calls
        calls += 1
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        await boom()

    assert calls == _ATTEMPTS
    assert record_async_sleep == [1.0, 1.0]


@pytest.mark.usefixtures("record_async_sleep")
async def test_retrying_default_attempts_is_three() -> None:
    """Bare `retrying(when=...)` runs exactly three attempts by default."""
    calls = 0

    with pytest.raises(ValueError, match="boom"):  # noqa: PT012
        async for attempt in retrying(when=ValueError):
            async with attempt:
                calls += 1
                msg = "boom"
                raise ValueError(msg)

    assert calls == _ATTEMPTS


async def test_retry_exponential_default_max_delay_caps_at_thirty(
    mocker: MockerFixture,
    record_async_sleep: list[float],
) -> None:
    """`@retry.exponential` caps the default backoff at 30.0 seconds."""
    mocker.patch(
        "grelmicro.resilience.backoffs.exponential.random.uniform",
        side_effect=lambda _low, high: high,
    )

    @retry.exponential(when=ValueError, attempts=11)
    async def boom() -> None:
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        await boom()

    # 0.1 * 2**9 = 51.2 is capped to the 30.0 default max_delay.
    assert max(record_async_sleep) == pytest.approx(30.0)


@pytest.mark.usefixtures("record_async_sleep")
async def test_retry_exponential_default_attempts_is_three() -> None:
    """`@retry.exponential(when=...)` runs exactly three attempts by default."""
    calls = 0

    @retry.exponential(when=ValueError)
    async def boom() -> None:
        nonlocal calls
        calls += 1
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        await boom()

    assert calls == _ATTEMPTS


@pytest.mark.usefixtures("record_async_sleep")
async def test_retrying_exponential_default_attempts_is_three() -> None:
    """`retrying.exponential(when=...)` runs exactly three attempts by default."""
    calls = 0

    with pytest.raises(ValueError, match="boom"):  # noqa: PT012
        async for attempt in retrying.exponential(when=ValueError):
            async with attempt:
                calls += 1
                msg = "boom"
                raise ValueError(msg)

    assert calls == _ATTEMPTS


async def test_retrying_exponential_default_max_delay_caps_at_thirty(
    mocker: MockerFixture,
    record_async_sleep: list[float],
) -> None:
    """`retrying.exponential` caps the default backoff at 30.0 seconds."""
    mocker.patch(
        "grelmicro.resilience.backoffs.exponential.random.uniform",
        side_effect=lambda _low, high: high,
    )

    with pytest.raises(ValueError, match="boom"):  # noqa: PT012
        async for attempt in retrying.exponential(when=ValueError, attempts=11):
            async with attempt:
                msg = "boom"
                raise ValueError(msg)

    # 0.1 * 2**9 = 51.2 is capped to the 30.0 default max_delay.
    assert max(record_async_sleep) == pytest.approx(30.0)


# --- Sync paths: sub-second delay, attempt-indexed delay, block budget ---

_SUB_SECOND = 0.5
_LINEAR_BASE = 2.0


def test_sync_decorator_sleeps_sub_second_delay(
    record_sync_sleep: list[float],
) -> None:
    """A sub-second delay still triggers a sleep (`> 0`, not `> 1`)."""
    policy = Retry(
        "subsec-sync",
        ConstantBackoff(delay=_SUB_SECOND),
        when=ValueError,
        attempts=_SECOND,
    )

    @policy
    def boom() -> None:
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        boom()

    assert record_sync_sleep == [_SUB_SECOND]


def test_sync_block_form_sleeps_sub_second_delay(
    record_sync_sleep: list[float],
) -> None:
    """The sync block form sleeps a sub-second delay (`> 0`, not `> 1`)."""
    policy = Retry(
        "subsec-block-sync",
        ConstantBackoff(delay=_SUB_SECOND),
        when=ValueError,
        attempts=_SECOND,
    )

    with pytest.raises(ValueError, match="boom"):  # noqa: PT012
        for attempt in policy:
            with attempt:
                msg = "boom"
                raise ValueError(msg)

    assert record_sync_sleep == [_SUB_SECOND]


def test_sync_decorator_uses_attempt_indexed_delay(
    record_sync_sleep: list[float],
) -> None:
    """The sync exception path feeds the real attempt number to the backoff."""
    policy = Retry(
        "linear-sync",
        LinearBackoff(base_delay=_LINEAR_BASE, max_delay=100.0),
        when=ValueError,
        attempts=_ATTEMPTS,
    )

    @policy
    def boom() -> None:
        msg = "boom"
        raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        boom()

    # Linear: base*1 then base*2. A dropped attempt number would feed
    # None to the backoff and raise a TypeError.
    assert record_sync_sleep == [_LINEAR_BASE, _LINEAR_BASE * _SECOND]


def test_sync_block_form_uses_attempt_indexed_delay(
    record_sync_sleep: list[float],
) -> None:
    """The sync block form feeds the real attempt number to the backoff."""
    policy = Retry(
        "linear-block-sync",
        LinearBackoff(base_delay=_LINEAR_BASE, max_delay=100.0),
        when=ValueError,
        attempts=_ATTEMPTS,
    )

    with pytest.raises(ValueError, match="boom"):  # noqa: PT012
        for attempt in policy:
            with attempt:
                msg = "boom"
                raise ValueError(msg)

    assert record_sync_sleep == [_LINEAR_BASE, _LINEAR_BASE * _SECOND]


async def test_async_result_retry_uses_attempt_indexed_delay(
    record_async_sleep: list[float],
) -> None:
    """The async result path feeds the real attempt number to the backoff."""
    policy = Retry(
        "linear-result-async",
        LinearBackoff(base_delay=_LINEAR_BASE, max_delay=100.0),
        when=Match.result(lambda value: value is not None and value < 0),
        attempts=_ATTEMPTS,
    )

    @policy
    async def always_negative() -> int:
        return -1

    assert await always_negative() == -_FIRST
    assert record_async_sleep == [_LINEAR_BASE, _LINEAR_BASE * _SECOND]


def test_sync_result_retry_uses_attempt_indexed_delay(
    record_sync_sleep: list[float],
) -> None:
    """The sync result path feeds the real attempt number to the backoff."""
    policy = Retry(
        "linear-result-sync",
        LinearBackoff(base_delay=_LINEAR_BASE, max_delay=100.0),
        when=Match.result(lambda value: value is not None and value < 0),
        attempts=_ATTEMPTS,
    )

    @policy
    def always_negative() -> int:
        return -1

    assert always_negative() == -_FIRST
    assert record_sync_sleep == [_LINEAR_BASE, _LINEAR_BASE * _SECOND]


def test_sync_block_form_first_attempt_delay_before_is_zero(
    record_sync_sleep: list[float],
) -> None:
    """The first sync attempt reports a zero pre-delay and never sleeps."""
    policy = Retry(
        "first-delay-sync",
        ConstantBackoff(delay=_DELAY),
        when=ValueError,
        attempts=_ATTEMPTS,
    )
    seen: list[float] = []

    for attempt in policy:
        seen.append(attempt.delay_before)
        with attempt:
            if attempt.number == _SECOND:
                break
            msg = "boom"
            raise ValueError(msg)

    assert seen == [0.0, _DELAY]
    assert record_sync_sleep == [_DELAY]


def test_sync_block_form_budget_at_boundary_stops(
    mocker: MockerFixture,
    record_sync_sleep: list[float],
) -> None:
    """The sync block form stops when elapsed exactly equals the budget."""
    _fixed_clock(mocker, _START, _AT_BUDGET)
    policy = Retry(
        "budget-at-block-sync",
        ConstantBackoff(delay=_DELAY),
        when=ValueError,
        attempts=_ATTEMPTS,
        max_seconds=_BUDGET,
    )
    calls = 0

    with pytest.raises(ValueError, match="boom") as info:  # noqa: PT012
        for attempt in policy:
            with attempt:
                calls += 1
                msg = "boom"
                raise ValueError(msg)

    assert calls == _FIRST
    assert record_sync_sleep == []
    assert any("budget elapsed" in note for note in info.value.__notes__)


# --- Result-path budget boundary is inclusive (>=) -----------------------


@pytest.mark.usefixtures("record_async_sleep")
async def test_async_result_budget_at_boundary_stops(
    mocker: MockerFixture,
) -> None:
    """A matching result stops when elapsed exactly equals the budget."""
    # started_at=100, result check at 150 (elapsed exactly 50 == budget).
    reads = iter([_START, _AT_BUDGET])
    mocker.patch(
        "grelmicro.resilience.retry.clock_monotonic",
        side_effect=lambda: next(reads, _AT_BUDGET),
    )
    policy = Retry(
        "result-budget-eq-async",
        ConstantBackoff(delay=_DELAY),
        when=Match.result(lambda value: value is not None and value < 0),
        attempts=_FIVE,
        max_seconds=_BUDGET,
    )
    calls = 0

    @policy
    async def always_negative() -> int:
        nonlocal calls
        calls += 1
        return -calls

    assert await always_negative() == -_FIRST
    assert calls == _FIRST


@pytest.mark.usefixtures("record_sync_sleep")
def test_sync_result_budget_at_boundary_stops(
    mocker: MockerFixture,
) -> None:
    """A matching result stops when elapsed exactly equals the budget (sync)."""
    reads = iter([_START, _AT_BUDGET])
    mocker.patch(
        "grelmicro.resilience.retry.clock_monotonic",
        side_effect=lambda: next(reads, _AT_BUDGET),
    )
    policy = Retry(
        "result-budget-eq-sync",
        ConstantBackoff(delay=_DELAY),
        when=Match.result(lambda value: value is not None and value < 0),
        attempts=_FIVE,
        max_seconds=_BUDGET,
    )
    calls = 0

    @policy
    def always_negative() -> int:
        nonlocal calls
        calls += 1
        return -calls

    assert always_negative() == -_FIRST
    assert calls == _FIRST


# --- FQN resolution keeps the full dotted module path --------------------


def test_env_when_resolves_nested_fqn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-segment FQN resolves through the full module path."""
    monkeypatch.setenv(
        "GREL_RETRY_NESTEDFQN_WHEN", "asyncio.exceptions.TimeoutError"
    )
    policy = Retry("nestedfqn", env_load=True)
    matcher = policy.config.when
    assert matcher(Outcome.from_exception(TimeoutError())) is True


def test_env_when_rejects_non_exception_fqn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An FQN that resolves to a non-Exception type is rejected."""
    monkeypatch.setenv("GREL_RETRY_NOTEXC_WHEN", "builtins.int")
    with pytest.raises(
        (ValueError, TypeError), match="not an Exception subclass"
    ):
        Retry("notexc", env_load=True)  # type: ignore[call-arg]
