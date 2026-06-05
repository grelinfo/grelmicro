"""Tests for entry-point discovery of Providers and Adapters."""

from __future__ import annotations

import pytest

from grelmicro._discovery import (
    adapter_group,
    load_adapter,
    load_provider,
)
from grelmicro.errors import (
    AdapterNotRegisteredError,
    ProviderNotRegisteredError,
)


def test_adapter_group_name() -> None:
    """The adapter group name is derived from the component kind."""
    assert adapter_group("sync") == "grelmicro.sync.adapters"


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
        ("sync", "memory", "MemorySyncAdapter"),
        ("sync", "redis", "RedisSyncAdapter"),
        ("sync", "kubernetes", "KubernetesSyncAdapter"),
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
        load_adapter("sync", "mongo")
    message = str(exc.value)
    assert "'mongo'" in message
    assert "grelmicro.sync.adapters" in message
    assert "redis" in message


def test_load_adapter_unknown_kind_reports_empty_group() -> None:
    """An unknown kind has no registered adapters and says so."""
    with pytest.raises(AdapterNotRegisteredError, match="none installed"):
        load_adapter("nonexistent", "redis")


def test_provider_error_renders_empty_group() -> None:
    """The provider error reads 'none installed' when nothing is registered."""
    assert "none installed" in str(ProviderNotRegisteredError("redis", []))
