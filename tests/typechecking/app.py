"""Type assertions for the `Grelmicro` app container."""

from typing import Any, assert_type

from grelmicro import Grelmicro, Usable
from grelmicro.cache import Cache
from grelmicro.health import HealthChecks
from grelmicro.providers.memory import MemoryProvider


def build() -> None:
    """Accept both component instances and bare component classes in `uses=`."""
    assert_type(Grelmicro(), Grelmicro)
    assert_type(Grelmicro(uses=[HealthChecks()]), Grelmicro)
    assert_type(Grelmicro(uses=[HealthChecks]), Grelmicro)


def usable_list() -> None:
    """`Usable` annotates a list mixing Components and Providers."""
    components: list[Usable] = [HealthChecks()]
    components.append(MemoryProvider())
    assert_type(Grelmicro(uses=components), Grelmicro)


def conditional() -> None:
    """Accept a `None` entry in `uses=` for a conditional registration."""
    provider: MemoryProvider | None = MemoryProvider()
    assert_type(Grelmicro(uses=[HealthChecks(), provider]), Grelmicro)


def usable_list_with_conditionals() -> None:
    """Annotate a prebuilt list carrying `None` entries as `list[Usable | None]`."""
    components: list[Usable | None] = [HealthChecks(), None]
    assert_type(Grelmicro(uses=components), Grelmicro)


def current() -> None:
    """`Grelmicro.current()` returns the app, not `Any`."""
    assert_type(Grelmicro.current(), Grelmicro)


async def lifecycle() -> None:
    """`async with` yields the app itself."""
    async with Grelmicro() as micro:
        assert_type(micro, Grelmicro)


def get_by_class() -> None:
    """`get(Component)` keeps the component type, `get(str)` stays `Any`."""
    micro = Grelmicro(uses=[HealthChecks()])
    assert_type(micro.get(HealthChecks), HealthChecks)
    assert_type(micro.get(HealthChecks, "default"), HealthChecks)
    assert_type(micro.get(Cache), Cache)
    assert_type(micro.get("mailer"), Any)
