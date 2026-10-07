"""Environment-driven Shield configuration tests."""

from __future__ import annotations

import pytest

from grelmicro.errors import EnvLoadOffWarning, SettingsValidationError
from grelmicro.resilience import Outcome, Shield, SlowShieldConfig


def test_shield_when_from_environment_names_transient_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GREL_SHIELD_{NAME}_WHEN` reads fully-qualified class names."""
    # Arrange
    monkeypatch.setenv("GREL_SHIELD_DB_PROFILE", "internal")
    monkeypatch.setenv(
        "GREL_SHIELD_DB_WHEN", "builtins.KeyError,builtins.ValueError"
    )

    # Act
    s = Shield("db", env_load=True)

    # Assert
    assert s.config.when(Outcome.from_exception(ValueError()))
    assert not s.config.when(Outcome.from_exception(RuntimeError()))
    assert s.config.profile_name == "internal"


def test_shield_when_from_kind_wide_environment_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GREL_SHIELD_WHEN` applies to a Shield whose own variable is unset."""
    # Arrange
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_SHIELD_WHEN", "builtins.ValueError")

    # Act
    s = Shield.api("kindwide")

    # Assert
    assert s.config.when(Outcome.from_exception(ValueError()))


def test_env_load_max_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reads `MAX_RATE` from env."""
    monkeypatch.setenv("GREL_SHIELD_API_PROFILE", "api")
    monkeypatch.setenv("GREL_SHIELD_API_MAX_RATE", "12.5")
    s = Shield("api", when=TimeoutError, env_load=True)
    assert s.config.max_rate == 12.5  # noqa: PLR2004


@pytest.mark.parametrize(
    ("variable", "named"),
    [
        ("GREL_SHIELD_MAX_RATE", "GREL_SHIELD_MAX_RATE"),
        ("GREL_SHIELD_CHECKOUT_MAX_RATE", "GREL_SHIELD_CHECKOUT_MAX_RATE"),
    ],
)
def test_a_bad_number_names_the_variable_that_is_set(
    monkeypatch: pytest.MonkeyPatch, variable: str, named: str
) -> None:
    """The kind-wide variable was reported under the instance address.

    `GREL_SHIELD_MAX_RATE` failed naming `GREL_SHIELD_CHECKOUT_MAX_RATE`,
    which is not set, so the operator was sent to the wrong variable.
    """
    # Arrange
    monkeypatch.setenv(variable, "not-a-number")
    # Act / Assert
    with pytest.raises(SettingsValidationError) as error:
        Shield("checkout", when=TimeoutError, env_load=True)
    assert named in str(error.value)


def test_env_load_with_explicit_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit kwargs win over env values."""

    async def synth(_exc: BaseException) -> str:
        return "default"

    monkeypatch.setenv("GREL_SHIELD_MIX_PROFILE", "api")
    monkeypatch.setenv("GREL_SHIELD_MIX_MAX_RATE", "0.1")
    monkeypatch.setenv("GREL_SHIELD_MIX_WHEN", "builtins.ValueError")
    s = Shield(
        "mix",
        env_load=True,
        when=KeyError,
        max_rate=5.0,
        cache=object(),  # any duck-typed object passes through.
        cache_key=lambda *_, **__: "k",
        fallback=synth,
    )
    assert s.config.max_rate == 5.0  # noqa: PLR2004
    assert not s.config.when(Outcome.from_exception(ValueError()))


def test_env_load_invalid_profile_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown profile name is rejected."""
    monkeypatch.setenv("GREL_SHIELD_X_PROFILE", "weird")
    with pytest.raises(ValueError, match="not a valid profile"):
        Shield("x", when=TimeoutError, env_load=True)


def test_env_load_default_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """`env_load=None` falls through to `env_load_default()`."""
    # The root conftest sets `GREL_ENV_LOAD=true`, so the default is True.
    # Disable it for this test to exercise the off-path.
    monkeypatch.delenv("GREL_ENV_LOAD", raising=False)
    s = Shield("autoload", when=TimeoutError, max_rate=2.0)
    assert s.config.max_rate == 2.0  # noqa: PLR2004


def test_constructor_with_max_rate_no_env() -> None:
    """The non-env max_rate kwarg path is exercised."""
    s = Shield(
        "explicit-max-rate", when=TimeoutError, env_load=False, max_rate=3.0
    )
    assert s.config.max_rate == 3.0  # noqa: PLR2004


def test_from_config_accepts_config() -> None:
    """The pre-built config path works."""
    from grelmicro.resilience import ApiShieldConfig  # noqa: PLC0415

    cfg = ApiShieldConfig(when=TimeoutError, max_rate=2.0)
    s = Shield.from_config("dup", cfg)
    assert s.config is cfg


def test_factory_reads_values_with_the_preset_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A factory resolves values from env, and env cannot move the preset."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_SHIELD_PINNED_MAX_RATE", "5")
    monkeypatch.setenv("GREL_SHIELD_PINNED_PROFILE", "api")

    shield = Shield.slow("pinned", when=TimeoutError)

    assert isinstance(shield.config, SlowShieldConfig)
    assert shield.config.max_rate == 5.0  # noqa: PLR2004


def test_factory_ignores_a_blank_numeric_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A variable set to an empty string leaves the field at its default."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_SHIELD_BLANK_MAX_RATE", "")

    assert Shield.api("blank", when=TimeoutError).config.max_rate is None


def test_keyword_beats_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value passed in code outranks the same variable in the environment."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_SHIELD_KW_MAX_RATE", "5")

    assert (
        Shield.api("kw", when=TimeoutError, max_rate=9.0).config.max_rate == 9.0  # noqa: PLR2004
    )


def test_gate_off_reports_a_shield_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shield reports a variable set while the gate is off, like every pattern."""
    monkeypatch.delenv("GREL_ENV_LOAD", raising=False)
    monkeypatch.setenv("GREL_SHIELD_GATEOFF_MAX_RATE", "5")

    with pytest.warns(EnvLoadOffWarning, match="GREL_SHIELD_GATEOFF_MAX_RATE"):
        Shield.slow("gateoff", when=TimeoutError)


def test_gate_off_keeps_every_passed_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the gate off, each field passed in code reaches the config."""
    monkeypatch.delenv("GREL_ENV_LOAD", raising=False)

    def _key(*_args: object, **_kwargs: object) -> str:
        return "k"

    async def _fallback(*_args: object, **_kwargs: object) -> str:
        return "fb"

    cache = object()
    shield = Shield.api(
        "allvalues",
        when=TimeoutError,
        max_rate=3.0,
        cache=cache,
        cache_key=_key,
        fallback=_fallback,
    )

    assert shield.config.when(Outcome.from_exception(TimeoutError()))
    assert shield.config.max_rate == 3.0  # noqa: PLR2004
    assert shield.config.cache is cache
    assert shield.config.cache_key is _key
    assert shield.config.fallback is _fallback
