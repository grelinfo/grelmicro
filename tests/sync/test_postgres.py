"""Tests for the Postgres Sync Adapter."""

import pytest

from grelmicro.errors import OutOfContextError
from grelmicro.providers.postgres import (
    PostgresProvider,
    PostgresProviderConfigError,
)
from grelmicro.sync._backends import sync_backend_registry
from grelmicro.sync.postgres import PostgresSyncAdapter

pytestmark = [pytest.mark.timeout(1)]

URL = "postgresql://test_user:test_password@test_host:1234/test_db"


@pytest.mark.parametrize(
    "table_name",
    [
        "locks table",
        "%locks",
        "locks;table",
        "locks' OR '1'='1",
        "locks; DROP TABLE users; --",
    ],
)
def test_table_name_invalid(table_name: str) -> None:
    """Invalid SQL identifiers for the table name raise."""
    with pytest.raises(
        ValueError, match=r"Table name '.*' is not a valid SQL identifier"
    ):
        PostgresSyncAdapter(
            provider=PostgresProvider(URL), table_name=table_name
        )


async def test_out_of_context_errors() -> None:
    """Adapter methods raise when called outside the context manager."""
    backend = PostgresSyncAdapter(provider=PostgresProvider(URL))
    name = "lock"
    token = "token"  # noqa: S105

    with pytest.raises(OutOfContextError):
        await backend.acquire(name=name, token=token, duration=1)
    with pytest.raises(OutOfContextError):
        await backend.release(name=name, token=token)
    with pytest.raises(OutOfContextError):
        await backend.locked(name=name)
    with pytest.raises(OutOfContextError):
        await backend.owned(name=name, token=token)


def test_adapter_with_implicit_env_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `provider=`, the adapter builds its own from env vars."""
    monkeypatch.setenv("POSTGRES_URL", URL)

    backend = PostgresSyncAdapter()

    assert backend.provider.url == URL
    assert backend._owns_provider is True


def test_adapter_borrows_external_provider() -> None:
    """An explicit `provider=` is borrowed, not owned."""
    provider = PostgresProvider(URL)
    backend = PostgresSyncAdapter(provider=provider)

    assert backend.provider is provider
    assert backend._owns_provider is False


def test_adapter_env_prefix_passed_to_implicit_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_prefix=` reaches the implicit provider."""
    monkeypatch.setenv("WRITE_POSTGRES_URL", URL)

    backend = PostgresSyncAdapter(env_prefix="WRITE_POSTGRES_")

    assert backend.provider.url == URL
    assert backend.provider.env_prefix == "WRITE_POSTGRES_"


def test_env_validation_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Implicit provider surfaces `PostgresProviderConfigError`."""
    monkeypatch.delenv("POSTGRES_URL", raising=False)
    monkeypatch.delenv("POSTGRES_HOST", raising=False)

    with pytest.raises(PostgresProviderConfigError):
        PostgresSyncAdapter()


def test_constructor_does_not_register() -> None:
    """Constructing the adapter performs no registry writes."""
    sync_backend_registry.reset()

    PostgresSyncAdapter(provider=PostgresProvider(URL))

    assert not sync_backend_registry.is_loaded


def test_custom_table_name() -> None:
    """Custom `table_name=` is stored on the adapter."""
    backend = PostgresSyncAdapter(
        provider=PostgresProvider(URL), table_name="my_locks"
    )

    assert backend._table_name == "my_locks"
