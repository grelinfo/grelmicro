"""Exact-value bulkhead tests that pin the fail-fast and reconfigure logic.

These tests target mutation survivors that loose assertions miss: the
default `max_wait` of zero (fail fast, not a one second wait), keyword
forwarding on the shared-executor path, and the reconfigure guard that
only rebuilds the private executor when one already exists.
"""

import asyncio

import pytest
from pytest_mock import MockerFixture

from grelmicro.resilience import Bulkhead

pytestmark = [pytest.mark.timeout(5)]

_KWARGS_SUM = 5


async def test_fail_fast_uses_zero_wait_by_default(
    mocker: MockerFixture,
) -> None:
    """With no `max_wait`, the acquire deadline is exactly zero seconds."""
    waits: list[float] = []
    real_timeout = asyncio.timeout

    def _spy(delay: float) -> object:
        waits.append(delay)
        return real_timeout(delay)

    mocker.patch("grelmicro.resilience.bulkhead.asyncio.timeout", side_effect=_spy)
    bulkhead = Bulkhead("api", max_concurrent=1)

    async with bulkhead:
        pass

    assert waits == [0.0]


async def test_to_thread_shared_executor_forwards_kwargs() -> None:
    """`to_thread` forwards kwargs on the shared-executor path too."""
    bulkhead = Bulkhead("api")  # no max_workers, shared executor path

    def add(a: int, *, b: int) -> int:
        return a + b

    assert await bulkhead.to_thread(add, 2, b=3) == _KWARGS_SUM


async def test_private_executor_sized_to_max_workers() -> None:
    """The private pool is built with exactly the configured worker count."""
    bulkhead = Bulkhead("checkout", max_workers=1)

    await bulkhead.to_thread(lambda: None)

    assert bulkhead._executor is not None
    assert bulkhead._executor._max_workers == 1


async def test_reconfigure_without_executor_does_not_crash() -> None:
    """Reconfiguring max_workers with no built executor leaves it None."""
    bulkhead = Bulkhead("api", max_workers=2)
    # No `to_thread` call yet, so the private executor is never built.
    await bulkhead.reconfigure(
        bulkhead.config.model_copy(update={"max_workers": 4})
    )

    assert bulkhead.config.max_workers == 4  # noqa: PLR2004
