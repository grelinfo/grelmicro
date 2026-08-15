"""Tests for grelmicro._config.resolve_config."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, PositiveFloat, ValidationError

from grelmicro._config import (
    _build_settings_cls,
    default_env_prefix,
    env_prefixes,
    env_segment,
    parse_csv_or_json,
    resolve_config,
    resolve_config_from_mapping,
)
from grelmicro.coordination import Lock
from grelmicro.errors import SettingsValidationError

_KIND_DEFAULT = 42.0
"""Value of `GREL_LOCK_LEASE_DURATION`, the kind-wide fallback."""

_INSTANCE_OVERRIDE = 7.0
"""Value of `GREL_LOCK_CART_LEASE_DURATION`, the instance variable."""

_KEYWORD = 99.0
"""Value passed as a keyword argument, which beats both env layers."""

_CONFIG_DEFAULT = 60.0
"""`LockConfig.lease_duration` default, reached when no variable matches."""


class _SampleSettingsError(SettingsValidationError):
    """Settings error used to test `error_type` wrapping."""


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
        env_load=False,
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
        env_load=False,
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
        env_load=True,
    )
    assert cfg is explicit


def test_explicit_with_non_none_kwarg_raises() -> None:
    """Mixing ``explicit`` with a real kwarg is rejected."""
    explicit = _Sample(name="seed")
    with pytest.raises(TypeError, match="pre-built config"):
        resolve_config(
            _Sample,
            explicit=explicit,
            kwargs={"name": "seed", "timeout": KWARG_TIMEOUT},
            env_prefix="X_",
            env_load=False,
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
        env_load=True,
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
        env_load=True,
    )
    assert cfg.timeout == KWARG_TIMEOUT


def test_env_load_false_ignores_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``env_load=False`` skips env reading entirely."""
    monkeypatch.setenv("X_TIMEOUT", str(ENV_TIMEOUT))
    cfg = resolve_config(
        _Sample,
        explicit=None,
        kwargs={"name": "a"},
        env_prefix="X_",
        env_load=False,
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
        env_load=True,
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
            env_load=False,
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
            env_load=False,
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
            env_load=True,
        )


def test_error_type_wraps_validation_error() -> None:
    """`error_type` wraps a `ValidationError` into the typed settings error."""
    with pytest.raises(_SampleSettingsError) as exc_info:
        resolve_config(
            _Sample,
            explicit=None,
            kwargs={"name": "a", "timeout": -1.0},
            env_prefix="X_",
            env_load=False,
            error_type=_SampleSettingsError,
        )
    assert isinstance(exc_info.value, SettingsValidationError)


def test_from_mapping_error_type_wraps_validation_error() -> None:
    """`resolve_config_from_mapping` wraps with `error_type` too."""
    current = _Sample(name="a")
    with pytest.raises(_SampleSettingsError) as exc_info:
        resolve_config_from_mapping(
            current,
            env_prefix="X_",
            mapping={"X_TIMEOUT": "-1.0"},
            error_type=_SampleSettingsError,
        )
    assert isinstance(exc_info.value, SettingsValidationError)


def test_from_mapping_without_error_type_raises_validation_error() -> None:
    """Without `error_type`, the raw `ValidationError` propagates."""
    current = _Sample(name="a")
    with pytest.raises(ValidationError):
        resolve_config_from_mapping(
            current,
            env_prefix="X_",
            mapping={"X_TIMEOUT": "-1.0"},
        )


def test_frozen_is_preserved() -> None:
    """``frozen=True`` from the source class survives the dynamic mixin."""
    cfg = resolve_config(
        _Sample,
        explicit=None,
        kwargs={"name": "a"},
        env_prefix="X_",
        env_load=True,
    )
    with pytest.raises(ValidationError):
        cfg.timeout = ENV_TIMEOUT  # ty: ignore[invalid-assignment]


def test_returns_correct_instance_type() -> None:
    """The returned instance is an instance of ``config_cls``."""
    cfg = resolve_config(
        _Sample,
        explicit=None,
        kwargs={"name": "a"},
        env_prefix="X_",
        env_load=True,
    )
    assert isinstance(cfg, _Sample)


# --- env_segment ---


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("cart", "CART"),
        ("Cart", "CART"),
        ("cart_v2", "CART_V2"),
        ("payments-eu", "PAYMENTS_EU"),
        ("cart.v2", "CART_V2"),
        ("foo:bar", "FOO_BAR"),
        ("weather/svc", "WEATHER_SVC"),
        ("a-b-c", "A_B_C"),
        ("my--lock", "MY_LOCK"),
        ("multi...dot", "MULTI_DOT"),
        ("_under_", "UNDER"),
        ("trail-", "TRAIL"),
        ("-lead", "LEAD"),
        ("svc:prod-1", "SVC_PROD_1"),
        ("a", "A"),
    ],
)
def test_env_segment_normalizes(name: str, expected: str) -> None:
    """``env_segment`` produces a portable POSIX env var segment."""
    assert env_segment(name) == expected


