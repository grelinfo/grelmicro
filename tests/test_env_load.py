"""Tests for the GREL_ENV_LOAD opt-in flag."""

import logging
import warnings
from datetime import timedelta

import pytest

from grelmicro import GrelmicroConfigWarning
from grelmicro._config import (
    env_load_default,
    flush_ignored_env_reports,
    resolve_config,
)
from grelmicro._deprecation import ENV_LOAD_DEFAULT, PENDING, unread_env
from grelmicro.coordination.lock import Lock, LockConfig
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.errors import EnvLoadOffWarning, GrelmicroFutureWarning
from grelmicro.http import OpsServer
from grelmicro.resilience import Fallback
from tests._future import assert_future_warning
from tests._logs import records_of

LEASE_OVERRIDE = 999
LEASE_FROM_ENV = 42
DEFAULT_LEASE = LockConfig.model_fields["lease_duration"].default


@pytest.fixture
def backend() -> MemoryLockAdapter:
    """Memory backend usable without an event loop."""
    return MemoryLockAdapter()


@pytest.fixture
def _no_env_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override the autouse fixture and turn the global flag off explicitly."""
    monkeypatch.setenv("GREL_ENV_LOAD", "0")


@pytest.fixture
def _env_load_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override the autouse fixture and leave the global flag unset."""
    monkeypatch.delenv("GREL_ENV_LOAD", raising=False)


