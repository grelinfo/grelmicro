"""Tests for the Postgres Rate Limiter Adapter."""

import pytest

from grelmicro.providers.postgres import (
    PostgresProvider,
    PostgresProviderConfigError,
)
from grelmicro.resilience.ratelimiter.postgres import PostgresRateLimiterAdapter

pytestmark = [pytest.mark.timeout(1)]

URL = "postgresql://test_user:test_password@test_host:1234/test_db"


@pytest.mark.parametrize(
    "table_name",
    [
        "rate limiter",
        "%rl",
        "rl;table",
        "rl' OR '1'='1",
        "rl; DROP TABLE users; --",
    ],
)
def test_table_name_invalid(table_name: str) -> None:
    """Invalid SQL identifiers for the table name raise."""
    with pytest.raises(
        ValueError, match=r"Table name '.*' is not a valid SQL identifier"
    ):
        PostgresRateLimiterAdapter(
            provider=PostgresProvider(URL), table_name=table_name
        )


def test_adapter_with_implicit_env_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the adapter builds its own from env vars."""
    monkeypatch.setenv("POSTGRES_URL", URL)

    backend = PostgresRateLimiterAdapter()

    assert backend.provider.url == URL
    assert backend._owns_provider is True


def test_adapter_borrows_external_provider() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    provider = PostgresProvider(URL)
    backend = PostgresRateLimiterAdapter(provider=provider)

    assert backend.provider is provider
    assert backend._owns_provider is False


def test_adapter_env_prefix_passed_to_implicit_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_prefix=` reaches the implicit provider."""
    monkeypatch.setenv("WRITE_POSTGRES_URL", URL)

    backend = PostgresRateLimiterAdapter(env_prefix="WRITE_POSTGRES_")

    assert backend.provider.url == URL
    assert backend.provider.env_prefix == "WRITE_POSTGRES_"


def test_env_validation_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Implicit provider surfaces `PostgresProviderConfigError`."""
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("POSTGRES_HOST", raising=False)

    with pytest.raises(PostgresProviderConfigError):
        PostgresRateLimiterAdapter()


def test_custom_table_name() -> None:
    """Custom `table_name=` is stored on the adapter."""
    backend = PostgresRateLimiterAdapter(
        provider=PostgresProvider(URL), table_name="my_rate_limiter"
    )

    assert backend._table_name == "my_rate_limiter"


def test_prefix_stored() -> None:
    """`prefix=` is stored on the adapter."""
    backend = PostgresRateLimiterAdapter(
        provider=PostgresProvider(URL), prefix="myapp:"
    )

    assert backend._prefix == "myapp:"


def test_auto_migrate_flag() -> None:
    """`auto_migrate=` is stored on the adapter."""
    backend = PostgresRateLimiterAdapter(
        provider=PostgresProvider(URL), auto_migrate=False
    )

    assert backend._auto_migrate is False


def test_provider_ratelimiter_factory() -> None:
    """`PostgresProvider.ratelimiter()` builds a `PostgresRateLimiterAdapter`."""
    provider = PostgresProvider(URL)

    backend = provider.ratelimiter()

    assert isinstance(backend, PostgresRateLimiterAdapter)
    assert backend.provider is provider
    assert backend._owns_provider is False