@pytest.mark.parametrize("bad", ["", "-", ".", "::", "---", "_-_-_", "/"])
def test_env_segment_rejects_empty_result(bad: str) -> None:
    """A name with no portable characters raises ``ValueError``."""
    with pytest.raises(ValueError, match="empty environment variable"):
        env_segment(bad)


@pytest.mark.parametrize("bad", ["1cart", "9-payments", "42"])
def test_env_segment_rejects_leading_digit(bad: str) -> None:
    """A name producing an env segment starting with a digit raises."""
    with pytest.raises(ValueError, match="starts with a digit"):
        env_segment(bad)


# --- default_env_prefix ---


def test_default_env_prefix_drops_segment_for_default_instance() -> None:
    """The default instance owns the bare ``GREL_{COMPONENT}_`` prefix."""
    assert default_env_prefix("LOCK", "default") == "GREL_LOCK_"


def test_default_env_prefix_keeps_segment_for_named_instance() -> None:
    """A named instance keeps the normalised name segment."""
    assert default_env_prefix("LOCK", "cart") == "GREL_LOCK_CART_"
    assert (
        default_env_prefix("RETRY", "payments-eu") == "GREL_RETRY_PAYMENTS_EU_"
    )


# --- parse_csv_or_json ---


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a,b,c", ["a", "b", "c"]),
        (" a , b , c ", ["a", "b", "c"]),
        ("a", ["a"]),
        (",a,,b,", ["a", "b"]),
        ('["a","b"]', ["a", "b"]),
        ("[1, 2]", [1, 2]),
        ("", []),
        (["a", "b"], ["a", "b"]),
        (("a", "b"), ("a", "b")),
        (None, None),
        (42, 42),
    ],
)
def test_parse_csv_or_json(value: object, expected: object) -> None:
    """CSV strings split on commas, JSON arrays parse, others pass through."""
    assert parse_csv_or_json(value) == expected


# --- _build_settings_cls cache ---


def test_build_settings_cls_returns_same_class_for_same_inputs() -> None:
    """The dynamic Settings subclass is memoized on (config_cls, env_prefix)."""
    first = _build_settings_cls(_Sample, "GREL_SAMPLE_")
    second = _build_settings_cls(_Sample, "GREL_SAMPLE_")
    assert first is second


def test_build_settings_cls_distinct_prefixes_get_distinct_classes() -> None:
    """Different prefixes still produce distinct Settings subclasses."""
    a = _build_settings_cls(_Sample, "GREL_SAMPLE_A_")
    b = _build_settings_cls(_Sample, "GREL_SAMPLE_B_")
    assert a is not b
    assert a.model_config["env_prefix"] == "GREL_SAMPLE_A_"
    assert b.model_config["env_prefix"] == "GREL_SAMPLE_B_"


def test_named_instance_falls_back_to_the_kind_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One variable retunes every instance of a kind.

    A service with twelve named locks sets `GREL_LOCK_LEASE_DURATION` once
    instead of naming each instance.
    """
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_LOCK_LEASE_DURATION", "42")

    assert Lock("cart").config.lease_duration == _KIND_DEFAULT
    assert Lock("checkout").config.lease_duration == _KIND_DEFAULT


def test_instance_variable_beats_the_kind_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The more specific variable wins, and only for its own instance."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_LOCK_LEASE_DURATION", "42")
    monkeypatch.setenv("GREL_LOCK_CART_LEASE_DURATION", "7")

    assert Lock("cart").config.lease_duration == _INSTANCE_OVERRIDE
    assert Lock("checkout").config.lease_duration == _KIND_DEFAULT


def test_keyword_beats_every_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keyword argument still wins over both env layers."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_LOCK_LEASE_DURATION", "42")
    monkeypatch.setenv("GREL_LOCK_CART_LEASE_DURATION", "7")

    assert Lock("cart", lease_duration=99).config.lease_duration == _KEYWORD


def test_custom_env_prefix_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit `env_prefix=` means read exactly these variables.

    The caller took control of the namespace, so grelmicro does not quietly
    add its own kind-wide variables underneath it.
    """
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_LOCK_LEASE_DURATION", "42")

    lock = Lock("cart", env_prefix="MYAPP_LOCK_")

    assert lock.config.lease_duration == _CONFIG_DEFAULT


def test_env_prefixes_helper() -> None:
    """The helper reports no fallback for the default instance or an override."""
    assert env_prefixes("LOCK", "cart") == ("GREL_LOCK_CART_", "GREL_LOCK_")
    assert env_prefixes("LOCK", "default") == ("GREL_LOCK_", None)
    assert env_prefixes("LOCK", "cart", "MYAPP_") == ("MYAPP_", None)