@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "on"])
def test_env_opt_in_truthy_values(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Truthy values turn the global flag on."""
    monkeypatch.setenv("GREL_ENV_LOAD", value)
    assert env_load_default() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "anything"])
def test_env_opt_in_falsy_values(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Anything else keeps the flag off."""
    monkeypatch.setenv("GREL_ENV_LOAD", value)
    assert env_load_default() is False


@pytest.mark.usefixtures("_no_env_opt_in")
def test_env_ignored_when_flag_off(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the global flag, env vars are not read, and that is reported."""
    monkeypatch.setenv("GREL_LOCK_CART_LEASE_DURATION", str(LEASE_OVERRIDE))
    with pytest.warns(
        GrelmicroConfigWarning, match="GREL_LOCK_CART_LEASE_DURATION"
    ):
        lock = Lock("cart", backend=backend)
    assert lock.config.lease_duration == DEFAULT_LEASE


@pytest.mark.usefixtures("_no_env_opt_in")
def test_ignored_env_is_reported_once(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same ignored variable is reported once, not on every construction."""
    monkeypatch.setenv("GREL_LOCK_CART_LEASE_DURATION", str(LEASE_OVERRIDE))
    with pytest.warns(
        GrelmicroConfigWarning, match="GREL_LOCK_CART_LEASE_DURATION"
    ):
        Lock("cart", backend=backend)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Lock("cart", backend=backend)

    assert [w for w in caught if w.category is GrelmicroConfigWarning] == []


@pytest.mark.usefixtures("_no_env_opt_in")
def test_ignored_env_waits_for_logging_then_logs(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The report reaches the `grelmicro` logger once logging is configured."""
    monkeypatch.setenv("GREL_LOCK_CART_LEASE_DURATION", str(LEASE_OVERRIDE))

    with caplog.at_level(logging.WARNING, logger="grelmicro"):
        with pytest.warns(GrelmicroConfigWarning):
            Lock("cart", backend=backend)

        # Queued, logging is not configured yet.
        assert records_of(caplog, "grelmicro") == []
        flush_ignored_env_reports()

    [record] = records_of(caplog, "grelmicro")
    assert record.__dict__["variable"] == "GREL_LOCK_CART_LEASE_DURATION"
    assert "GREL_ENV_LOAD=1" in record.getMessage()


@pytest.mark.usefixtures("_no_env_opt_in")
def test_ignored_env_is_logged_once(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Configuring logging twice does not repeat the report."""
    monkeypatch.setenv("GREL_LOCK_CART_LEASE_DURATION", str(LEASE_OVERRIDE))

    with caplog.at_level(logging.WARNING, logger="grelmicro"):
        with pytest.warns(GrelmicroConfigWarning):
            Lock("cart", backend=backend)

        flush_ignored_env_reports()
        flush_ignored_env_reports()

    assert len(records_of(caplog, "grelmicro")) == 1


@pytest.mark.usefixtures("_no_env_opt_in")
def test_ignored_env_after_logging_is_logged_straight_away(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A component built after logging is configured reports without waiting."""
    flush_ignored_env_reports()
    monkeypatch.setenv("GREL_LOCK_CART_LEASE_DURATION", str(LEASE_OVERRIDE))

    with (
        caplog.at_level(logging.WARNING, logger="grelmicro"),
        pytest.warns(GrelmicroConfigWarning),
    ):
        Lock("cart", backend=backend)

    [record] = records_of(caplog, "grelmicro")
    assert record.__dict__["variable"] == "GREL_LOCK_CART_LEASE_DURATION"


@pytest.mark.usefixtures("_no_env_opt_in")
def test_unrelated_prefixed_env_is_not_reported(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only declared field names are matched, never a prefix sweep.

    Kubernetes injects `{SVCNAME}_SERVICE_HOST` for every Service, so a
    prefix sweep would warn on every pod start.
    """
    monkeypatch.setenv("GREL_LOCK_CART_SERVICE_HOST", "10.0.0.1")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Lock("cart", backend=backend)

    assert [w for w in caught if w.category is GrelmicroConfigWarning] == []


@pytest.mark.usefixtures("_env_load_unset")
@pytest.mark.parametrize(
    "variable",
    [
        "GREL_OPS_SERVICE_HOST",
        "GREL_OPS_SERVICE_PORT",
        "GREL_OPS_SERVICE_PORT_HTTP",
        "GREL_OPS_PORT_8080_TCP",
        "GREL_OPS_PORT_8080_TCP_PROTO",
        "GREL_OPS_PORT_8080_TCP_PORT",
        "GREL_OPS_PORT_8080_TCP_ADDR",
        "GREL_OPS_ADMIN_PORT_8080_UDP_ADDR",
    ],
)
def test_kubernetes_service_link_companions_are_not_reported(
    variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The variables Kubernetes injects beside `{SVCNAME}_PORT` never warn."""
    monkeypatch.setenv(variable, "tcp://10.0.0.5:8080")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        OpsServer()
        OpsServer(name="admin")

    assert [w for w in caught if issubclass(w.category, Warning)] == []


@pytest.mark.usefixtures("_env_load_unset")
@pytest.mark.parametrize("variable", ["GREL_OPS_PORT", "GREL_OPS_ADMIN_PORT"])
def test_a_kubernetes_service_link_is_not_reported(
    variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A service link is ignored once read, so it never warns it will be read."""
    monkeypatch.setenv(variable, "tcp://10.0.0.5:8080")
    monkeypatch.setenv(f"{variable}_8080_TCP", "tcp://10.0.0.5:8080")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        OpsServer()
        OpsServer(name="admin")

    assert caught == []


@pytest.mark.usefixtures("_no_env_opt_in")
def test_field_passed_as_kwarg_is_not_reported(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keyword argument outranks the environment, so there is nothing to report."""
    monkeypatch.setenv("GREL_LOCK_CART_LEASE_DURATION", str(LEASE_OVERRIDE))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lock = Lock("cart", backend=backend, lease_duration=LEASE_FROM_ENV)

    assert [w for w in caught if w.category is GrelmicroConfigWarning] == []
    assert lock.config.lease_duration == timedelta(seconds=LEASE_FROM_ENV)


@pytest.mark.usefixtures("_no_env_opt_in")
def test_explicit_env_load_false_is_not_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit opt-out is a decision, so it is never reported."""
    monkeypatch.setenv("GREL_LOCK_TEST_LEASE_DURATION", str(LEASE_FROM_ENV))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        resolve_config(
            LockConfig,
            explicit=None,
            kwargs={},
            env_prefix="GREL_LOCK_TEST_",
            env_load=False,
        )

    assert [w for w in caught if w.category is GrelmicroConfigWarning] == []


@pytest.mark.usefixtures("_no_env_opt_in")
def test_per_call_env_load_true_overrides_flag_off(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_load=True` reads env even when the global flag is off."""
    monkeypatch.setenv("GREL_LOCK_CART_LEASE_DURATION", str(LEASE_OVERRIDE))
    lock = Lock("cart", backend=backend, env_load=True)
    assert lock.config.lease_duration == timedelta(seconds=LEASE_OVERRIDE)


def test_per_call_env_load_false_overrides_flag_on(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_load=False` ignores env even when the global flag is on."""
    # autouse fixture sets GREL_ENV_LOAD=true
    monkeypatch.setenv("GREL_LOCK_CART_LEASE_DURATION", str(LEASE_OVERRIDE))
    lock = Lock("cart", backend=backend, env_load=False)
    assert lock.config.lease_duration == DEFAULT_LEASE


@pytest.mark.usefixtures("_no_env_opt_in")
def test_resolve_config_respects_global_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`resolve_config(env_load=None)` follows the global flag."""
    monkeypatch.setenv("GREL_LOCK_TEST_LEASE_DURATION", str(LEASE_FROM_ENV))
    with pytest.warns(
        GrelmicroConfigWarning, match="GREL_LOCK_TEST_LEASE_DURATION"
    ):
        cfg = resolve_config(
            LockConfig,
            explicit=None,
            kwargs={},
            env_prefix="GREL_LOCK_TEST_",
            env_load=None,
        )
    assert cfg.lease_duration == DEFAULT_LEASE  # flag off, env ignored

    monkeypatch.setenv("GREL_ENV_LOAD", "true")
    cfg2 = resolve_config(
        LockConfig,
        explicit=None,
        kwargs={},
        env_prefix="GREL_LOCK_TEST_",
        env_load=None,
    )
    assert cfg2.lease_duration == timedelta(
        seconds=LEASE_FROM_ENV
    )  # flag on, env read


UNREAD = "GREL_LOCK_CART_LEASE_DURATION"
UNREAD_VALUE = "314159"


def _future_messages(caught: list[warnings.WarningMessage]) -> list[str]:
    """Return the text of each `GrelmicroFutureWarning` in `caught`."""
    return [
        str(w.message)
        for w in caught
        if issubclass(w.category, GrelmicroFutureWarning)
    ]


def test_unread_env_is_pending() -> None:
    """The 0.44 default is tracked, so the release that switches it drops it."""
    assert ENV_LOAD_DEFAULT in PENDING
    assert ENV_LOAD_DEFAULT.switch_in == "0.44.0"


def test_unread_env_message_names_the_variable() -> None:
    """The message names the variable and the flag value for each choice."""
    assert unread_env(UNREAD).message == (
        f"`{UNREAD}` is set but not read today, and is read by default "
        "from 0.44.0. Pass `GREL_ENV_LOAD=0` to keep the current behavior, "
        "or `GREL_ENV_LOAD=1` to adopt the new one now."
    )


@pytest.mark.usefixtures("_env_load_unset")
def test_unread_env_warns_while_the_flag_is_unset(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A variable left unread by default warns of the 0.44 default.

    `env_load=False` is a decision, so it warns nothing.
    """
    monkeypatch.setenv(UNREAD, UNREAD_VALUE)

    assert_future_warning(
        unread_env(UNREAD),
        default=lambda: Lock("cart", backend=backend),
        explicit=lambda: Lock("other", backend=backend, env_load=False),
    )


@pytest.mark.parametrize("value", ["", "  "])
def test_blank_flag_counts_as_unset(
    value: str,
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank `GREL_ENV_LOAD` chooses nothing, so the variable still warns."""
    monkeypatch.setenv("GREL_ENV_LOAD", value)
    monkeypatch.setenv(UNREAD, UNREAD_VALUE)

    with pytest.warns(GrelmicroFutureWarning, match=UNREAD):
        Lock("cart", backend=backend)


@pytest.mark.usefixtures("_env_load_unset")
def test_unread_env_replaces_the_diagnostic(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One warning per variable: the future warning, not `EnvLoadOffWarning`."""
    monkeypatch.setenv(UNREAD, UNREAD_VALUE)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lock = Lock("cart", backend=backend)

    assert [w.category for w in caught] == [GrelmicroFutureWarning]
    assert lock.config.lease_duration == DEFAULT_LEASE


@pytest.mark.usefixtures("_env_load_unset")
def test_unread_env_never_carries_the_value(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The variable name is echoed, its value never is."""
    monkeypatch.setenv(UNREAD, UNREAD_VALUE)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Lock("cart", backend=backend)

    [message] = _future_messages(caught)
    assert UNREAD in message
    assert UNREAD_VALUE not in message


@pytest.mark.usefixtures("_env_load_unset")
def test_unread_env_warns_once(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same variable warns once per process, not on every construction."""
    monkeypatch.setenv(UNREAD, UNREAD_VALUE)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Lock("cart", backend=backend)
        Lock("cart", backend=backend)

    assert len(_future_messages(caught)) == 1


@pytest.mark.usefixtures("_env_load_unset")
def test_unread_env_still_logs(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log record of the unread variable is unchanged."""
    monkeypatch.setenv(UNREAD, UNREAD_VALUE)

    with caplog.at_level(logging.WARNING, logger="grelmicro"):
        with pytest.warns(GrelmicroFutureWarning):
            Lock("cart", backend=backend)
        flush_ignored_env_reports()

    [record] = records_of(caplog, "grelmicro")
    assert record.__dict__["variable"] == UNREAD
    assert record.__dict__["diagnostic"] == "env-load-off"


@pytest.mark.parametrize("value", ["1", "true"])
def test_flag_on_reads_without_the_future_warning(
    value: str,
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GREL_ENV_LOAD=1` reads the variable now, so nothing warns."""
    monkeypatch.setenv("GREL_ENV_LOAD", value)
    monkeypatch.setenv(UNREAD, str(LEASE_OVERRIDE))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lock = Lock("cart", backend=backend)

    assert caught == []
    assert lock.config.lease_duration == timedelta(seconds=LEASE_OVERRIDE)


@pytest.mark.parametrize("value", ["0", "false", "off"])
def test_flag_off_keeps_the_diagnostic_only(
    value: str,
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GREL_ENV_LOAD=0` keeps today's report, since nothing changes in 0.44."""
    monkeypatch.setenv("GREL_ENV_LOAD", value)
    monkeypatch.setenv(UNREAD, UNREAD_VALUE)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lock = Lock("cart", backend=backend)

    assert [w.category for w in caught] == [EnvLoadOffWarning]
    assert UNREAD in str(caught[0].message)
    assert lock.config.lease_duration == DEFAULT_LEASE


@pytest.mark.usefixtures("_env_load_unset")
def test_env_load_true_kwarg_reads_without_the_future_warning(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_load=True` reads the variable now, so nothing warns."""
    monkeypatch.setenv(UNREAD, str(LEASE_OVERRIDE))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lock = Lock("cart", backend=backend, env_load=True)

    assert caught == []
    assert lock.config.lease_duration == timedelta(seconds=LEASE_OVERRIDE)


@pytest.mark.usefixtures("_env_load_unset")
def test_env_load_false_kwarg_is_silent(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_load=False` keeps ignoring the variable and warns nothing."""
    monkeypatch.setenv(UNREAD, UNREAD_VALUE)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lock = Lock("cart", backend=backend, env_load=False)

    assert caught == []
    assert lock.config.lease_duration == DEFAULT_LEASE


def _zero(_: BaseException) -> int:
    """Return the fallback value of the `Fallback` tests."""
    return 0


@pytest.mark.usefixtures("_env_load_unset")
def test_unread_fallback_env_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Fallback` resolves its own config and warns of an unread variable too."""
    monkeypatch.setenv("GREL_FALLBACK_PRICES_DEFAULT", "[]")

    assert_future_warning(
        unread_env("GREL_FALLBACK_PRICES_DEFAULT"),
        default=lambda: Fallback("prices", when=ValueError, factory=_zero),
        explicit=lambda: Fallback(
            "other", when=ValueError, factory=_zero, env_load=False
        ),
    )


@pytest.mark.usefixtures("_no_env_opt_in")
def test_unread_fallback_env_reports_the_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the flag off, `Fallback` reports the variable like every component."""
    monkeypatch.setenv("GREL_FALLBACK_PRICES_DEFAULT", "[]")

    with pytest.warns(EnvLoadOffWarning, match="GREL_FALLBACK_PRICES_DEFAULT"):
        Fallback("prices", when=ValueError, factory=_zero)
