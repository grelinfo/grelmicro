"""Component-level tests: publish resolution, config, registration."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import BaseModel

from grelmicro.outbox import Message, Outbox
from grelmicro.outbox.errors import (
    HandlerAlreadyRegisteredError,
    OutboxSettingsValidationError,
)
from grelmicro.outbox.memory import MemoryOutboxAdapter
from grelmicro.outbox.postgres import PostgresOutboxAdapter
from grelmicro.providers.postgres import PostgresProvider

pytestmark = [pytest.mark.timeout(5)]

URL = "postgresql://test_user:test_password@test_host:1234/test_db"


class OrderPlaced(BaseModel):
    """A sample payload model."""

    order_id: int


def test_defaults_and_kind() -> None:
    """The component exposes its kind and default config."""
    outbox = Outbox(MemoryOutboxAdapter())
    assert outbox.kind == "outbox"
    assert outbox.name == "default"
    assert outbox.config.relay is True
    assert outbox.config.table == "grelmicro_outbox"


def test_config_from_kwargs() -> None:
    """Kwargs flow into the resolved config."""
    outbox = Outbox(MemoryOutboxAdapter(), relay=False, max_attempts=3)
    assert outbox.config.relay is False
    assert outbox.config.max_attempts == 3  # noqa: PLR2004


def test_invalid_config_raises() -> None:
    """An out-of-range setting raises the component error."""
    with pytest.raises(OutboxSettingsValidationError):
        Outbox(MemoryOutboxAdapter(), max_attempts=0)


def test_provider_builds_adapter() -> None:
    """A Provider is turned into the matching adapter with the config table."""
    outbox = Outbox(PostgresProvider(URL), table="custom_outbox")
    assert isinstance(outbox.backend, PostgresOutboxAdapter)
    assert outbox.backend._table == "custom_outbox"


def test_handler_already_registered() -> None:
    """Registering the same topic twice raises."""
    outbox = Outbox(MemoryOutboxAdapter())

    @outbox.handler("job")
    async def one(message: Message[object]) -> None: ...

    with pytest.raises(HandlerAlreadyRegisteredError):

        @outbox.handler("job")
        async def two(message: Message[object]) -> None: ...


async def test_publish_topic_requires_payload() -> None:
    """Publishing a topic string without a payload raises."""
    outbox = Outbox(MemoryOutboxAdapter(), relay=False)
    async with outbox:
        with pytest.raises(TypeError):
            await outbox.publish(None, "job")


async def test_publish_model_rejects_extra_payload() -> None:
    """Publishing a model plus a payload dict raises."""
    outbox = Outbox(MemoryOutboxAdapter(), relay=False)
    async with outbox:
        with pytest.raises(TypeError):
            await outbox.publish(None, OrderPlaced(order_id=1), {"extra": 1})


async def test_publish_model_derives_topic() -> None:
    """A model instance derives its topic from the class name."""
    backend = MemoryOutboxAdapter()
    outbox = Outbox(backend, relay=False)
    async with outbox:
        await outbox.publish(None, OrderPlaced(order_id=7))
    (row,) = backend._rows.values()
    assert row.record.topic == "OrderPlaced"
    assert row.record.payload == {"order_id": 7}


async def test_publish_delay_holds_message_back() -> None:
    """A delayed message is not immediately claimable."""
    backend = MemoryOutboxAdapter()
    outbox = Outbox(backend, relay=False)
    async with outbox:
        await outbox.publish(None, "job", {}, delay=timedelta(hours=1))
        await outbox.publish(None, "later", {}, delay=60.0)
        claimed = await backend.claim(
            topics=["job", "later"], limit=10, lease=5
        )
        assert claimed == []


async def test_purge_older_than_conversion() -> None:
    """`purge(older_than=...)` accepts a timedelta or seconds."""
    backend = MemoryOutboxAdapter()
    outbox = Outbox(backend, relay=False)
    async with outbox:
        await outbox.publish(None, "job", {})
        assert await outbox.purge(older_than=timedelta(hours=1)) == 0
        assert await outbox.purge(older_than=3600.0) == 0
        assert await outbox.purge() == 0


async def test_relay_start_failure_closes_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relay that fails to start still closes the backend, no leak."""
    backend = MemoryOutboxAdapter()
    closed = False

    async def _track_aexit(*_args: object) -> None:
        nonlocal closed
        closed = True

    monkeypatch.setattr(backend, "__aexit__", _track_aexit)

    class _BoomRelay:
        def __init__(self, **_: object) -> None: ...

        async def __aenter__(self) -> None:
            msg = "relay boom"
            raise RuntimeError(msg)

    monkeypatch.setattr("grelmicro.outbox._component.Relay", _BoomRelay)

    outbox = Outbox(backend, relay=True)
    with pytest.raises(RuntimeError, match="relay boom"):
        await outbox.__aenter__()
    assert closed is True
