"""Exact-value fallback tests for config resolution and FQN handling.

These tests pin behavior that loose assertions miss: the config and
kwargs are mutually exclusive (even when only `default` is given),
`from_config` keeps the passed name, and an env FQN keeps its full
dotted path and must resolve to an Exception subclass.
"""

import pytest

from grelmicro.resilience import (
    Fallback,
    FallbackConfig,
    Match,
    Outcome,
)


def test_config_and_default_only_kwarg_conflict() -> None:
    """Passing `config=` together with only `default=` is rejected."""
    cfg = FallbackConfig(when=Match.exception(ValueError), default=1)
    with pytest.raises(TypeError, match="not both"):
        Fallback("conflict", default=2, config=cfg)


def test_from_config_keeps_the_name() -> None:
    """`Fallback.from_config` keeps the given name, not None."""
    cfg = FallbackConfig(when=Match.exception(ValueError), default=1)
    policy = Fallback.from_config("named", cfg)
    assert policy.name == "named"


def test_env_when_resolves_nested_fqn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-segment env FQN resolves through the full module path."""
    monkeypatch.setenv(
        "GREL_FALLBACK_NESTEDFQN_WHEN", "asyncio.exceptions.TimeoutError"
    )
    monkeypatch.setenv("GREL_FALLBACK_NESTEDFQN_DEFAULT", "null")
    policy = Fallback("nestedfqn", env_load=True)
    matcher = policy.config.when
    assert matcher(Outcome.from_exception(TimeoutError())) is True


def test_env_when_rejects_non_exception_fqn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An env FQN that resolves to a non-Exception type is rejected."""
    monkeypatch.setenv("GREL_FALLBACK_NOTEXC_WHEN", "builtins.int")
    monkeypatch.setenv("GREL_FALLBACK_NOTEXC_DEFAULT", "null")
    with pytest.raises(
        (ValueError, TypeError), match="not an Exception subclass"
    ):
        Fallback("notexc", env_load=True)
