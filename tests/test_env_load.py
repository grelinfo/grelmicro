"""Tests for the GREL_ENV_LOAD flag."""

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
from grelmicro._deprecation import ENV_LOAD_DEFAULT_WARNING, PENDING
from grelmicro.coordination.lock import Lock, LockConfig
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.errors import (
    EnvLoadOffWarning,
    Grelmicro045Warning,
    GrelmicroFutureWarning,
)
from grelmicro.http import OpsServer
from grelmicro.resilience import Fallback
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


@pytest.mark.parametrize("value", ["", "  "])
def test_env_load_blank_values_read(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank flag counts as unset, so the variables are read."""
    monkeypatch.setenv("GREL_ENV_LOAD", value)
    assert env_load_default() is True


@pytest.mark.usefixtures("_env_load_unset")
def test_env_load_unset_reads() -> None:
    """With the flag unset, the variables are read."""
    assert env_load_default() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "anything"])
def test_env_opt_in_falsy_values(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any other value turns the flag off."""
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


@pytest.mark.usefixtures("_env_load_unset")
def test_unset_flag_reads_without_a_warning(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With `GREL_ENV_LOAD` unset, a `GREL_*` variable is read and nothing warns."""
    monkeypatch.setenv(UNREAD, str(LEASE_OVERRIDE))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lock = Lock("cart", backend=backend)

    assert caught == []
    assert lock.config.lease_duration == timedelta(seconds=LEASE_OVERRIDE)


@pytest.mark.parametrize("value", ["", "  "])
def test_blank_flag_reads_without_a_warning(
    value: str,
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank `GREL_ENV_LOAD` counts as unset, so the variable is read."""
    monkeypatch.setenv("GREL_ENV_LOAD", value)
    monkeypatch.setenv(UNREAD, str(LEASE_OVERRIDE))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lock = Lock("cart", backend=backend)

    assert caught == []
    assert lock.config.lease_duration == timedelta(seconds=LEASE_OVERRIDE)


@pytest.mark.usefixtures("_env_load_unset")
def test_unset_flag_keyword_beats_variable(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keyword argument beats the variable, which beats the default."""
    monkeypatch.setenv(UNREAD, str(LEASE_OVERRIDE))

    passed = Lock("cart", backend=backend, lease_duration=LEASE_FROM_ENV)
    from_env = Lock("cart", backend=backend)
    monkeypatch.delenv(UNREAD)
    default = Lock("cart", backend=backend)

    assert passed.config.lease_duration == timedelta(seconds=LEASE_FROM_ENV)
    assert from_env.config.lease_duration == timedelta(seconds=LEASE_OVERRIDE)
    assert default.config.lease_duration == DEFAULT_LEASE


@pytest.mark.parametrize("value", ["1", "true"])
def test_flag_on_reads_without_a_warning(
    value: str,
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GREL_ENV_LOAD=1` reads the variable, and nothing warns."""
    monkeypatch.setenv("GREL_ENV_LOAD", value)
    monkeypatch.setenv(UNREAD, str(LEASE_OVERRIDE))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lock = Lock("cart", backend=backend)

    assert caught == []
    assert lock.config.lease_duration == timedelta(seconds=LEASE_OVERRIDE)


@pytest.mark.parametrize("value", ["0", "false", "off"])
def test_flag_off_keeps_the_variable_unread_and_reports_it(
    value: str,
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GREL_ENV_LOAD=0` leaves the variable unread and reports it."""
    monkeypatch.setenv("GREL_ENV_LOAD", value)
    monkeypatch.setenv(UNREAD, UNREAD_VALUE)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lock = Lock("cart", backend=backend)

    assert [w.category for w in caught] == [EnvLoadOffWarning]
    assert UNREAD in str(caught[0].message)
    assert UNREAD_VALUE not in str(caught[0].message)
    assert lock.config.lease_duration == DEFAULT_LEASE


@pytest.mark.usefixtures("_env_load_unset")
def test_env_load_false_kwarg_is_silent(
    backend: MemoryLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_load=False` leaves the variable unread and warns nothing."""
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
def test_unset_flag_reads_the_fallback_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Fallback` resolves its own config and reads the variable by default too."""
    monkeypatch.setenv("GREL_FALLBACK_PRICES_DEFAULT", "[]")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fallback = Fallback("prices", when=ValueError)

    assert caught == []
    assert fallback.config.default == []


@pytest.mark.usefixtures("_no_env_opt_in")
def test_unread_fallback_env_reports_the_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the flag off, `Fallback` reports the variable like every component."""
    monkeypatch.setenv("GREL_FALLBACK_PRICES_DEFAULT", "[]")

    with pytest.warns(EnvLoadOffWarning, match="GREL_FALLBACK_PRICES_DEFAULT"):
        Fallback("prices", when=ValueError, factory=_zero)


def test_env_load_default_warning_is_pending_removal() -> None:
    """`EnvLoadDefaultWarning` is no longer emitted and is removed in 0.45.0."""
    assert ENV_LOAD_DEFAULT_WARNING in PENDING
    assert ENV_LOAD_DEFAULT_WARNING.removed_in == "0.45.0"
    assert ENV_LOAD_DEFAULT_WARNING.message == (
        "`EnvLoadDefaultWarning` is removed in 0.45.0. "
        "Use `EnvLoadOffWarning` instead."
    )


@pytest.mark.parametrize("module", ["grelmicro", "grelmicro.errors"])
def test_env_load_default_warning_still_imports_and_warns(module: str) -> None:
    """The old name still resolves, warns, and keeps its parent categories."""
    import importlib  # noqa: PLC0415

    package = importlib.import_module(module)

    with pytest.warns(Grelmicro045Warning, match="EnvLoadDefaultWarning"):
        category = package.EnvLoadDefaultWarning

    assert issubclass(category, Grelmicro045Warning)
    assert issubclass(category, EnvLoadOffWarning)
    assert issubclass(category, GrelmicroFutureWarning)
    assert category.code == EnvLoadOffWarning.code
    assert category.__module__ == "grelmicro.errors"
