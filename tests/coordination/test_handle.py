"""Tests for the LockHandle dataclass."""

import dataclasses

import pytest

from grelmicro.coordination import LockHandle

pytestmark = [pytest.mark.timeout(1)]


def test_handle_carries_fields() -> None:
    """The handle exposes name, token, and fencing_token."""
    handle = LockHandle(name="cart", token="worker-1", fencing_token=7)

    assert handle.name == "cart"
    assert handle.token == "worker-1"
    assert handle.fencing_token == 7  # noqa: PLR2004


def test_handle_is_frozen() -> None:
    """The handle is immutable."""
    handle = LockHandle(name="cart", token="worker-1", fencing_token=1)

    with pytest.raises(dataclasses.FrozenInstanceError):
        handle.fencing_token = 2  # type: ignore[misc]


def test_handle_uses_slots() -> None:
    """The handle uses slots, so it carries no instance __dict__."""
    handle = LockHandle(name="cart", token="worker-1", fencing_token=1)

    assert not hasattr(handle, "__dict__")
    assert LockHandle.__slots__ == ("name", "token", "fencing_token")
