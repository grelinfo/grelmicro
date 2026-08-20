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
from unittest import mock

import anyio
import pytest
from pydantic import BaseModel, ValidationError, field_validator

import grelmicro
import grelmicro._config
from grelmicro import Grelmicro
from grelmicro._config import env_segment, reconfigure_all
from grelmicro.cache import TTLCache, cached
from grelmicro.config import ExternalConfig, FileConfigAdapter
from grelmicro.coordination import (
    LeaderElection,
    Lock,
    ReadWriteLock,
    TaskLock,
)
from grelmicro.coordination.postgres import PostgresLockAdapter
from grelmicro.errors import SettingsValidationError, _scrub
from grelmicro.log import Log
from grelmicro.metrics import Metrics
from grelmicro.resilience import (
    Bulkhead,
    CircuitBreaker,
    Fallback,
    RateLimiter,
    Retry,
    Shield,
    Timeout,
)
from grelmicro.security import TrustedProxies
from grelmicro.trace import Trace

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
    """One error, so a per-module subclass never comes back by accident.

    Every module is walked, not only the ones named `errors`. Scoping the
    scan to `*.errors` let the provider modules keep a subclass each while
    this passed.
    """
    offenders = []
    scanned = 0
    for info in pkgutil.walk_packages(grelmicro.__path__, prefix="grelmicro."):
        try:
            module = importlib.import_module(info.name)
        except ImportError:
            # An adapter whose optional client library is not installed.
            continue
        scanned += 1
        offenders += [
            f"{info.name}.{name}"
            for name, obj in vars(module).items()
            if isinstance(obj, type)
            and issubclass(obj, SettingsValidationError)
            and obj is not SettingsValidationError
            and obj.__module__ == info.name
        ]
    assert scanned, "no module was scanned, so this proves nothing"
    assert not offenders, (
        f"configuration errors are not split by module: {offenders}"
    )


COMPONENT_CASES: list[tuple[str, str, str]] = [
    ("Metrics.export_interval", "GREL_METRICS_EXPORT_INTERVAL", "metrics"),
    ("Metrics.headers", "GREL_METRICS_HEADERS", "metrics"),
    (
        "Metrics.resource_attributes",
        "GREL_METRICS_RESOURCE_ATTRIBUTES",
        "metrics",
    ),
    ("Trace.sample_ratio", "GREL_TRACE_SAMPLE_RATIO", "trace"),
    ("Trace.headers", "GREL_TRACE_HEADERS", "trace"),
    ("Log.level", "GREL_LOG_LEVEL", "log"),
]
"""Components resolve lazily, so a bad value surfaces when the app opens.

The dict-valued fields are the reason this list exists. pydantic-settings
JSON-decodes a complex field in the source stage, before validation, so a
malformed one raised `pydantic_settings.SettingsError` and never reached
`resolve_config`'s `except ValidationError`.
"""


