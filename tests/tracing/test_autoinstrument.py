"""Tests for `Trace(instrument=...)` auto-instrumentation.

Covers the selection logic, the per-provider instrument hooks, and the
app-level provider pass. FastAPI request spans are in `test_fastapi_telemetry.py`.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Collection
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from litestar import Litestar
from opentelemetry.instrumentation.dependencies import (
    DependencyConflict,
    DependencyConflictError,
)
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from starlette.applications import Starlette

from grelmicro import Grelmicro
from grelmicro.errors import SettingsValidationError
from grelmicro.providers.memory import MemoryProvider
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.valkey import ValkeyProvider
from grelmicro.trace import Trace
from grelmicro.trace import _autoinstrument as ai
from grelmicro.trace._autoinstrument import (
    InstrumentDirective,
    installed_instrumentors,
    instrument_libraries,
    instrument_providers,
    is_selected,
    provider_library_name,
    uninstrument_libraries,
    uninstrument_providers,
    validate_directive,
)
from tests._logs import records_of

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

REDIS_URL = "redis://localhost:6379/0"
PG_URL = "postgresql://localhost:5432/db"


def _none_trace(*, instrument: InstrumentDirective = True) -> Trace:
    """Return a `Trace` with no exporter (no real OTLP connection)."""
    return Trace(exporter="none", instrument=instrument)


# --- selection logic ---------------------------------------------------------


def test_is_selected_bool() -> None:
    """A bool selects all or nothing."""
    assert is_selected("redis", directive=True) is True
    assert is_selected("redis", directive=False) is False


def test_is_selected_allow_list() -> None:
    """An allow-list selects only listed names."""
    assert is_selected("redis", directive=["redis", "fastapi"]) is True
    assert is_selected("postgres", directive=["redis"]) is False


def test_is_selected_single_name_string() -> None:
    """A bare string is a single-name allow-list, not a char sequence."""
    assert is_selected("redis", directive="redis") is True
    assert is_selected("postgres", directive="redis") is False


def test_is_selected_map_is_all_except() -> None:
    """A map selects all except names mapped to `False`."""
    assert is_selected("redis", directive={"redis": False}) is False
    assert is_selected("postgres", directive={"redis": False}) is True
    assert is_selected("redis", directive={"redis": True}) is True


def test_validate_directive_allows_bool_and_known_names() -> None:
    """A bool needs no validation; known names and a single string pass."""
    validate_directive(directive=True, known={"redis"})
    validate_directive(["redis"], known={"redis", "fastapi"})
    validate_directive("redis", known={"redis"})
    validate_directive({"redis": False}, known={"redis"})


def test_validate_directive_rejects_unknown_names() -> None:
    """An unknown name in a string, list, or map raises (typo guard)."""
    with pytest.raises(SettingsValidationError, match="unknown targets"):
        validate_directive("typo", known={"redis"})
    with pytest.raises(SettingsValidationError, match="unknown targets"):
        validate_directive(["reddis"], known={"redis"})
    with pytest.raises(SettingsValidationError, match="unknown targets"):
        validate_directive({"typo": False}, known={"redis"})


def test_validate_directive_rejects_non_bool_map_values() -> None:
    """A map value that is not a bool raises, so options are not swallowed."""
    with pytest.raises(SettingsValidationError, match="non-bool"):
        validate_directive({"redis": {"opt": 1}}, known={"redis"})  # ty: ignore[invalid-argument-type]


# --- orchestration with spy providers ----------------------------------------


class _SpyProvider:
    short_name = "spy"

    def __init__(self, *, attaches: bool = True) -> None:
        self._attaches = attaches
        self.tracer_provider: object | None = None
        self.uninstrumented = False

    def instrument(self, tracer_provider: object) -> bool:
        self.tracer_provider = tracer_provider
        return self._attaches

    def uninstrument(self) -> None:
        self.uninstrumented = True


def test_instrument_providers_selected_and_reversible() -> None:
    """A selected provider is instrumented and later un-instrumented."""
    provider = _SpyProvider()
    tracer_provider = object()
    out = instrument_providers([provider], tracer_provider, directive=True)  # ty: ignore[invalid-argument-type]
    assert out == [provider]
    assert provider.tracer_provider is tracer_provider

    uninstrument_providers(out)
    assert provider.uninstrumented is True


def test_instrument_providers_skips_unselected() -> None:
    """An unselected provider is left untouched."""
    provider = _SpyProvider()
    assert instrument_providers([provider], object(), directive=False) == []  # ty: ignore[invalid-argument-type]
    assert provider.tracer_provider is None


def test_instrument_providers_warns_on_explicit_miss(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A named provider that cannot attach warns; not added to the result."""
    provider = _SpyProvider(attaches=False)
    caplog.set_level("WARNING")
    assert instrument_providers([provider], object(), directive=["spy"]) == []  # ty: ignore[invalid-argument-type]
    assert "no instrumentor" in caplog.text


