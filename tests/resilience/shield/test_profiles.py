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


def test_default_timeout_errors_includes_timeout_error() -> None:
    """The default tuple covers `TimeoutError`."""
    config = ApiShieldConfig()
    assert TimeoutError in config.timeout_errors


def test_effective_timeout_errors_appends_timeout_error() -> None:
    """A user-supplied tuple gets `TimeoutError` appended."""
    config = ApiShieldConfig(timeout_errors=(ValueError,))
    assert TimeoutError in config.effective_timeout_errors()
    assert ValueError in config.effective_timeout_errors()


def test_effective_tuple_skips_duplicate_when_already_covered() -> None:
    """Passing `BaseException`-style ancestors does not duplicate the entry."""

    class MyTimeout(TimeoutError):  # noqa: N818
        pass

    config = ApiShieldConfig(timeout_errors=(MyTimeout, TimeoutError))
    effective = config.effective_timeout_errors()
    assert effective.count(TimeoutError) == 1


def test_config_kind_discriminator() -> None:
    """The `kind` field tags each subclass for the union."""
    assert ApiShieldConfig().kind == "api"
    assert InternalShieldConfig().kind == "internal"
    assert SlowShieldConfig().kind == "slow"


def test_config_extra_forbidden() -> None:
    """Unknown fields are rejected."""
    with pytest.raises(ValidationError):
        ApiShieldConfig(unknown_field="x")  # ty: ignore[unknown-argument]


def test_timeout_errors_rejects_base_exception_class() -> None:
    """`BaseException`-only types cannot be passed as `timeout_errors`."""
    with pytest.raises(ValueError, match="not an Exception subclass"):
        ApiShieldConfig(timeout_errors=KeyboardInterrupt)


def test_config_frozen() -> None:
    """Configs are frozen after construction."""
    config = ApiShieldConfig()
    with pytest.raises(ValidationError):
        config.max_rate = 5  # ty: ignore[invalid-assignment]


def test_model_dump_roundtrip() -> None:
    """`model_dump` round-trips through `model_validate`."""
    config = ApiShieldConfig(max_rate=2.5)
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
def test_timeout_errors_rejects_an_unreadable_value(value: object) -> None:
    """A value that cannot be classified is refused, not a crash.

    `isinstance` reads `__class__`, and a lazy proxy raises from it while
    unbound. A validator converts only `ValueError`, so whatever the proxy
    raised escaped the documented error entirely.
    """
    with pytest.raises(ValidationError):
        ApiShieldConfig(timeout_errors=value)


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
def test_timeout_errors_refuses_a_base_exception_by_every_route(
    value: object,
) -> None:
    """A `BaseException`-only type is never retried, so it is never accepted.

    Passed alone or by name it was refused, passed inside a tuple it was
    accepted, and the entry then sat in the config doing nothing.
    """
    with pytest.raises(ValidationError, match="Exception subclass"):
        ApiShieldConfig(timeout_errors=value)


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
def test_timeout_errors_rejects_a_container_that_refuses_to_be_walked(
    value: object,
) -> None:
    """Normalizing the entries walks the container, which is caller code."""
    with pytest.raises(ValidationError):
        ApiShieldConfig(timeout_errors=value)
