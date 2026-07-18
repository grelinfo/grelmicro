"""OutboxConfig resolution: kwargs, environment, and validation."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from grelmicro.outbox import Outbox, OutboxConfig
from grelmicro.outbox.errors import OutboxSettingsValidationError
from grelmicro.outbox.memory import MemoryOutboxAdapter

pytestmark = [pytest.mark.timeout(5)]

ENV_ATTEMPTS = 5
KWARG_ATTEMPTS = 2
NAMED_ATTEMPTS = 7
CONFIG_ATTEMPTS = 4


def test_defaults() -> None:
    """The config ships the documented defaults."""
    config = OutboxConfig()
    assert config.table == "grelmicro_outbox"
    assert config.relay is True
    assert config.max_attempts == 10  # noqa: PLR2004
    assert config.notify is True


def test_kwargs_override_defaults() -> None:
    """Kwargs flow into the resolved config."""
    outbox = Outbox(
        MemoryOutboxAdapter(), max_attempts=KWARG_ATTEMPTS, poll_interval=2.0
    )
    assert outbox.config.max_attempts == KWARG_ATTEMPTS
    assert outbox.config.poll_interval == 2.0  # noqa: PLR2004


def test_env_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing settings resolve from `GREL_OUTBOX_` environment variables."""
    monkeypatch.setenv("GREL_OUTBOX_MAX_ATTEMPTS", str(ENV_ATTEMPTS))
    monkeypatch.setenv("GREL_OUTBOX_RELAY", "false")
    outbox = Outbox(MemoryOutboxAdapter())
    assert outbox.config.max_attempts == ENV_ATTEMPTS
    assert outbox.config.relay is False


def test_named_instance_env_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """A named instance reads `GREL_OUTBOX_{NAME}_` variables."""
    monkeypatch.setenv("GREL_OUTBOX_ORDERS_MAX_ATTEMPTS", str(NAMED_ATTEMPTS))
    outbox = Outbox(MemoryOutboxAdapter(), name="orders")
    assert outbox.name == "orders"
    assert outbox.config.max_attempts == NAMED_ATTEMPTS


def test_kwargs_win_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A kwarg overrides the environment."""
    monkeypatch.setenv("GREL_OUTBOX_MAX_ATTEMPTS", str(ENV_ATTEMPTS))
    outbox = Outbox(MemoryOutboxAdapter(), max_attempts=KWARG_ATTEMPTS)
    assert outbox.config.max_attempts == KWARG_ATTEMPTS


def test_explicit_config() -> None:
    """A pre-built config is used as-is."""
    outbox = Outbox(
        MemoryOutboxAdapter(),
        config=OutboxConfig(max_attempts=CONFIG_ATTEMPTS),
    )
    assert outbox.config.max_attempts == CONFIG_ATTEMPTS


def test_explicit_config_and_kwargs_conflict() -> None:
    """Passing both a config and kwargs raises."""
    with pytest.raises(TypeError):
        Outbox(
            MemoryOutboxAdapter(),
            config=OutboxConfig(),
            max_attempts=KWARG_ATTEMPTS,
        )


def test_invalid_value_raises() -> None:
    """An out-of-range setting raises the component error."""
    with pytest.raises(OutboxSettingsValidationError):
        Outbox(MemoryOutboxAdapter(), poll_interval=0)


def test_extra_field_forbidden() -> None:
    """Unknown fields are rejected."""
    with pytest.raises(ValidationError):
        OutboxConfig(unknown=1)  # type: ignore[call-arg]  # ty: ignore[unknown-argument]


def test_config_is_frozen() -> None:
    """The config is immutable."""
    config = OutboxConfig()
    with pytest.raises(ValidationError):
        config.max_attempts = KWARG_ATTEMPTS  # type: ignore[misc]  # ty: ignore[invalid-assignment]


def test_keep_delivered_accepts_bool_and_timedelta() -> None:
    """`keep_delivered` is a bool by default and accepts a retention window."""
    assert OutboxConfig().keep_delivered is False
    assert OutboxConfig(keep_delivered=True).keep_delivered is True
    assert OutboxConfig(keep_delivered=timedelta(days=30)).keep_delivered == (
        timedelta(days=30)
    )


def test_keep_delivered_parses_seconds_over_bool() -> None:
    """A numeric value that is not 0/1 resolves to a duration, not a bool."""
    config = OutboxConfig.model_validate({"keep_delivered": 2_592_000})
    assert config.keep_delivered == timedelta(days=30)


def test_keep_delivered_env_plain_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain seconds count in the environment resolves to a duration."""
    monkeypatch.setenv("GREL_OUTBOX_KEEP_DELIVERED", "2592000")
    outbox = Outbox(MemoryOutboxAdapter())
    assert outbox.config.keep_delivered == timedelta(days=30)


@pytest.mark.parametrize("value", ["true", "1"])
def test_keep_delivered_env_bool(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A boolean spelling, including the numeric `1`, stays a bool."""
    monkeypatch.setenv("GREL_OUTBOX_KEEP_DELIVERED", value)
    assert Outbox(MemoryOutboxAdapter()).config.keep_delivered is True


@pytest.mark.parametrize("window", [timedelta(0), timedelta(seconds=-1)])
def test_keep_delivered_rejects_non_positive_window(
    window: timedelta,
) -> None:
    """A zero or negative retention window is rejected."""
    with pytest.raises(ValidationError):
        OutboxConfig(keep_delivered=window)
