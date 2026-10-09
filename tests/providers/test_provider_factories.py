"""A provider method returns a backend and ends in `_backend`.

A component method returns the pattern: `coordination.lock("cart")` is a
`Lock`, `redis.lock_backend()` is the backend it runs on.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest

from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.coordination import Coordination
from grelmicro.providers import Provider
from grelmicro.providers.memory import MemoryProvider
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.providers.valkey import ValkeyProvider

PATTERNS = (
    "lock",
    "readwritelock",
    "leaderelection",
    "schedule",
    "cache",
    "outbox",
    "ratelimiter",
    "circuitbreaker",
)
"""Every pattern a provider builds a backend for."""

PROVIDERS = (
    Provider,
    MemoryProvider,
    PostgresProvider,
    RedisProvider,
    SQLiteProvider,
    ValkeyProvider,
)
"""The base class and every shipped provider."""


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda cls: cls.__name__)
@pytest.mark.parametrize("pattern", PATTERNS)
def test_provider_factory_ends_in_backend(
    provider: type[Provider], pattern: str
) -> None:
    """Each provider builds a pattern's backend with `<pattern>_backend()`."""
    assert callable(getattr(provider, f"{pattern}_backend", None))


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda cls: cls.__name__)
@pytest.mark.parametrize("pattern", PATTERNS)
def test_provider_bare_pattern_name_raises_attribute_error(
    provider: type[Provider], pattern: str
) -> None:
    """The bare pattern name is not a provider method."""
    with pytest.raises(AttributeError):
        getattr(provider, pattern)


@pytest.mark.parametrize(
    "name",
    [
        "lock_backend",
        "readwritelock_backend",
        "leaderelection_backend",
        "schedule_backend",
    ],
)
def test_coordination_backend_property_matches_provider_name(
    name: str,
) -> None:
    """`Coordination` exposes each backend under the provider's method name."""
    coordination = Coordination(MemoryProvider())

    assert getattr(coordination, name) is not None


@pytest.mark.parametrize("name", ["rwlock_backend", "election_backend"])
def test_coordination_short_backend_name_raises_attribute_error(
    name: str,
) -> None:
    """The short property names are gone."""
    coordination = Coordination(MemoryProvider())

    with pytest.raises(AttributeError):
        getattr(coordination, name)


@pytest.mark.parametrize(
    ("pattern", "build"),
    [
        ("lock", lambda p: Coordination(lock=p)),
        ("readwritelock", lambda p: Coordination(readwritelock=p)),
        ("leaderelection", lambda p: Coordination(leaderelection=p)),
        ("schedule", lambda p: Coordination(schedule=p)),
    ],
)
def test_coordination_keyword_matches_pattern_name(
    pattern: str, build: Callable[[Provider], Coordination]
) -> None:
    """Each `Coordination` keyword is the pattern name its backend serves."""
    coordination = build(MemoryProvider())

    assert getattr(coordination, f"{pattern}_backend") is not None


@pytest.mark.parametrize("keyword", ["rwlock", "election"])
def test_coordination_short_keyword_raises_type_error(keyword: str) -> None:
    """The short keywords are gone."""
    build = cast("Callable[..., Coordination]", Coordination)

    with pytest.raises(TypeError, match=keyword):
        build(**{keyword: MemoryProvider()})


@pytest.mark.parametrize("pattern", PATTERNS)
def test_provider_subclass_with_bare_pattern_name_raises_type_error(
    pattern: str,
) -> None:
    """A subclass defining `<pattern>()` without `<pattern>_backend()` is refused."""

    def factory(self: Provider) -> None: ...

    with pytest.raises(
        TypeError,
        match=rf"Custom\.{pattern}\(\) to {pattern}_backend\(\)",
    ):
        type("Custom", (Provider,), {pattern: factory})


def test_provider_subclass_with_bare_name_beside_backend_factory_is_accepted() -> (
    None
):
    """A bare-named helper is fine once the subclass builds the backend."""

    class Custom(Provider):
        def cache(self) -> None: ...

        def cache_backend(self, **kwargs: Any) -> MemoryCacheAdapter:  # noqa: ANN401
            return MemoryCacheAdapter(**kwargs)

    assert Custom.cache_backend is not Provider.cache_backend


def test_provider_subclass_inherits_backend_factory_for_bare_helper() -> None:
    """A helper on a subclass of a shipped provider is judged on the inherited factory."""

    class Custom(MemoryProvider):
        def lock(self) -> None: ...

    assert Custom.lock_backend is MemoryProvider.lock_backend


def test_provider_subclass_with_bare_name_from_mixin_raises_type_error() -> (
    None
):
    """A bare pattern method inherited from a mixin is refused too."""

    class Mixin:
        def lock(self) -> None: ...

    with pytest.raises(
        TypeError, match=r"Custom\.lock\(\) to lock_backend\(\)"
    ):

        class Custom(Mixin, Provider):
            pass


def test_provider_subclass_with_bare_name_attribute_is_accepted() -> None:
    """A plain attribute named after a pattern is not a factory."""

    class Custom(Provider):
        cache = None

    assert Custom.cache is None
