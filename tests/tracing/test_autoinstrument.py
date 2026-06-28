"""Tests for `Trace(instrument=...)` auto-instrumentation.

Covers the selection logic, the per-provider instrument hooks, the app-level
provider pass, the FastAPI install pass, and an end-to-end request span.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind
from starlette.applications import Starlette
from starlette.status import HTTP_200_OK

from grelmicro import Grelmicro
from grelmicro.providers.memory import MemoryProvider
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.valkey import ValkeyProvider
from grelmicro.trace import Trace, TraceExporterType
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
from grelmicro.trace.errors import TraceSettingsValidationError

if TYPE_CHECKING:
    from collections.abc import Iterator

REDIS_URL = "redis://localhost:6379/0"
PG_URL = "postgresql://localhost:5432/db"


def _none_trace(*, instrument: InstrumentDirective = True) -> Trace:
    """Return a `Trace` with no exporter (no real OTLP connection)."""
    return Trace(exporter=TraceExporterType.NONE, instrument=instrument)


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
    with pytest.raises(TraceSettingsValidationError, match="unknown targets"):
        validate_directive("typo", known={"redis"})
    with pytest.raises(TraceSettingsValidationError, match="unknown targets"):
        validate_directive(["reddis"], known={"redis"})
    with pytest.raises(TraceSettingsValidationError, match="unknown targets"):
        validate_directive({"typo": False}, known={"redis"})


def test_validate_directive_rejects_non_bool_map_values() -> None:
    """A map value that is not a bool raises, so options are not swallowed."""
    with pytest.raises(TraceSettingsValidationError, match="non-bool"):
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
    assert caplog.text == ""


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

    def __init__(self, name: str, instance: _FakeInstrumentor) -> None:
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
    assert {"fastapi", "redis", "asyncpg"} <= names


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
    """A Trace app with no providers still sweeps used libraries, FastAPI aside."""
    instances = _fake_entries(monkeypatch, ["asyncpg", "fastapi"])
    micro = Grelmicro(uses=[_none_trace()])
    async with micro:
        # asyncpg (app-owned, no provider) is instrumented...
        assert instances["asyncpg"].tracer_provider is micro.trace.provider
        # ...but FastAPI is owned by the integration, excluded from the sweep.
        assert instances["fastapi"].tracer_provider is None
    # Reversed on exit.
    assert instances["asyncpg"].uninstrumented is True


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


def test_valkey_provider_instrument_reports_unsupported() -> None:
    """Valkey has no OTel instrumentor, so instrument returns False."""
    valkey = ValkeyProvider(REDIS_URL)
    assert valkey.instrument(TracerProvider()) is False
    assert getattr(valkey, "_instrumentor", None) is None


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
    with pytest.raises(TraceSettingsValidationError, match="unknown targets"):
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


# --- FastAPI install pass ----------------------------------------------------


def test_install_instruments_fastapi_by_default() -> None:
    """`micro.install(app)` instruments a FastAPI app at install time."""
    app = FastAPI()
    Grelmicro(uses=[_none_trace()]).install(app)
    try:
        assert app._is_instrumented_by_opentelemetry is True  # ty: ignore[unresolved-attribute]
    finally:
        FastAPIInstrumentor.uninstrument_app(app)


def test_install_instrument_false_skips_fastapi() -> None:
    """`instrument=False` leaves the FastAPI app un-instrumented."""
    app = FastAPI()
    Grelmicro(uses=[_none_trace(instrument=False)]).install(app)
    assert getattr(app, "_is_instrumented_by_opentelemetry", False) is False


def test_install_skips_plain_starlette() -> None:
    """A non-FastAPI Starlette app is skipped (FastAPI instrumentor only)."""
    app = Starlette()
    Grelmicro(uses=[_none_trace()]).install(app)
    assert getattr(app, "_is_instrumented_by_opentelemetry", False) is False


def test_install_without_trace_skips_fastapi() -> None:
    """No `Trace` component means no FastAPI instrumentation."""
    app = FastAPI()
    Grelmicro(uses=[]).install(app)
    assert getattr(app, "_is_instrumented_by_opentelemetry", False) is False


# --- end-to-end span ---------------------------------------------------------


@pytest.fixture
def _isolate_tracer_provider() -> Iterator[None]:
    """Give the test a clean OTel global so the proxy resolves deterministically.

    `instrument_app` runs at install time with the proxy tracer, which
    resolves the process-global provider at request time. Other tests in the
    suite leave a provider in that global, so reset it to the default proxy
    here and restore it after.
    """
    from opentelemetry import trace as otel_trace  # noqa: PLC0415

    saved = otel_trace._TRACER_PROVIDER
    otel_trace._TRACER_PROVIDER = None
    try:
        yield
    finally:
        otel_trace._TRACER_PROVIDER = saved


@pytest.mark.usefixtures("_isolate_tracer_provider")
def test_fastapi_request_produces_server_span() -> None:
    """A request through an instrumented app emits a SERVER span."""
    exporter = InMemorySpanExporter()
    micro = Grelmicro(uses=[_none_trace()])
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, bool]:
        return {"ok": True}

    micro.install(app)
    try:
        with TestClient(app) as client:
            # The lifespan installed Trace's provider; the app's proxy tracer
            # resolves to it, so capture spans from that exact provider.
            micro.trace.provider.add_span_processor(
                SimpleSpanProcessor(exporter)
            )
            assert client.get("/ping").status_code == HTTP_200_OK
        spans = exporter.get_finished_spans()
        assert any(span.kind is SpanKind.SERVER for span in spans)
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
