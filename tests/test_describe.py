"""Tests for `Grelmicro.describe()` and the `grelmicro check` command."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any, Self

import pytest
from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.__main__ import TargetError, load_target, main
from grelmicro._describe import (
    AppReport,
    _mask,
    describe_provider,
)
from grelmicro._discovery import load_integration
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.health import HealthChecks
from grelmicro.providers._base import Provider
from grelmicro.providers.memory import MemoryProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.sqlite import SQLiteProvider

if TYPE_CHECKING:
    from grelmicro.cache._protocol import CacheBackend

_USAGE_ERROR = 2
"""Exit code for a target that cannot be resolved, distinct from a failed check."""

_TIMEOUT = 5.0
"""A non-credential config value, which must survive masking untouched."""


def test_describe_lists_components() -> None:
    """Every registered component appears with its kind, name, and backend."""
    micro = Grelmicro(
        uses=[Cache(MemoryCacheAdapter()), HealthChecks()],
        environment="development",
    )

    report = micro.describe()

    assert isinstance(report, AppReport)
    assert report.environment == "development"
    kinds = {component.kind for component in report.components}
    assert kinds == {"cache", "health"}
    cache = next(c for c in report.components if c.kind == "cache")
    assert cache.name == "default"
    assert cache.backends == ("MemoryCacheAdapter",)


def test_describe_reports_declined_provider_kinds() -> None:
    """A Provider says which kinds it does not serve, not only which it does.

    This is the answer to "why is my outbox unwired", which today is
    swallowed as a `NotImplementedError` inside the default wiring.
    """
    micro = Grelmicro(
        uses=[SQLiteProvider("sqlite:///:memory:")],
        environment="development",
    )

    report = micro.describe()

    sqlite = next(p for p in report.providers if p.short_name == "sqlite")
    assert "outbox" in sqlite.declines
    assert "leaderelection" in sqlite.declines
    assert "cache" in sqlite.serves


def test_describe_masks_provider_credentials() -> None:
    """A password in a Provider URL never reaches the report."""
    micro = Grelmicro(
        uses=[RedisProvider("redis://user:hunter2@localhost:6379/0")],
        environment="development",
    )

    report = micro.describe()

    redis = next(p for p in report.providers if p.short_name == "redis")
    assert redis.url is not None
    assert "hunter2" not in redis.url
    assert "***" in redis.url
    assert "hunter2" not in report.render()


def test_describe_reports_provider_env_prefix() -> None:
    """A Provider names the variables that repoint it."""
    micro = Grelmicro(
        uses=[RedisProvider("redis://localhost:6379/0")],
        environment="development",
    )

    report = micro.describe()

    redis = next(p for p in report.providers if p.short_name == "redis")
    assert redis.env_prefix == "REDIS_"
    assert "REDIS_*" in report.render()


def test_describe_scope_check_passes_in_development() -> None:
    """A memory backend is the point of `development`, so it is not a failure."""
    micro = Grelmicro(uses=[MemoryProvider()], environment="development")

    report = micro.describe()

    assert report.ok
    assert all(check.status != "fail" for check in report.checks)


def test_describe_scope_check_fails_in_production() -> None:
    """The same memory backend fails the check in a strict tier."""
    micro = Grelmicro(uses=[MemoryProvider()], environment="production")

    report = micro.describe()

    assert not report.ok
    assert any(
        check.name == "backend-scope" and check.status == "fail"
        for check in report.checks
    )


def test_describe_scope_check_warns_when_undeclared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no tier declared the same finding is a warning, not a failure."""
    monkeypatch.delenv("GREL_ENVIRONMENT", raising=False)
    micro = Grelmicro(uses=[MemoryProvider()])

    report = micro.describe()

    assert report.ok
    assert any(check.status == "warn" for check in report.checks)


def test_describe_renders_text() -> None:
    """The rendered report names the environment, components, and checks."""
    micro = Grelmicro(
        uses=[Cache(MemoryCacheAdapter())], environment="development"
    )

    text = micro.describe().render()

    assert "Environment: development" in text
    assert "cache/default" in text
    assert "Checks" in text


def test_describe_on_empty_app() -> None:
    """An app with nothing registered still renders."""
    micro = Grelmicro(environment="development")

    text = micro.describe().render()

    assert "none registered" in text


def test_load_target_requires_module_attribute_form() -> None:
    """A target without a colon says what to write instead."""
    with pytest.raises(TargetError, match=r"module:attribute"):
        load_target("bogus")


def test_load_target_reports_missing_module() -> None:
    """An unimportable module is reported, not raised as ImportError."""
    with pytest.raises(TargetError, match=r"cannot import module"):
        load_target("grelmicro_does_not_exist:micro")


def test_load_target_reports_missing_attribute() -> None:
    """A module without the attribute is reported by name."""
    with pytest.raises(TargetError, match=r"has no attribute"):
        load_target("grelmicro:not_a_real_attribute")


def test_load_target_rejects_non_app() -> None:
    """An attribute that is not a `Grelmicro` is refused."""
    with pytest.raises(TargetError, match=r"not a Grelmicro app"):
        load_target("grelmicro:Component")


