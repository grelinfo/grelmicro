"""Tests for entry-point discovery of Providers and Adapters."""

from __future__ import annotations

from importlib.metadata import entry_points

import pytest

from grelmicro._discovery import (
    adapter_group,
    load_adapter,
    load_provider,
)
from grelmicro.coordination._component import COORDINATION_BACKENDS
from grelmicro.errors import (
    AdapterNotRegisteredError,
    ProviderNotRegisteredError,
)


def test_adapter_group_name() -> None:
    """The adapter group name is derived from the component kind."""
    assert adapter_group("coordination") == "grelmicro.coordination.adapters"


@pytest.mark.parametrize(
    ("short_name", "qualname"),
    [
        ("redis", "RedisProvider"),
        ("postgres", "PostgresProvider"),
        ("sqlite", "SQLiteProvider"),
    ],
)
def test_load_provider_resolves_first_party(
    short_name: str, qualname: str
) -> None:
    """First-party Providers resolve through the same path as third-party."""
    assert load_provider(short_name).__name__ == qualname


def test_load_provider_unknown_raises() -> None:
    """An unknown provider name lists the names that are installed."""
    with pytest.raises(ProviderNotRegisteredError) as exc:
        load_provider("mongo")
    message = str(exc.value)
    assert "'mongo'" in message
    assert "redis" in message


@pytest.mark.parametrize(
    ("kind", "short_name", "qualname"),
    [
        ("coordination", "memory", "MemoryLockAdapter"),
        ("coordination", "redis", "RedisLockAdapter"),
        ("coordination", "kubernetes", "KubernetesLockAdapter"),
        ("coordination.election", "memory", "MemoryLeaderElectionAdapter"),
        (
            "coordination.election",
            "kubernetes",
            "KubernetesLeaderElectionAdapter",
        ),
        ("cache", "postgres", "PostgresCacheAdapter"),
        ("ratelimiter", "sqlite", "SQLiteRateLimiterAdapter"),
        ("circuitbreaker", "memory", "MemoryCircuitBreakerAdapter"),
    ],
)
def test_load_adapter_resolves_first_party(
    kind: str, short_name: str, qualname: str
) -> None:
    """First-party Adapters resolve by `(kind, short_name)`."""
    assert load_adapter(kind, short_name).__name__ == qualname


def test_load_adapter_unknown_raises() -> None:
    """An unknown adapter name names the kind, group, and installed names."""
    with pytest.raises(AdapterNotRegisteredError) as exc:
        load_adapter("coordination", "mongo")
    message = str(exc.value)
    assert "'mongo'" in message
    assert "grelmicro.coordination.adapters" in message
    assert "redis" in message


def test_load_adapter_unknown_kind_reports_empty_group() -> None:
    """An unknown kind has no registered adapters and says so."""
    with pytest.raises(AdapterNotRegisteredError, match="none installed"):
        load_adapter("nonexistent", "redis")


def test_provider_error_renders_empty_group() -> None:
    """The provider error reads 'none installed' when nothing is registered."""
    assert "none installed" in str(ProviderNotRegisteredError("redis", []))


COORDINATION_GROUPS = {
    "lock": "coordination",
    "rwlock": "coordination.readwritelock",
    "election": "coordination.election",
    "schedule": "coordination.schedule",
}
"""The adapter group each coordination backend resolves through."""


def test_every_coordination_backend_has_an_adapter_group() -> None:
    """A backend with no group cannot be resolved, or extended by a plugin.

    `COORDINATION_BACKENDS` is the list every wiring path reads. A backend
    added there needs an entry-point group too, or its short names resolve to
    nothing.
    """
    assert {slot.keyword for slot in COORDINATION_BACKENDS} == set(
        COORDINATION_GROUPS
    )


@pytest.mark.parametrize("kind", sorted(COORDINATION_GROUPS.values()))
def test_a_coordination_group_ships_its_memory_adapter(kind: str) -> None:
    """Every coordination kind resolves at least the memory short name."""
    assert load_adapter(kind, "memory") is not None


@pytest.mark.parametrize(
    "kind",
    [
        "coordination",
        "coordination.readwritelock",
        "coordination.election",
        "coordination.schedule",
        "cache",
        "ratelimiter",
        "circuitbreaker",
    ],
)
def test_every_registered_adapter_imports(kind: str) -> None:
    """Each entry point names a class that still exists under that path."""
    registered = list(entry_points(group=adapter_group(kind)))

    assert registered
    for entry_point in registered:
        assert isinstance(entry_point.load(), type)
