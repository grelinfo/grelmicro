"""Shield profile configuration tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from grelmicro.resilience.shield import (
    ApiShieldConfig,
    InternalShieldConfig,
    SlowShieldConfig,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


def test_internal_profile_constants() -> None:
    """`internal` profile freezes the spec table values."""
    assert InternalShieldConfig.max_consecutive_failures == 10  # noqa: PLR2004
    assert InternalShieldConfig.initial_max_rate == 100.0  # noqa: PLR2004
    assert InternalShieldConfig.adaptive_burst_capacity == 200.0  # noqa: PLR2004
    assert InternalShieldConfig.min_rate_floor == 1.0
    assert InternalShieldConfig.initial_timeout == 1.0
    assert InternalShieldConfig.timeout_clamp_min == 0.05  # noqa: PLR2004
    assert InternalShieldConfig.timeout_clamp_max == 5.0  # noqa: PLR2004
    assert InternalShieldConfig.backoff_scale == 0.5  # noqa: PLR2004
    assert InternalShieldConfig.backoff_cap == 5.0  # noqa: PLR2004
    assert InternalShieldConfig.profile_name == "internal"


def test_api_profile_constants() -> None:
    """`api` profile freezes the spec table values."""
    assert ApiShieldConfig.max_consecutive_failures == 20  # noqa: PLR2004
    assert ApiShieldConfig.initial_max_rate == 2.0  # noqa: PLR2004
    assert ApiShieldConfig.adaptive_burst_capacity == 5.0  # noqa: PLR2004
    assert ApiShieldConfig.min_rate_floor == 0.25  # noqa: PLR2004
    assert ApiShieldConfig.initial_timeout == 10.0  # noqa: PLR2004
    assert ApiShieldConfig.timeout_clamp_min == 0.5  # noqa: PLR2004
    assert ApiShieldConfig.timeout_clamp_max == 60.0  # noqa: PLR2004
    assert ApiShieldConfig.backoff_scale == 1.0
    assert ApiShieldConfig.backoff_cap == 30.0  # noqa: PLR2004
    assert ApiShieldConfig.profile_name == "api"


def test_slow_profile_constants() -> None:
    """`slow` profile freezes the spec table values."""
    assert SlowShieldConfig.max_consecutive_failures == 5  # noqa: PLR2004
    assert SlowShieldConfig.initial_max_rate == 0.5  # noqa: PLR2004
    assert SlowShieldConfig.adaptive_burst_capacity == 1.0
    assert SlowShieldConfig.min_rate_floor == 0.05  # noqa: PLR2004
    assert SlowShieldConfig.initial_timeout == 120.0  # noqa: PLR2004
    assert SlowShieldConfig.timeout_clamp_min == 5.0  # noqa: PLR2004
    assert SlowShieldConfig.timeout_clamp_max == 600.0  # noqa: PLR2004
    assert SlowShieldConfig.backoff_scale == 2.0  # noqa: PLR2004
    assert SlowShieldConfig.backoff_cap == 60.0  # noqa: PLR2004
    assert SlowShieldConfig.profile_name == "slow"


def test_shield_config_without_when_raises_validation_error() -> None:
    """A Shield config has no default `when`, it names the field."""
    # Act / Assert
    with pytest.raises(ValidationError, match="when"):
        ApiShieldConfig()  # ty: ignore[missing-argument]


def test_shield_config_timeout_errors_field_is_refused() -> None:
    """The removed `timeout_errors` field is refused by the config."""
    # Act / Assert
    with pytest.raises(ValidationError, match="timeout_errors"):
        ApiShieldConfig(when=ValueError, timeout_errors=(ValueError,))  # type: ignore[call-arg]  # ty: ignore[unknown-argument]


def test_config_kind_discriminator() -> None:
    """The `kind` field tags each subclass for the union."""
    assert ApiShieldConfig(when=TimeoutError).kind == "api"
    assert InternalShieldConfig(when=TimeoutError).kind == "internal"
    assert SlowShieldConfig(when=TimeoutError).kind == "slow"


def test_config_extra_forbidden() -> None:
    """Unknown fields are rejected."""
    with pytest.raises(ValidationError):
        ApiShieldConfig(when=TimeoutError, unknown_field="x")  # ty: ignore[unknown-argument]


def test_config_frozen() -> None:
    """Configs are frozen after construction."""
    config = ApiShieldConfig(when=TimeoutError)
    with pytest.raises(ValidationError):
        config.max_rate = 5  # ty: ignore[invalid-assignment]


def test_model_dump_roundtrip() -> None:
    """`model_dump` round-trips through `model_validate`."""
    config = ApiShieldConfig(when=TimeoutError, max_rate=2.5)
    data = config.model_dump()
    rebuilt = ApiShieldConfig.model_validate(data)
    assert rebuilt == config


class _LazyProxy:
    """Forwards `__class__` to a target that is not bound yet."""

    @property
    def __class__(self) -> type:  # type: ignore[override]
        """Raise, the way an unbound proxy does."""
        msg = "proxy is not bound"
        raise RuntimeError(msg)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(_LazyProxy(), id="lazy-proxy"),
        pytest.param((ValueError, _LazyProxy()), id="proxy-inside-a-tuple"),
    ],
)
def test_shield_config_when_unreadable_value_raises_validation_error(
    value: object,
) -> None:
    """A value that cannot be classified is refused, not a crash.

    `isinstance` reads `__class__`, and a lazy proxy raises from it while
    unbound. A validator converts only `ValueError`, so whatever the proxy
    raised escaped the documented error entirely.
    """
    with pytest.raises(ValidationError):
        ApiShieldConfig(when=value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(KeyboardInterrupt, id="alone"),
        pytest.param((KeyboardInterrupt,), id="in-a-tuple"),
        pytest.param([KeyboardInterrupt], id="in-a-list"),
        pytest.param((ValueError, KeyboardInterrupt), id="beside-a-good-one"),
        pytest.param("builtins.KeyboardInterrupt", id="by-name"),
    ],
)
def test_shield_config_when_base_exception_raises_validation_error(
    value: object,
) -> None:
    """A `BaseException`-only type is never retried, so it is never accepted.

    Passed alone or by name it was refused, passed inside a tuple it was
    accepted, and the entry then sat in the config doing nothing.
    """
    with pytest.raises(ValidationError, match=r"xception (subclass|class)"):
        ApiShieldConfig(when=value)


class _UnwalkableTuple(tuple):  # type: ignore[type-arg]  # noqa: SLOT001
    """A tuple subclass that refuses to be walked."""

    def __iter__(self) -> Iterator[object]:
        """Raise, the way a lazily-populated container does when detached."""
        msg = "iter exploded"
        raise RuntimeError(msg)


class _UnwalkableList(list):  # type: ignore[type-arg]
    """A list subclass that refuses to be walked."""

    __slots__ = ()

    def __iter__(self) -> Iterator[object]:
        """Raise, the way a detached cursor does."""
        msg = "iter exploded"
        raise RuntimeError(msg)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(_UnwalkableTuple((ValueError,)), id="tuple"),
        pytest.param(_UnwalkableList([ValueError]), id="list"),
    ],
)
def test_shield_config_when_unwalkable_container_raises_validation_error(
    value: object,
) -> None:
    """Normalizing the entries walks the container, which is caller code."""
    with pytest.raises(ValidationError):
        ApiShieldConfig(when=value)
