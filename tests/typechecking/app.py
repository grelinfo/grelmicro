"""Type assertions for the `Grelmicro` app container."""

from typing import assert_type

from grelmicro import Grelmicro
from grelmicro.health import HealthChecks


def build() -> None:
    """Accept both component instances and bare component classes in `uses=`."""
    assert_type(Grelmicro(), Grelmicro)
    assert_type(Grelmicro(uses=[HealthChecks()]), Grelmicro)
    assert_type(Grelmicro(uses=[HealthChecks]), Grelmicro)


def current() -> None:
    """`Grelmicro.current()` returns the app, not `Any`."""
    assert_type(Grelmicro.current(), Grelmicro)


async def lifecycle() -> None:
    """`async with` yields the app itself."""
    async with Grelmicro() as micro:
        assert_type(micro, Grelmicro)