@pytest.mark.parametrize(
    ("label", "variable", "kind"),
    COMPONENT_CASES,
    ids=[case[0] for case in COMPONENT_CASES],
)
async def test_component_rejects_bad_env_when_the_app_opens(
    label: str,
    variable: str,
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A component reports a bad value the same way a pattern does."""
    build = {"metrics": Metrics, "trace": Trace, "log": Log}[kind]
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv(variable, SECRET)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(SettingsValidationError) as exc_info:
            async with Grelmicro(uses=[build()]):
                pass  # pragma: no cover
    assert SECRET not in str(exc_info.value), (
        f"{label} echoed the rejected value into its error"
    )


_KEPT_ATTEMPTS = 2
"""The value the skipped key must leave untouched."""

_APPLIED_SECONDS = 9
"""The value the key after the bad one must still apply."""

_KEPT_SECONDS = 5
"""The value an instance keeps when its own key is refused."""


def test_reconfigure_all_survives_a_value_pydantic_never_converts() -> None:
    """One bad key must not stop every other instance from updating.

    `Match.exception` raised `TypeError`, which pydantic does not convert,
    so it escaped `reconfigure_all` and aborted the whole resync cycle.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        retry = Retry("resync", when=ValueError, attempts=_KEPT_ATTEMPTS)
        other = Timeout("resync-other", seconds=5)
        anyio.run(
            reconfigure_all,
            {
                "GREL_RETRY_RESYNC_WHEN": "[123]",
                "GREL_TIMEOUT_RESYNC_OTHER_SECONDS": str(_APPLIED_SECONDS),
            },
        )
    assert retry.config.attempts == _KEPT_ATTEMPTS, (
        "the bad key should be skipped"
    )
    assert other.config.seconds == _APPLIED_SECONDS, (
        "a later instance must still update after a bad key"
    )


NON_PATTERN_CASES: list[tuple[str, Callable[[], object]]] = [
    ("cached.lock", lambda: _cached(lock=SECRET)),
    ("cached.early", lambda: _cached(early=943.25)),
    ("cached.stale_ttl", lambda: _cached(stale_ttl=-943.25)),
    ("TrustedProxies.entry", lambda: _trusted([SECRET])),
    ("TrustedProxies.type", lambda: _trusted([12.5])),
    ("TrustedProxies.max_hops", lambda: _trusted(["10.0.0.0/8"], max_hops=-1)),
    ("ExternalConfig.reload_interval", lambda: _external(-943.25)),
    ("Grelmicro.environment", lambda: _app_environment(SECRET)),
]
"""Surfaces outside the pattern and component families that take config."""


def _cached(**kwargs: object) -> object:
    return cached(ttl=60, **kwargs)  # ty: ignore[invalid-argument-type]


def _trusted(networks: list[object], **kwargs: object) -> object:
    return TrustedProxies(networks, **kwargs)  # ty: ignore[invalid-argument-type]


def _external(reload_interval: float) -> object:
    return ExternalConfig(
        config=FileConfigAdapter("/nonexistent"),
        reload_interval=reload_interval,
    )


def _app_environment(value: str) -> object:
    return Grelmicro(environment=value)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    ("label", "build"),
    NON_PATTERN_CASES,
    ids=[case[0] for case in NON_PATTERN_CASES],
)
def test_non_pattern_surface_reports_the_same_way(
    label: str,
    build: Callable[[], object],
) -> None:
    """`cached()` used to raise two different errors from one call."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(SettingsValidationError) as exc_info:
            build()
    assert SECRET not in str(exc_info.value), (
        f"{label} echoed the rejected value into its error"
    )


EMPTY_WHEN_CASES: list[tuple[str, str, Callable[[], object]]] = [
    ("Retry", "GREL_RETRY_EMPTY_WHEN", lambda: Retry("empty")),
    ("Fallback", "GREL_FALLBACK_EMPTY_WHEN", lambda: Fallback("empty")),
]
"""An operator who leaves the variable blank, which parses to no entries."""


@pytest.mark.parametrize(
    ("variable", "build"),
    [(case[1], case[2]) for case in EMPTY_WHEN_CASES],
    ids=[case[0] for case in EMPTY_WHEN_CASES],
)
def test_empty_when_is_reported_not_escaped(
    variable: str,
    build: Callable[[], object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank `when=` reached `Match.exception()` with no arguments.

    That raised `TypeError`, which pydantic never converts, so setting the
    variable to an empty string escaped every documented `except`.
    """
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv(variable, "")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(SettingsValidationError, match="empty"):
            build()


