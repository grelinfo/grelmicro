"""Shield profile configuration tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from grelmicro.resilience.shield import (
    ApiShieldConfig,
    InternalShieldConfig,
    SlowShieldConfig,
)


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
        ApiShieldConfig(unknown_field="x")  # type: ignore[call-arg]  # ty: ignore[unknown-argument]


def test_timeout_errors_rejects_base_exception_class() -> None:
    """`BaseException`-only types cannot be passed as `timeout_errors`."""
    with pytest.raises(TypeError, match="not an Exception subclass"):
        ApiShieldConfig(timeout_errors=KeyboardInterrupt)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_config_frozen() -> None:
    """Configs are frozen after construction."""
    config = ApiShieldConfig()
    with pytest.raises(ValidationError):
        config.max_rate = 5  # type: ignore[misc]  # ty: ignore[invalid-assignment]


def test_model_dump_roundtrip() -> None:
    """`model_dump` round-trips through `model_validate`."""
    config = ApiShieldConfig(max_rate=2.5)
    data = config.model_dump()
    rebuilt = ApiShieldConfig.model_validate(data)
    assert rebuilt == config
