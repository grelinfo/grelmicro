"""Attempt-count boundary tests for the retry run loops.

The broader suite checks that retries happen and eventually re-raise. These
tests pin the exact number of calls on exhaustion, so an off-by-one in the
attempt range or a flip of the `number >= attempts` exhaustion guard is caught.
"""

import pytest

from grelmicro.resilience import ConstantBackoff, Retry

pytestmark = [pytest.mark.timeout(1)]

_FAST_DELAY = 0.001
_ATTEMPTS = 3
_SECOND_TRY = 2
_BOOM = "boom"


@pytest.fixture
def fast_constant() -> ConstantBackoff:
    """Return a near-zero constant backoff so retries do not wait."""
    return ConstantBackoff(delay=_FAST_DELAY)


async def test_async_exhaustion_calls_fn_exactly_attempts_times(
    fast_constant: ConstantBackoff,
) -> None:
    """An always-failing async call runs exactly `attempts` times, then raises."""
    policy = Retry(
        "count-async", fast_constant, when=ValueError, attempts=_ATTEMPTS
    )
    calls = 0

    @policy
    async def always_fail() -> None:
        nonlocal calls
        calls += 1
        raise ValueError(_BOOM)

    with pytest.raises(ValueError, match=_BOOM):
        await always_fail()

    assert calls == _ATTEMPTS


def test_sync_exhaustion_calls_fn_exactly_attempts_times(
    fast_constant: ConstantBackoff,
) -> None:
    """An always-failing sync call runs exactly `attempts` times, then raises."""
    policy = Retry(
        "count-sync", fast_constant, when=ValueError, attempts=_ATTEMPTS
    )
    calls = 0

    @policy
    def always_fail() -> None:
        nonlocal calls
        calls += 1
        raise ValueError(_BOOM)

    with pytest.raises(ValueError, match=_BOOM):
        always_fail()

    assert calls == _ATTEMPTS


async def test_async_succeeds_without_extra_attempts(
    fast_constant: ConstantBackoff,
) -> None:
    """A call that succeeds on the second try runs exactly twice."""
    policy = Retry(
        "count-recover", fast_constant, when=ValueError, attempts=_ATTEMPTS
    )
    calls = 0

    @policy
    async def fail_once() -> str:
        nonlocal calls
        calls += 1
        if calls < _SECOND_TRY:
            raise ValueError(_BOOM)
        return "ok"

    assert await fail_once() == "ok"
    assert calls == _SECOND_TRY