def test_reload_skips_a_validator_that_raises_outside_pydantic() -> None:
    """The reload guard covers whatever a validator raises, not just pydantic.

    grelmicro's own validators raise `ValueError` so pydantic converts them,
    which leaves this branch reachable only from a third-party config class.
    Patching one in is the only way to prove the loop survives it.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        timeout = Timeout("reload-guard", seconds=_KEPT_SECONDS)

        def _explode(*_args: object, **_kwargs: object) -> object:
            msg = "a validator pydantic does not convert"
            raise TypeError(msg)

        with mock.patch.object(
            grelmicro._config, "resolve_config_from_mapping", _explode
        ):
            anyio.run(
                reconfigure_all,
                {"GREL_TIMEOUT_RELOAD_GUARD_SECONDS": "9"},
            )
    assert timeout.config.seconds == _KEPT_SECONDS, (
        "the failing instance keeps its config"
    )


def test_scrub_removes_the_value_only_where_a_message_can_carry_it() -> None:
    """Scrubbing is scoped to the error types that repeat the input.

    A blanket replace destroyed the help text: with `INF` rejected, the
    message listing `'INFO'` as an option lost the very word the operator
    needed. Only a whole-token match is removed, and only for the types
    pydantic builds from the input.
    """
    assert _scrub(
        f"tag {SECRET!r} is unknown", SECRET, "union_tag_invalid"
    ) == ("tag '[redacted]' is unknown")
    assert _scrub(f"got {SECRET}", SECRET, "value_error") == "got [redacted]"
    # A constraint message never repeats the input, so it is left alone.
    assert (
        _scrub("Input should be 'DEBUG', 'INFO'", "INF", "literal_error")
        == "Input should be 'DEBUG', 'INFO'"
    )
    # A near miss must keep the correct spelling it is offering.
    assert "Europe/Zurich" in _scrub(
        "unknown timezone name, did you mean 'Europe/Zurich'",
        "Europe/Zurichh",
        "value_error",
    )
    # `import_error` quotes the module, so the message is replaced outright.
    assert SECRET not in _scrub(
        f"No module named '{SECRET}'", f"{SECRET}.Boom", "import_error"
    )
    # Pydantic reports the input at the level that failed, so the offending
    # string can sit one key down. `union_tag_invalid` hands back the whole
    # mapping, and scrubbing only top-level strings missed it.
    assert SECRET not in _scrub(
        f"Input tag '{SECRET}' found using 'kind'",
        {"kind": SECRET},
        "union_tag_invalid",
    )
    assert SECRET not in _scrub(f"got {SECRET}", [SECRET], "value_error")


def test_scrub_covers_a_custom_error_code() -> None:
    """A code pydantic does not define carries an unreviewed message.

    The backstop listed pydantic's own echoing types, so a
    `PydanticCustomError` raised by grelmicro or by a third-party config
    class was skipped entirely.
    """
    # Act / Assert
    assert SECRET not in _scrub(
        f"rejected {SECRET}", SECRET, "some_third_party_code"
    )
    # grelmicro's own code is reviewed, and its message offers a member of
    # the timezone database that can share a segment with the rejected name.
    assert (
        _scrub(
            "unknown timezone name, did you mean 'Europe/Zurich'",
            "Zurich",
            "time_zone_name",
        )
        == "unknown timezone name, did you mean 'Europe/Zurich'"
    )
    # A code pydantic does define keeps the message it wrote.
    assert (
        _scrub("Input should be 'DEBUG', 'INFO'", "INF", "literal_error")
        == "Input should be 'DEBUG', 'INFO'"
    )


NUMERIC_CANARY = 943257
"""A rejected number standing in for a value read from the environment."""


def test_scrub_removes_a_rejected_number_too() -> None:
    """A number is a value from the environment like any other.

    Only strings were scrubbed, so a validator naming a rejected number
    leaked it. Constraint messages stay untouched: `Input should be greater
    than 0` describes the limit, not what arrived.
    """
    assert str(NUMERIC_CANARY) not in _scrub(
        f"rejected {NUMERIC_CANARY}", NUMERIC_CANARY, "value_error"
    )
    assert str(NUMERIC_CANARY) not in _scrub(
        f"rejected {NUMERIC_CANARY}", [NUMERIC_CANARY], "value_error"
    )
    assert (
        _scrub("Input should be greater than 0", 0, "greater_than")
        == "Input should be greater than 0"
    )
    # A bool would remove the words `True` and `False` from a message that
    # legitimately uses them.
    boolean_input: object = True
    assert _scrub("Input should be True", boolean_input, "value_error") == (
        "Input should be True"
    )


IDENTITY_CASES: list[tuple[str, Callable[[], object]]] = [
    ("Lock name", lambda: Lock("bad name with spaces")),
    ("env segment", lambda: env_segment("123-starts-with-digit")),
    ("table name", lambda: PostgresLockAdapter(table_name="1bad")),
]
"""Names refused at construction, one per validator that checks an identity."""


@pytest.mark.parametrize(
    ("label", "build"),
    IDENTITY_CASES,
    ids=[case[0] for case in IDENTITY_CASES],
)
def test_a_rejected_name_raises_the_one_error(
    label: str, build: Callable[[], object]
) -> None:
    """A bad name raises `SettingsValidationError`, not a bare `ValueError`.

    A caller should not have to know whether their bad input counted as
    configuration or as identity. These raised a bare `ValueError`, so
    `except SettingsValidationError` missed them, and nothing failed when
    the fix was reverted because no test asserted the subtype.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(SettingsValidationError) as exc_info:
            build()
    assert type(exc_info.value) is not ValueError, (
        f"{label} raised a bare ValueError, which escapes "
        f"except SettingsValidationError"
    )


