"""The one contract every configuration path owes its caller.

A bad value raises `SettingsValidationError` and never carries the value.
Both halves were false in 0.39.0 for the three classes that resolve their
own configuration instead of going through `resolve_config`, and the
release notes claimed otherwise. A per-class test cannot catch that,
because the rule holds for a family and each member was tested alone.

The second half is the one worth the machinery. Wrapping strips pydantic's
copy of the input, but a validator that names the value it rejected writes
it straight into `msg`, which the wrapper renders verbatim.
"""

import importlib
import pkgutil
import warnings
from collections.abc import Callable

import pytest

import grelmicro
from grelmicro.cache import TTLCache
from grelmicro.coordination import (
    LeaderElection,
    Lock,
    ReadWriteLock,
    TaskLock,
)
from grelmicro.errors import SettingsValidationError
from grelmicro.resilience import (
    Bulkhead,
    CircuitBreaker,
    Fallback,
    RateLimiter,
    Retry,
    Shield,
    Timeout,
)

SECRET = "s3cret-value-never-echoed"
"""Stand-in for a credential an operator put behind a variable by mistake."""

ENV_CASES: list[tuple[str, str, Callable[[], object]]] = [
    ("Timeout", "GREL_TIMEOUT_T_SECONDS", lambda: Timeout("t")),
    ("Retry", "GREL_RETRY_R_WHEN", lambda: Retry("r")),
    ("Bulkhead", "GREL_BULKHEAD_B_MAX_CONCURRENT", lambda: Bulkhead("b")),
    ("Fallback", "GREL_FALLBACK_F_WHEN", lambda: Fallback("f")),
    ("Shield", "GREL_SHIELD_S_MAX_RATE", lambda: Shield("s")),
    ("Shield.profile", "GREL_SHIELD_P_PROFILE", lambda: Shield("p")),
    (
        "RateLimiter",
        "GREL_RATELIMITER_RL_CAPACITY",
        lambda: RateLimiter.token_bucket("rl", refill_rate=1),
    ),
    (
        "CircuitBreaker",
        "GREL_CIRCUITBREAKER_CB_ERROR_THRESHOLD",
        lambda: CircuitBreaker.consecutive_count("cb"),
    ),
    ("Lock", "GREL_LOCK_L_LEASE_DURATION", lambda: Lock("l")),
    (
        "LeaderElection",
        "GREL_LEADERELECTION_LE_LEASE_DURATION",
        lambda: LeaderElection("le"),
    ),
    (
        "ReadWriteLock",
        "GREL_READWRITELOCK_RW_LEASE_DURATION",
        lambda: ReadWriteLock("rw"),
    ),
    ("TaskLock", "GREL_TASKLOCK_TL_LEASE_DURATION", lambda: TaskLock("tl")),
]
"""One env-path case per pattern that reads the environment."""

_MIN_ENV_CASES = 12
"""Floor for the sweep, so a shrunken list cannot pass silently."""

DOTTED_SECRET = "acme.vault.Sk_live_abc"
"""A value shaped like an import path, which the FQN fields try to resolve.

`SECRET` has no dot, so every FQN field rejected it on the first branch
("must be a fully-qualified name") and three later branches went untested.
Those branches raised `TypeError`, which pydantic never converts, so they
escaped `except SettingsValidationError` and `except ValueError` alike, and
two of them rebuilt the rejected value out of the module path and the
attribute name.
"""

FQN_CASES: list[tuple[str, str, Callable[[], object]]] = [
    ("Retry.when", "GREL_RETRY_FQ_WHEN", lambda: Retry("fq")),
    ("Fallback.when", "GREL_FALLBACK_FQ_WHEN", lambda: Fallback("fq")),
    (
        "Shield.timeout_errors",
        "GREL_SHIELD_FQ_TIMEOUT_ERRORS",
        lambda: Shield("fq"),
    ),
]
"""Every field that resolves an env value as a dotted import path."""

RESOLVABLE_NON_EXCEPTION = "os.getcwd"
"""Importable, but not an Exception subclass: the branch that raised `TypeError`."""


@pytest.mark.parametrize(
    ("label", "variable", "build"),
    FQN_CASES,
    ids=[case[0] for case in FQN_CASES],
)
@pytest.mark.parametrize(
    "value",
    [DOTTED_SECRET, RESOLVABLE_NON_EXCEPTION],
    ids=["unimportable", "not-an-exception"],
)
def test_fqn_field_rejects_without_escaping_or_echoing(
    label: str,
    variable: str,
    build: Callable[[], object],
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every branch of the FQN resolvers wraps, and none rebuilds the value."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv(variable, value)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(SettingsValidationError) as exc_info:
            build()
    message = str(exc_info.value)
    for part in value.split("."):
        assert part not in message, (
            f"{label} rebuilt the rejected value from {part!r}"
        )


def test_every_pattern_is_covered() -> None:
    """The sweep refuses to pass on a list someone quietly trimmed."""
    assert len(ENV_CASES) >= _MIN_ENV_CASES


@pytest.mark.parametrize(
    ("label", "variable", "build"),
    ENV_CASES,
    ids=[case[0] for case in ENV_CASES],
)
def test_bad_env_value_raises_settings_error_without_echoing_it(
    label: str,
    variable: str,
    build: Callable[[], object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected env value raises the one error and stays out of it."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv(variable, SECRET)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(SettingsValidationError) as exc_info:
            build()
    assert SECRET not in str(exc_info.value), (
        f"{label} echoed the rejected value into its error"
    )


KWARG_CASES: list[tuple[str, Callable[[], object]]] = [
    ("Timeout", lambda: Timeout("t", seconds=-1)),
    ("Retry", lambda: Retry("r", attempts=-1)),
    ("Bulkhead", lambda: Bulkhead("b", max_concurrent=-1)),
    (
        "Fallback",
        lambda: Fallback("f", when=ValueError, default=1, factory=lambda _: 2),
    ),
    ("Shield", lambda: Shield("s", max_rate=-1)),
    (
        "RateLimiter",
        lambda: RateLimiter.token_bucket("rl", capacity=-1, refill_rate=1),
    ),
    (
        "CircuitBreaker",
        lambda: CircuitBreaker.consecutive_count("cb", error_threshold=-1),
    ),
    ("Lock", lambda: Lock("l", lease_duration=-1)),
    ("TTLCache", lambda: TTLCache(ttl=-1)),
]
"""The kwargs path, which must fail the same way as the env path."""


@pytest.mark.parametrize(
    "build",
    [case[1] for case in KWARG_CASES],
    ids=[case[0] for case in KWARG_CASES],
)
def test_bad_kwarg_raises_settings_error(
    build: Callable[[], object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both construction doors report a bad value the same way."""
    monkeypatch.setenv("GREL_ENV_LOAD", "0")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(SettingsValidationError):
            build()


def test_no_module_declares_a_settings_error_subclass() -> None:
    """One error, so a per-module subclass never comes back by accident."""
    offenders = []
    for info in pkgutil.walk_packages(grelmicro.__path__, prefix="grelmicro."):
        if not info.name.endswith(".errors"):
            continue
        module = importlib.import_module(info.name)
        offenders += [
            f"{info.name}.{name}"
            for name, obj in vars(module).items()
            if isinstance(obj, type)
            and issubclass(obj, SettingsValidationError)
            and obj is not SettingsValidationError
            and obj.__module__ == info.name
        ]
    assert not offenders, (
        f"configuration errors are not split by module: {offenders}"
    )
