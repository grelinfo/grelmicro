"""Exact backoff-delay and async-callable tests for the Shield retry loop.

The broader suite checks that retries back off and give up. These pin the exact
delay formula `random() * min(scale * 2 ** (attempt - 1), cap)` and the
async-callable guard, so a flipped operator/exponent in `_backoff_for` or a
flipped boolean in the `run` guard is caught.
"""

from __future__ import annotations

import functools

from grelmicro.resilience import Shield

_FIXED_RANDOM = 0.5
_ATTEMPT = 4
_EXP_BASE = 2
_PARTIAL_ARG = 21
_DOUBLED = 42


def test_backoff_is_random_times_capped_exponential() -> None:
    """Delay is `random() * min(scale * 2 ** (attempt - 1), cap)`."""
    shield = Shield.api("backoff-exact", timeout_errors=(ValueError,))
    shield._random = lambda: _FIXED_RANDOM
    state = shield._state

    scale = state.config.backoff_scale
    cap = state.config.backoff_cap
    expected = _FIXED_RANDOM * min(scale * _EXP_BASE ** (_ATTEMPT - 1), cap)

    assert shield._backoff_for(state, _ATTEMPT) == expected


async def test_run_accepts_partial_wrapped_coroutine() -> None:
    """A `functools.partial` over a coroutine is a valid async callable."""

    async def work(value: int) -> int:
        return value * 2

    shield = Shield.api("partial-run", timeout_errors=(ValueError,))
    wrapped = functools.partial(work, _PARTIAL_ARG)

    assert await shield.run(wrapped) == _DOUBLED
