"""Coordination leases take whole seconds or a `timedelta`, never a float."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from grelmicro.coordination.leaderelection import (
    LeaderElection,
    LeaderElectionConfig,
)
from grelmicro.coordination.lock import Lock, LockConfig
from grelmicro.coordination.readwritelock import (
    ReadWriteLock,
    ReadWriteLockConfig,
)
from grelmicro.coordination.tasklock import TaskLock, TaskLockConfig
from grelmicro.errors import SettingsValidationError

WHOLE = 12
UNDER_A_SECOND = timedelta(seconds=12, milliseconds=500)

COMPONENTS = [
    pytest.param(Lock, "lease_duration", id="lock"),
    pytest.param(ReadWriteLock, "lease_duration", id="rwlock"),
    pytest.param(LeaderElection, "lease_duration", id="leader-lease"),
    pytest.param(LeaderElection, "renew_deadline", id="leader-renew-deadline"),
    pytest.param(TaskLock, "lease_duration", id="tasklock"),
    pytest.param(TaskLock, "min_hold_duration", id="tasklock-min-hold"),
]

CONFIGS = [
    pytest.param(LockConfig, "lease_duration", id="lock"),
    pytest.param(ReadWriteLockConfig, "lease_duration", id="rwlock"),
    pytest.param(LeaderElectionConfig, "lease_duration", id="leader-lease"),
    pytest.param(
        LeaderElectionConfig, "renew_deadline", id="leader-renew-deadline"
    ),
    pytest.param(TaskLockConfig, "lease_duration", id="tasklock"),
    pytest.param(TaskLockConfig, "min_hold_duration", id="tasklock-min-hold"),
]

ENVIRONMENT = [
    pytest.param(Lock, "lease_duration", "LOCK", id="lock"),
    pytest.param(ReadWriteLock, "lease_duration", "READWRITELOCK", id="rwlock"),
    pytest.param(
        LeaderElection, "lease_duration", "LEADERELECTION", id="leader-lease"
    ),
    pytest.param(
        LeaderElection,
        "renew_deadline",
        "LEADERELECTION",
        id="leader-renew-deadline",
    ),
    pytest.param(TaskLock, "lease_duration", "TASKLOCK", id="tasklock"),
    pytest.param(
        TaskLock, "min_hold_duration", "TASKLOCK", id="tasklock-min-hold"
    ),
]


@pytest.mark.parametrize(("component", "field"), COMPONENTS)
@pytest.mark.parametrize("value", [12.5, 12.0, True])
def test_coordination_component_float_lease_refused(
    component: type, field: str, value: float
) -> None:
    """A float or a bool lease is refused by the component."""
    with pytest.raises(
        SettingsValidationError, match="whole seconds or a timedelta"
    ):
        component("cart", **{field: value}, env_load=False)


@pytest.mark.parametrize(("config", "field"), CONFIGS)
@pytest.mark.parametrize("value", [12.5, 12.0, True])
def test_coordination_config_float_lease_refused(
    config: type, field: str, value: float
) -> None:
    """A float or a bool lease is refused by the config."""
    with pytest.raises(ValidationError, match="whole seconds or a timedelta"):
        config(**{field: value})


@pytest.mark.parametrize(("component", "field"), COMPONENTS)
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (WHOLE, timedelta(seconds=WHOLE)),
        (UNDER_A_SECOND, UNDER_A_SECOND),
    ],
)
def test_coordination_component_lease_reads_back_as_timedelta(
    component: type, field: str, value: int | timedelta, expected: timedelta
) -> None:
    """Whole seconds and a `timedelta` are taken and read back as a `timedelta`."""
    built = component("cart", **{field: value}, env_load=False)
    assert getattr(built.config, field) == expected


@pytest.mark.parametrize(("component", "field", "kind"), ENVIRONMENT)
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("12", timedelta(seconds=WHOLE)), ("PT12.5S", UNDER_A_SECOND)],
)
def test_coordination_component_lease_from_environment_reads_as_timedelta(
    component: type,
    field: str,
    kind: str,
    raw: str,
    expected: timedelta,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env text is whole seconds or an ISO 8601 duration."""
    monkeypatch.setenv(f"GREL_{kind}_CART_{field.upper()}", raw)
    built = component("cart", env_load=True)
    assert getattr(built.config, field) == expected


def test_lock_lease_under_a_second_from_environment_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lease under a second reads from ISO 8601 text."""
    monkeypatch.setenv("GREL_LOCK_CART_LEASE_DURATION", "PT0.5S")
    lock = Lock("cart", env_load=True)
    assert lock.config.lease_duration == timedelta(milliseconds=500)


def test_lock_decimal_lease_from_environment_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decimal number of seconds from the environment is refused."""
    monkeypatch.setenv("GREL_LOCK_CART_LEASE_DURATION", "0.5")
    with pytest.raises(SettingsValidationError):
        Lock("cart", env_load=True)
