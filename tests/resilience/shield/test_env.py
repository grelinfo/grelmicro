"""Environment-driven Shield configuration tests."""

from __future__ import annotations

from typing import Any

import pytest

from grelmicro.resilience import Shield
from grelmicro.resilience.shield._profile import _resolve_fqn


def test_resolve_fqn_resolves_builtin_exception() -> None:
    """`_resolve_fqn` returns the class for a fully-qualified name."""
    assert _resolve_fqn("builtins.TimeoutError") is TimeoutError


def test_resolve_fqn_rejects_bare_name() -> None:
    """A name without a module path is rejected."""
    with pytest.raises(ValueError, match="fully-qualified name"):
        _resolve_fqn("TimeoutError")


def test_resolve_fqn_rejects_missing_module() -> None:
    """A missing module surfaces a clear error."""
    with pytest.raises(ValueError, match="cannot import module"):
        _resolve_fqn("not_a_real_module_anywhere.X")


def test_resolve_fqn_rejects_missing_attribute() -> None:
    """A missing attribute on the module surfaces a clear error."""
    with pytest.raises(ValueError, match="no attribute"):
        _resolve_fqn("builtins.NotAClass")


def test_resolve_fqn_rejects_non_exception() -> None:
    """A name that resolves to a non-exception object is rejected."""
    with pytest.raises(TypeError, match="not an Exception subclass"):
        _resolve_fqn("builtins.int")


def test_env_load_profile_and_timeout_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reads `PROFILE` and `TIMEOUT_ERRORS` from env."""
    monkeypatch.setenv("GREL_SHIELD_DB_PROFILE", "internal")
    monkeypatch.setenv(
        "GREL_SHIELD_DB_TIMEOUT_ERRORS",
        "builtins.TimeoutError,builtins.ValueError",
    )
    s = Shield("db", env_load=True)
    assert TimeoutError in s.config.timeout_errors
    assert ValueError in s.config.timeout_errors
    assert s.config.profile_name == "internal"


def test_env_load_max_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reads `MAX_RATE` from env."""
    monkeypatch.setenv("GREL_SHIELD_API_PROFILE", "api")
    monkeypatch.setenv("GREL_SHIELD_API_MAX_RATE", "12.5")
    s = Shield("api", env_load=True)
    assert s.config.max_rate == 12.5  # noqa: PLR2004


def test_env_load_with_explicit_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit kwargs win over env values."""

    async def synth(_exc: BaseException) -> str:
        return "default"

    monkeypatch.setenv("GREL_SHIELD_MIX_PROFILE", "api")
    monkeypatch.setenv("GREL_SHIELD_MIX_MAX_RATE", "0.1")
    monkeypatch.setenv("GREL_SHIELD_MIX_TIMEOUT_ERRORS", "builtins.ValueError")
    s = Shield(
        "mix",
        env_load=True,
        timeout_errors=(TimeoutError,),
        max_rate=5.0,
        cache=object(),  # any duck-typed object passes through.
        cache_key=lambda *_, **__: "k",
        fallback=synth,
    )
    assert s.config.max_rate == 5.0  # noqa: PLR2004


def test_env_load_invalid_profile_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown profile name is rejected."""
    monkeypatch.setenv("GREL_SHIELD_X_PROFILE", "weird")
    with pytest.raises(ValueError, match="not a valid profile"):
        Shield("x", env_load=True)


def test_env_load_default_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """`env_load=None` falls through to `env_load_default()`."""
    # The root conftest sets `GREL_ENV_LOAD=true`, so the default is True.
    # Disable it for this test to exercise the off-path.
    monkeypatch.delenv("GREL_ENV_LOAD", raising=False)
    s = Shield("autoload", max_rate=2.0)
    assert s.config.max_rate == 2.0  # noqa: PLR2004


def test_constructor_with_max_rate_no_env() -> None:
    """The non-env max_rate kwarg path is exercised."""
    s = Shield("explicit-max-rate", env_load=False, max_rate=3.0)
    assert s.config.max_rate == 3.0  # noqa: PLR2004


def test_constructor_rejects_config_and_kwargs_together() -> None:
    """Passing `config=` with kwargs raises."""
    from grelmicro.resilience import ApiShieldConfig  # noqa: PLC0415

    cfg = ApiShieldConfig(max_rate=2.0)
    with pytest.raises(TypeError, match="pre-built"):
        Shield("dup", config=cfg, max_rate=3.0)


def test_constructor_accepts_config() -> None:
    """The pre-built config path works."""
    from grelmicro.resilience import ApiShieldConfig  # noqa: PLC0415

    cfg = ApiShieldConfig(max_rate=2.0)
    s = Shield("dup", config=cfg)
    assert s.config is cfg


def test_config_accepts_single_exception_class() -> None:
    """A bare exception class is wrapped in a one-tuple."""
    from grelmicro.resilience import ApiShieldConfig  # noqa: PLC0415

    cfg = ApiShieldConfig(timeout_errors=TimeoutError)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    assert TimeoutError in cfg.timeout_errors


def test_config_normalizer_accepts_none() -> None:
    """A `None` input is passed through to pydantic for default handling."""
    from grelmicro.resilience.shield._profile import (  # noqa: PLC0415
        _BaseShieldConfig,
    )

    assert _BaseShieldConfig._normalize_timeout_errors(None) is None


def test_config_normalizer_passes_through_unknown_shapes() -> None:
    """The validator preserves shapes pydantic can validate downstream."""
    from grelmicro.resilience.shield._profile import (  # noqa: PLC0415
        _BaseShieldConfig,
    )

    # The "before" validator returns the value unchanged if it is not a
    # str, list, tuple, or class. Pydantic will then reject it. Use a
    # raw dict to exercise the passthrough branch.
    cls = _BaseShieldConfig._normalize_timeout_errors
    raw: Any = {"not": "valid"}
    assert cls(raw) == raw  # type: ignore[arg-type]