def test_a_rejected_name_is_repeated_in_the_message() -> None:
    """A name is code, so echoing it is the point, unlike a value.

    R3 makes a name the address the environment writes to and R6 keeps
    structure in code, so the name is a literal the caller wrote. The
    message is useless without it, and R7 governs values read from a
    variable.
    """
    with pytest.raises(SettingsValidationError) as exc_info:
        Lock("bad name with spaces")
    assert "bad name with spaces" in str(exc_info.value)


class _LazyProxy:
    """Forwards `__class__` to a target that is not bound yet.

    `werkzeug.local.LocalProxy` and `django.utils.functional.SimpleLazyObject`
    both behave this way while unbound.
    """

    @property
    def __class__(self) -> type:  # type: ignore[override]
        """Raise, the way an unbound proxy does."""
        msg = "proxy is not bound"
        raise RuntimeError(msg)


class _HostileText(str):
    """A string that raises while it is rendered."""

    __slots__ = ()

    def __str__(self) -> str:
        """Raise, so the scrubber cannot read what it must redact."""
        msg = "text is unreadable"
        raise RuntimeError(msg)


class _InterruptingText(str):
    """A string that raises an interrupt while it is rendered."""

    __slots__ = ()

    def __str__(self) -> str:
        """Raise an interrupt, which no guard may swallow."""
        raise KeyboardInterrupt


class _RejectingConfig(BaseModel, arbitrary_types_allowed=True):
    """Rejects whatever it is given, the way a strict validator does."""

    field: object

    @field_validator("field")
    @classmethod
    def _reject(cls, value: object) -> object:  # noqa: ARG003
        """Refuse every value, without naming it."""
        msg = "field is not acceptable"
        raise ValueError(msg)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(_LazyProxy(), id="lazy-proxy"),
        pytest.param(_HostileText("secret"), id="unreadable-text"),
        pytest.param({"nested": _LazyProxy()}, id="proxy-in-a-mapping"),
        pytest.param([_LazyProxy()], id="proxy-in-a-list"),
    ],
)
def test_an_unreadable_value_still_raises_the_settings_error(
    value: object,
) -> None:
    """The scrubber runs while an error is reported, so it cannot fail.

    Deciding what to redact reads `__class__` and renders the value, both
    caller code. A lazy proxy raising from `__class__` replaced the
    `SettingsValidationError` a component owes its caller with whatever
    the proxy raised, which no documented `except` catches.
    """
    with pytest.raises(ValidationError) as exc_info:
        _RejectingConfig(field=value)

    wrapped = SettingsValidationError(exc_info.value)

    assert "field is not acceptable" in str(wrapped)


def test_a_real_interrupt_is_never_swallowed_by_the_scrubber() -> None:
    """The scrubber absorbs an unreadable value, not a genuine Ctrl-C."""
    with pytest.raises(ValidationError) as exc_info:
        _RejectingConfig(field=_InterruptingText("secret"))

    with pytest.raises(KeyboardInterrupt):
        SettingsValidationError(exc_info.value)
