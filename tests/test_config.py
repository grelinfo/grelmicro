"""Tests for grelmicro._config.resolve_config."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, PositiveFloat, ValidationError

from grelmicro._config import resolve_config

DEFAULT_TIMEOUT = 5.0
DEFAULT_RETRIES = 3
KWARG_TIMEOUT = 1.0
ENV_TIMEOUT = 12.5
ENV_RETRIES = 7
SCOPED_ENV_TIMEOUT = 1.5


class _Sample(BaseModel):
    """A small Config used as the resolution target in these tests."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    timeout: PositiveFloat = DEFAULT_TIMEOUT
    retries: int = DEFAULT_RETRIES


def test_kwargs_only_uses_kwargs_and_defaults() -> None:
    """Without env, kwargs fill provided fields and defaults fill the rest."""
    cfg = resolve_config(
        _Sample,
        explicit=None,
        kwargs={"name": "a", "timeout": KWARG_TIMEOUT},
        env_prefix="X_",
        read_env=False,
    )
    assert cfg.name == "a"
    assert cfg.timeout == KWARG_TIMEOUT
    assert cfg.retries == DEFAULT_RETRIES


def test_none_kwargs_are_treated_as_unset() -> None:
    """``None`` kwarg values fall back to defaults."""
    cfg = resolve_config(
        _Sample,
        explicit=None,
        kwargs={"name": "a", "timeout": None, "retries": None},
        env_prefix="X_",
        read_env=False,
    )
    assert cfg.timeout == DEFAULT_TIMEOUT
    assert cfg.retries == DEFAULT_RETRIES


def test_explicit_returned_as_is() -> None:
    """A pre-built instance bypasses other sources."""
    explicit = _Sample(name="seed", timeout=KWARG_TIMEOUT, retries=1)
    cfg = resolve_config(
        _Sample,
        explicit=explicit,
        kwargs={"name": None, "timeout": None, "retries": None},
        env_prefix="X_",
        read_env=True,
    )
    assert cfg is explicit


def test_explicit_with_non_none_kwarg_raises() -> None:
    """Mixing ``explicit`` with a real kwarg is rejected."""
    explicit = _Sample(name="seed")
    with pytest.raises(TypeError, match="config="):
        resolve_config(
            _Sample,
            explicit=explicit,
            kwargs={"name": "seed", "timeout": KWARG_TIMEOUT},
            env_prefix="X_",
            read_env=False,
        )


def test_env_vars_fill_unset_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env vars matching the prefix populate fields not given as kwargs."""
    monkeypatch.setenv("X_TIMEOUT", str(ENV_TIMEOUT))
    monkeypatch.setenv("X_RETRIES", str(ENV_RETRIES))
    cfg = resolve_config(
        _Sample,
        explicit=None,
        kwargs={"name": "a"},
        env_prefix="X_",
        read_env=True,
    )
    assert cfg.timeout == ENV_TIMEOUT
    assert cfg.retries == ENV_RETRIES


def test_kwargs_override_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit kwargs win over env vars."""
    monkeypatch.setenv("X_TIMEOUT", str(ENV_TIMEOUT))
    cfg = resolve_config(
        _Sample,
        explicit=None,
        kwargs={"name": "a", "timeout": KWARG_TIMEOUT},
        env_prefix="X_",
        read_env=True,
    )
    assert cfg.timeout == KWARG_TIMEOUT


def test_read_env_false_ignores_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``read_env=False`` skips env reading entirely."""
    monkeypatch.setenv("X_TIMEOUT", str(ENV_TIMEOUT))
    cfg = resolve_config(
        _Sample,
        explicit=None,
        kwargs={"name": "a"},
        env_prefix="X_",
        read_env=False,
    )
    assert cfg.timeout == DEFAULT_TIMEOUT


def test_env_prefix_scopes_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only env vars under the configured prefix are consumed."""
    monkeypatch.setenv("OTHER_TIMEOUT", str(ENV_TIMEOUT))
    monkeypatch.setenv("X_TIMEOUT", str(SCOPED_ENV_TIMEOUT))
    cfg = resolve_config(
        _Sample,
        explicit=None,
        kwargs={"name": "a"},
        env_prefix="X_",
        read_env=True,
    )
    assert cfg.timeout == SCOPED_ENV_TIMEOUT


def test_invalid_kwarg_raises_validation_error() -> None:
    """``extra="forbid"`` on the Config rejects unknown kwargs."""
    with pytest.raises(ValidationError):
        resolve_config(
            _Sample,
            explicit=None,
            kwargs={"name": "a", "unknown_field": 1},
            env_prefix="X_",
            read_env=False,
        )


def test_invalid_value_raises_validation_error() -> None:
    """Field validators (e.g. ``PositiveFloat``) still apply."""
    invalid_timeout = -1.0
    with pytest.raises(ValidationError):
        resolve_config(
            _Sample,
            explicit=None,
            kwargs={"name": "a", "timeout": invalid_timeout},
            env_prefix="X_",
            read_env=False,
        )


def test_invalid_env_value_raises_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env-supplied values are validated like kwargs."""
    monkeypatch.setenv("X_TIMEOUT", "-1.0")
    with pytest.raises(ValidationError):
        resolve_config(
            _Sample,
            explicit=None,
            kwargs={"name": "a"},
            env_prefix="X_",
            read_env=True,
        )


def test_frozen_is_preserved() -> None:
    """``frozen=True`` from the source class survives the dynamic mixin."""
    cfg = resolve_config(
        _Sample,
        explicit=None,
        kwargs={"name": "a"},
        env_prefix="X_",
        read_env=True,
    )
    with pytest.raises(ValidationError):
        cfg.timeout = ENV_TIMEOUT  # type: ignore[misc]


def test_returns_correct_instance_type() -> None:
    """The returned instance is an instance of ``config_cls``."""
    cfg = resolve_config(
        _Sample,
        explicit=None,
        kwargs={"name": "a"},
        env_prefix="X_",
        read_env=True,
    )
    assert isinstance(cfg, _Sample)