def test_main_exits_zero_when_checks_pass(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`grelmicro check` prints the report and exits 0 when it is clean."""
    monkeypatch.setenv("GREL_ENVIRONMENT", "development")

    code = main(["check", "tests.test_describe:_PASSING_APP"])

    assert code == 0
    assert "Environment" in capsys.readouterr().out


def test_main_exits_one_when_a_check_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failing check turns into a non-zero exit code for CI."""
    code = main(["check", "tests.test_describe:_FAILING_APP"])

    assert code == 1
    assert "FAIL" in capsys.readouterr().out


def test_main_exits_two_on_bad_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A target that cannot be resolved is a usage error, not a check failure."""
    code = main(["check", "nope"])

    assert code == _USAGE_ERROR
    assert "error:" in capsys.readouterr().err


_PASSING_APP = Grelmicro(uses=[HealthChecks()], environment="development")
_FAILING_APP = Grelmicro(uses=[MemoryProvider()], environment="production")


def test_describe_flags_a_forgotten_install() -> None:
    """An app that never called `micro.install(app)` fails the ambient check.

    Without the binding middleware a pattern that omits `backend=` raises
    `OutOfContextError` on the first request. A mounted sub-app that forgot
    the call silently resolves against the host's components instead, which
    nothing else reports.
    """
    micro = Grelmicro(uses=[MemoryProvider()], environment="development")
    app = FastAPI()

    report = micro.describe(app)

    assert not report.ok
    assert any(
        check.name == "ambient-binding" and check.status == "fail"
        for check in report.checks
    )


def test_describe_passes_after_install() -> None:
    """Installing the app clears the ambient check."""
    micro = Grelmicro(uses=[MemoryProvider()], environment="development")
    app = FastAPI()
    micro.install(app)

    report = micro.describe(app)

    assert report.ok


def test_describe_warns_on_unknown_framework() -> None:
    """An unrecognized app is reported, never raised. `describe` only answers."""
    micro = Grelmicro(uses=[MemoryProvider()], environment="development")

    report = micro.describe(object())

    assert report.ok
    assert any(
        check.name == "ambient-binding" and check.status == "warn"
        for check in report.checks
    )


def test_describe_masks_secret_config_fields() -> None:
    """A config field whose name reads as a credential is masked by name."""
    assert _mask("password", "hunter2") == "***"
    assert _mask("api_key", "abc") == "***"
    assert _mask("auth_token", "abc") == "***"
    assert _mask("timeout", _TIMEOUT) == _TIMEOUT


def test_describe_masks_url_shaped_config_values() -> None:
    """A URL in any field is redacted even when the name says nothing."""
    masked = _mask("dsn", "postgres://user:hunter2@localhost:5432/db")

    assert "hunter2" not in masked
    assert "***" in masked


def test_describe_counts_a_failing_factory_as_served() -> None:
    """A factory that fails for its own reasons still ships the adapter.

    Only `NotImplementedError` means "this Provider does not serve the kind".
    A missing pool or bad credentials is a different problem, and reporting
    the kind as declined would send the reader down the wrong path.
    """

    class _Provider(Provider):
        short_name = "explosive"

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def cache(self, **kwargs: Any) -> CacheBackend:  # noqa: ANN401, ARG002
            msg = "no pool yet"
            raise RuntimeError(msg)

    report = describe_provider(_Provider())

    assert "cache" in report.serves
    assert "cache" not in report.declines


def test_integration_resolves_a_framework_subclass() -> None:
    """A user's `FastAPI` subclass still resolves through the framework it extends.

    The lookup walks the app class's MRO, so a subclass declared in the
    application's own package matches on `fastapi` rather than falling
    through to "unsupported framework".
    """

    class _MyApp(FastAPI):
        pass

    integration = load_integration(_MyApp())

    assert integration is not None
    assert getattr(integration, "__name__", "") == (
        "grelmicro.integrations.fastapi"
    )


def test_integration_returns_none_for_unknown_app() -> None:
    """An object no integration claims resolves to `None`, not an exception."""
    assert load_integration(object()) is None


def test_install_works_on_a_framework_subclass() -> None:
    """`install` wires a subclass the same way it wires the base class."""

    class _MyApp(FastAPI):
        pass

    micro = Grelmicro(uses=[MemoryProvider()], environment="development")
    app = _MyApp()
    micro.install(app)

    assert micro.check_ambient_binding(app)


def test_main_puts_the_working_directory_on_the_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`grelmicro check` imports an app from the directory it is run in.

    That is how `uvicorn app:app` behaves, and the app module is usually
    importable only from there.
    """
    monkeypatch.setattr(sys, "path", ["/somewhere", "/else"])

    code = main(["check", "tests.test_describe:_PASSING_APP"])

    assert code == 0
    assert sys.path[0] == ""


def test_main_does_not_duplicate_the_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path already carrying the working directory is left alone.

    Both sides are pinned here rather than left to whichever test happened to
    call `main` first, which under `pytest-xdist` depends on the worker.
    """
    monkeypatch.setattr(sys, "path", ["/somewhere", "", "/else"])

    code = main(["check", "tests.test_describe:_PASSING_APP"])

    assert code == 0
    assert sys.path == ["/somewhere", "", "/else"]