def test_instrument_providers_silent_on_default_miss(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A default-on provider that cannot attach stays quiet (no warning)."""
    provider = _SpyProvider(attaches=False)
    caplog.set_level("WARNING")
    assert instrument_providers([provider], object(), directive=True) == []  # ty: ignore[invalid-argument-type]
    assert records_of(caplog, "grelmicro") == []


def test_instrument_providers_swallows_instrument_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A provider whose instrument raises is logged and skipped."""

    class _Boom:
        short_name = "boom"

        def instrument(self, tracer_provider: object) -> bool:  # noqa: ARG002
            msg = "boom"
            raise RuntimeError(msg)

    caplog.set_level("WARNING")
    assert instrument_providers([_Boom()], object(), directive=True) == []  # ty: ignore[invalid-argument-type]
    assert "Failed to auto-instrument" in caplog.text


def test_uninstrument_providers_swallows_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A provider whose uninstrument raises is logged, not propagated."""

    class _Boom:
        short_name = "boom"

        def uninstrument(self) -> None:
            msg = "boom"
            raise RuntimeError(msg)

    caplog.set_level("WARNING")
    uninstrument_providers([_Boom()])  # ty: ignore[invalid-argument-type]
    assert "Failed to un-instrument" in caplog.text


# --- library sweep -----------------------------------------------------------


class _FakeInstrumentor:
    """Stand-in for an OTel instrumentor instance."""

    def __init__(self) -> None:
        self.tracer_provider: object | None = None
        self.uninstrumented = False
        self.raise_on_instrument = False

    def instrument(self, *, tracer_provider: object | None = None) -> None:
        if self.raise_on_instrument:
            msg = "boom"
            raise RuntimeError(msg)
        self.tracer_provider = tracer_provider

    def uninstrument(self) -> None:
        self.uninstrumented = True


class _FakeEntryPoint:
    """Stand-in for an `opentelemetry_instrumentor` entry point."""

    def __init__(self, name: str, instance: object) -> None:
        self.name = name
        self._instance = instance

    def load(self) -> object:
        instance = self._instance
        return lambda: instance


def _fake_entries(
    monkeypatch: pytest.MonkeyPatch, names: list[str]
) -> dict[str, _FakeInstrumentor]:
    """Patch the entry-point lookup with fakes; return them keyed by name."""
    instances = {name: _FakeInstrumentor() for name in names}
    entries = {
        name: _FakeEntryPoint(name, instance)
        for name, instance in instances.items()
    }
    monkeypatch.setattr(ai, "_instrumentor_entry_points", lambda: entries)
    return instances


def test_provider_library_name_maps_postgres_to_asyncpg() -> None:
    """The Postgres provider covers the asyncpg instrumentor; others match."""
    assert provider_library_name("postgres") == "asyncpg"
    assert provider_library_name("redis") == "redis"
    assert provider_library_name("valkey") == "valkey"


def test_installed_instrumentors_includes_bundled_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The test env installs the instrumentation extra, so these are present."""
    monkeypatch.undo()  # use real entry-point discovery, not the conftest stub
    names = installed_instrumentors()
    assert {"redis", "asyncpg"} <= names


def test_instrument_libraries_sweeps_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every installed, selected instrumentor is attached and reversible."""
    instances = _fake_entries(monkeypatch, ["asyncpg", "httpx"])
    tracer_provider = object()
    out = instrument_libraries(tracer_provider, directive=True, exclude=set())
    assert instances["asyncpg"].tracer_provider is tracer_provider
    assert instances["httpx"].tracer_provider is tracer_provider
    assert len(out) == 2  # noqa: PLR2004

    uninstrument_libraries(out)
    assert all(inst.uninstrumented for inst in instances.values())


def test_instrument_libraries_honors_exclude(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A library a provider already owns is skipped by the sweep."""
    instances = _fake_entries(monkeypatch, ["redis", "httpx"])
    instrument_libraries(object(), directive=True, exclude={"redis"})
    assert instances["redis"].tracer_provider is None
    assert instances["httpx"].tracer_provider is not None


def test_instrument_libraries_honors_deny_directive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `{name: False}` directive deselects that library."""
    instances = _fake_entries(monkeypatch, ["asyncpg", "httpx"])
    instrument_libraries(object(), {"httpx": False}, exclude=set())
    assert instances["asyncpg"].tracer_provider is not None
    assert instances["httpx"].tracer_provider is None


def test_instrument_libraries_drops_sqlalchemy_for_asyncpg(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SQLAlchemy is dropped when asyncpg is also active, to avoid double spans."""
    instances = _fake_entries(monkeypatch, ["asyncpg", "sqlalchemy"])
    caplog.set_level("WARNING")
    instrument_libraries(object(), directive=True, exclude=set())
    assert instances["asyncpg"].tracer_provider is not None
    assert instances["sqlalchemy"].tracer_provider is None
    assert "duplicate spans" in caplog.text


def test_instrument_libraries_swallows_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A library whose instrument raises is logged and skipped."""
    instances = _fake_entries(monkeypatch, ["asyncpg", "httpx"])
    instances["asyncpg"].raise_on_instrument = True
    caplog.set_level("WARNING")
    out = instrument_libraries(object(), directive=True, exclude=set())
    assert instances["httpx"].tracer_provider is not None
    assert len(out) == 1
    assert "Failed to auto-instrument library" in caplog.text


class _MissingLibraryInstrumentor(BaseInstrumentor):
    """An instrumentor whose library is not installed."""

    def instrumentation_dependencies(self) -> Collection[str]:
        return ("grelmicro-not-a-real-package >= 1.0",)

    def _instrument(self, **_kwargs: object) -> None:
        msg = "never instrumented"
        raise AssertionError(msg)

    def _uninstrument(self, **_kwargs: object) -> None:
        msg = "never instrumented"
        raise AssertionError(msg)


def test_instrument_libraries_skips_a_library_that_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An instrumentor whose library is missing is skipped with no warning or error."""
    entries = {
        "missing": _FakeEntryPoint("missing", _MissingLibraryInstrumentor())
    }
    monkeypatch.setattr(ai, "_instrumentor_entry_points", lambda: entries)
    caplog.set_level("DEBUG")

    out = instrument_libraries(object(), directive=True, exclude=set())
    uninstrument_libraries(out)

    assert out == []
    assert [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ] == []
    assert any(
        "Skipped library 'missing'" in record.getMessage()
        for record in records_of(caplog, "grelmicro")
    )


class _OldVersionInstrumentor(BaseInstrumentor):
    """An instrumentor whose package is installed at a version it does not support."""

    def instrumentation_dependencies(self) -> Collection[str]:
        return ("pytest < 1.0",)

    def _instrument(self, **_kwargs: object) -> None:
        msg = "never instrumented"
        raise AssertionError(msg)

    def _uninstrument(self, **_kwargs: object) -> None:
        msg = "never instrumented"
        raise AssertionError(msg)


def test_instrument_libraries_names_an_installed_package_at_an_unsupported_version(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A package at a version the instrumentor does not support logs an error."""
    entries = {"old": _FakeEntryPoint("old", _OldVersionInstrumentor())}
    monkeypatch.setattr(ai, "_instrumentor_entry_points", lambda: entries)
    caplog.set_level("DEBUG")

    out = instrument_libraries(object(), directive=True, exclude=set())

    assert out == []
    errors = [
        r for r in records_of(caplog, "grelmicro") if r.levelno == logging.ERROR
    ]
    assert len(errors) == 1
    assert "pytest < 1.0" in errors[0].getMessage()
    assert f"pytest {pytest.__version__}" in errors[0].getMessage()


class _RecordingBaseInstrumentor(BaseInstrumentor):
    """An instrumentor whose package is installed, recording its calls."""

    def __init__(self) -> None:
        self.instrumented_with: dict[str, object] | None = None
        self.fail = False

    def instrumentation_dependencies(self) -> Collection[str]:
        return ("pytest",)

    def _instrument(self, **kwargs: object) -> None:
        if self.fail:
            msg = "boom"
            raise RuntimeError(msg)
        self.instrumented_with = kwargs

    def _uninstrument(self, **_kwargs: object) -> None:
        self.instrumented_with = None


class _NarrowBaseInstrumentor(BaseInstrumentor):
    """A subclass that overrides `instrument` with a narrower signature."""

    def __init__(self) -> None:
        self.tracer_provider: object | None = None

    def instrumentation_dependencies(self) -> Collection[str]:
        return ()

    def instrument(self, *, tracer_provider: object | None = None) -> None:  # type: ignore[override]  # ty: ignore[incompatible-method-override]
        self.tracer_provider = tracer_provider

    def _instrument(self, **_kwargs: object) -> None:
        pass

    def _uninstrument(self, **_kwargs: object) -> None:
        pass


class _ConflictingInstrumentor:
    """An instrumentor that reports a conflict on its required packages."""

    def __init__(self, conflict: DependencyConflict) -> None:
        self.conflict = conflict

    def instrument(self, *, tracer_provider: object | None = None) -> None:  # noqa: ARG002
        raise DependencyConflictError(self.conflict)

    def uninstrument(self) -> None:
        msg = "never instrumented"
        raise AssertionError(msg)


class _BrokenEntryPoint:
    """An entry point whose module needs a package that is not installed."""

    name = "broken"

    def load(self) -> object:
        msg = "No module named 'not_installed'"
        raise ModuleNotFoundError(msg, name="not_installed")


def _warnings_and_errors(
    caplog: pytest.LogCaptureFixture,
) -> list[logging.LogRecord]:
    """Return the captured grelmicro records at warning or above."""
    return [
        record
        for record in records_of(caplog, "grelmicro")
        if record.levelno >= logging.WARNING
    ]


def test_instrument_libraries_instruments_a_compatible_base_instrumentor(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An instrumentor whose package is installed is attached to the provider."""
    instrumentor = _RecordingBaseInstrumentor()
    entries = {"ok": _FakeEntryPoint("ok", instrumentor)}
    monkeypatch.setattr(ai, "_instrumentor_entry_points", lambda: entries)
    provider = object()

    out = instrument_libraries(provider, directive=True, exclude=set())

    assert out == [instrumentor]
    assert instrumentor.instrumented_with == {"tracer_provider": provider}
    assert _warnings_and_errors(caplog) == []
    uninstrument_libraries(out)


def test_instrument_libraries_keeps_the_call_of_an_overridden_instrument(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A subclass with its own `instrument` is called as in 0.45.1."""
    instrumentor = _NarrowBaseInstrumentor()
    entries = {"narrow": _FakeEntryPoint("narrow", instrumentor)}
    monkeypatch.setattr(ai, "_instrumentor_entry_points", lambda: entries)
    provider = object()

    out = instrument_libraries(provider, directive=True, exclude=set())

    assert out == [instrumentor]
    assert instrumentor.tracer_provider is provider
    assert _warnings_and_errors(caplog) == []


def test_instrument_libraries_goes_on_after_a_base_instrumentor_raises(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing instrumentor warns, and the others are still attached."""
    failing = _RecordingBaseInstrumentor()
    failing.fail = True
    working = _FakeInstrumentor()
    entries = {
        "failing": _FakeEntryPoint("failing", failing),
        "working": _FakeEntryPoint("working", working),
    }
    monkeypatch.setattr(ai, "_instrumentor_entry_points", lambda: entries)
    caplog.set_level("WARNING")

    out = instrument_libraries(object(), directive=True, exclude=set())

    assert out == [working]
    assert "Failed to auto-instrument library 'failing'" in caplog.text


@pytest.mark.parametrize(
    ("conflict", "level"),
    [
        (DependencyConflict(required_any=["a >= 2", "b >= 2"]), logging.DEBUG),
        (
            DependencyConflict(
                required_any=["a >= 2", "b >= 2"], found_any=["a==1.0"]
            ),
            logging.ERROR,
        ),
    ],
    ids=["none-installed", "only-an-old-one-installed"],
)
def test_instrument_libraries_reads_an_either_or_conflict(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    conflict: DependencyConflict,
    level: int,
) -> None:
    """None of the packages installed is quiet, an old one installed is an error."""
    entries = {
        "either": _FakeEntryPoint("either", _ConflictingInstrumentor(conflict))
    }
    monkeypatch.setattr(ai, "_instrumentor_entry_points", lambda: entries)
    caplog.set_level("DEBUG")

    out = instrument_libraries(object(), directive=True, exclude=set())

    assert out == []
    levels = [
        record.levelno
        for record in records_of(caplog, "grelmicro")
        if "either" in record.getMessage()
    ]
    assert levels == [level]


def test_instrument_libraries_skips_an_entry_point_whose_module_cannot_load(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An instrumentor that needs a missing package to load is skipped quietly."""
    working = _FakeInstrumentor()
    entries = {
        "broken": _BrokenEntryPoint(),
        "working": _FakeEntryPoint("working", working),
    }
    monkeypatch.setattr(ai, "_instrumentor_entry_points", lambda: entries)
    caplog.set_level("DEBUG")

    out = instrument_libraries(object(), directive=True, exclude=set())

    assert out == [working]
    assert _warnings_and_errors(caplog) == []


def test_instrument_libraries_runs_without_the_instrumentation_package(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With no OpenTelemetry instrumentation package, the sweep still runs."""
    monkeypatch.setitem(
        sys.modules, "opentelemetry.instrumentation.dependencies", None
    )
    monkeypatch.setitem(
        sys.modules, "opentelemetry.instrumentation.instrumentor", None
    )
    working = _FakeInstrumentor()
    entries = {"working": _FakeEntryPoint("working", working)}
    monkeypatch.setattr(ai, "_instrumentor_entry_points", lambda: entries)
    provider = object()

    out = instrument_libraries(provider, directive=True, exclude=set())

    assert out == [working]
    assert working.tracer_provider is provider
    assert _warnings_and_errors(caplog) == []


def test_uninstrument_libraries_swallows_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A library whose uninstrument raises is logged, not propagated."""

    class _Boom:
        def uninstrument(self) -> None:
            msg = "boom"
            raise RuntimeError(msg)

    caplog.set_level("WARNING")
    uninstrument_libraries([_Boom()])
    assert "Failed to un-instrument library" in caplog.text


async def test_app_sweeps_libraries_without_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Trace app with no providers sweeps every used library."""
    instances = _fake_entries(monkeypatch, ["asyncpg", "fastapi", "starlette"])
    micro = Grelmicro(uses=[_none_trace()])
    async with micro:
        # No app is installed, so a framework's instrumentor is swept too.
        assert instances["asyncpg"].tracer_provider is micro.trace.provider
        assert instances["fastapi"].tracer_provider is micro.trace.provider
        assert instances["starlette"].tracer_provider is micro.trace.provider
    # Reversed on exit.
    assert instances["asyncpg"].uninstrumented is True


def _litestar() -> Litestar:
    return Litestar(route_handlers=[])


@pytest.mark.parametrize(
    ("app", "framework"),
    [(FastAPI, "fastapi"), (Starlette, "starlette"), (_litestar, "litestar")],
)
async def test_app_sweep_skips_the_framework_of_an_installed_app(
    monkeypatch: pytest.MonkeyPatch,
    app: Callable[[], object],
    framework: str,
) -> None:
    """The framework of an app grelmicro records requests for is not swept."""
    instances = _fake_entries(monkeypatch, ["asyncpg", framework])
    micro = Grelmicro(uses=[_none_trace()])
    micro.install(app())
    async with micro:
        assert instances["asyncpg"].tracer_provider is micro.trace.provider
        assert instances[framework].tracer_provider is None


# --- per-provider hooks ------------------------------------------------------


def test_redis_provider_instrument_and_uninstrument() -> None:
    """Redis attaches a per-instance instrumentor and detaches it."""
    redis = RedisProvider(REDIS_URL)
    assert redis.instrument(TracerProvider()) is True
    assert redis._instrumentor is not None
    redis.uninstrument()
    assert redis._instrumentor is None


def test_redis_uninstrument_without_instrument_is_noop() -> None:
    """Un-instrumenting a never-instrumented Redis provider is a no-op."""
    RedisProvider(REDIS_URL).uninstrument()


def test_valkey_provider_instrument_and_uninstrument() -> None:
    """Valkey attaches grelmicro's first-party instrumentor and detaches it."""
    import valkey  # noqa: PLC0415

    valkey_provider = ValkeyProvider(REDIS_URL)
    assert valkey_provider.instrument(TracerProvider()) is True
    assert hasattr(valkey.Valkey.execute_command, "__wrapped__")
    valkey_provider.uninstrument()
    assert not hasattr(valkey.Valkey.execute_command, "__wrapped__")


def test_valkey_uninstrument_without_instrument_is_noop() -> None:
    """Un-instrumenting a never-instrumented Valkey provider is a no-op."""
    ValkeyProvider(REDIS_URL).uninstrument()


def test_memory_provider_instrument_is_noop_success() -> None:
    """The base reports success (nothing to trace is not a failure)."""
    memory = MemoryProvider()
    assert memory.instrument(TracerProvider()) is True
    memory.uninstrument()


@pytest.fixture
def _clean_asyncpg() -> Iterator[None]:
    """Guarantee the global asyncpg patch is reverted after the test."""
    PostgresProvider._asyncpg_instrumented = False
    yield
    if PostgresProvider._asyncpg_instrumented:
        PostgresProvider(PG_URL).uninstrument()


@pytest.mark.usefixtures("_clean_asyncpg")
def test_postgres_provider_global_patch_guarded() -> None:
    """Asyncpg patches once process-wide; a second provider does not re-patch."""
    first = PostgresProvider(PG_URL)
    assert first.instrument(TracerProvider()) is True
    assert PostgresProvider._asyncpg_instrumented is True

    assert PostgresProvider(PG_URL).instrument(TracerProvider()) is True
    assert PostgresProvider._asyncpg_instrumented is True

    first.uninstrument()
    assert PostgresProvider._asyncpg_instrumented is False
    first.uninstrument()  # already reverted -> no-op


# --- app-level provider pass -------------------------------------------------


async def test_app_instruments_providers_by_default() -> None:
    """A provider under a `Trace` is instrumented on enter, reversed on exit."""
    redis = RedisProvider(REDIS_URL)
    async with Grelmicro(uses=[_none_trace(), redis]):
        assert redis._instrumentor is not None
    assert redis._instrumentor is None


async def test_app_instrument_false_skips() -> None:
    """`instrument=False` instruments nothing."""
    redis = RedisProvider(REDIS_URL)
    async with Grelmicro(uses=[_none_trace(instrument=False), redis]):
        assert getattr(redis, "_instrumentor", None) is None


async def test_app_allow_list_excludes_unlisted_provider() -> None:
    """An allow-list naming only a framework leaves providers untouched."""
    redis = RedisProvider(REDIS_URL)
    async with Grelmicro(uses=[_none_trace(instrument=["fastapi"]), redis]):
        assert getattr(redis, "_instrumentor", None) is None


async def test_app_unknown_target_raises() -> None:
    """An unknown instrument target fails app startup."""
    redis = RedisProvider(REDIS_URL)
    micro = Grelmicro(uses=[_none_trace(instrument=["bogus"]), redis])
    with pytest.raises(SettingsValidationError, match="unknown targets"):
        async with micro:
            pass


async def test_app_without_trace_does_not_instrument() -> None:
    """No `Trace` component means no provider instrumentation."""
    redis = RedisProvider(REDIS_URL)
    async with Grelmicro(uses=[redis]):
        assert getattr(redis, "_instrumentor", None) is None


async def test_app_with_trace_no_providers() -> None:
    """A `Trace` with no providers enters cleanly."""
    async with Grelmicro(uses=[_none_trace()]):
        pass


class _FailingEntryPoint:
    """An entry point whose module fails to load for another reason."""

    name = "failing"

    def load(self) -> object:
        msg = "boom"
        raise RuntimeError(msg)


def test_instrument_libraries_warns_on_an_entry_point_that_fails_to_load(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A module that fails to load for a reason other than a missing package warns."""
    entries = {"failing": _FailingEntryPoint()}
    monkeypatch.setattr(ai, "_instrumentor_entry_points", lambda: entries)
    caplog.set_level("WARNING")

    out = instrument_libraries(object(), directive=True, exclude=set())

    assert out == []
    assert "Failed to auto-instrument library 'failing'" in caplog.text
