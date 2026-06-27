"""Tests for the `Trace` component (Grelmicro app integration)."""

from __future__ import annotations

from base64 import b64encode

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider

from grelmicro import Component, ComponentAlreadyRegisteredError, Grelmicro
from grelmicro.errors import SettingsValidationError
from grelmicro.trace import (
    Trace,
    TraceConfig,
    TraceExporterType,
    TraceSamplerType,
)
from grelmicro.trace._component import _exporter_kwargs, _resolve_exporter_type
from grelmicro.trace.errors import (
    TraceError,
    TraceSettingsValidationError,
)


def test_tracing_config_accepts_case_insensitive_enums() -> None:
    """Enum fields accept any-case strings via `_missing_`."""
    config = TraceConfig.model_validate(
        {"exporter": "NONE", "sampler": "ALWAYS_OFF"}
    )
    assert config.exporter == TraceExporterType.NONE
    assert config.sampler == TraceSamplerType.ALWAYS_OFF


def test_tracing_config_rejects_unknown_enum_value() -> None:
    """Enum fields reject values that no member matches."""
    with pytest.raises(ValueError, match="exporter"):
        TraceConfig.model_validate({"exporter": "bogus"})


def test_tracing_config_rejects_none_for_enum() -> None:
    """`None` does not silently resolve to `TraceExporterType.NONE`."""
    with pytest.raises(ValueError, match="exporter"):
        TraceConfig.model_validate({"exporter": None})


def test_trace_satisfies_component_protocol() -> None:
    """`Trace` is a runtime-checkable `Component`."""
    assert isinstance(Trace(exporter=TraceExporterType.NONE), Component)


def test_trace_default_kind_and_name() -> None:
    """Default kind is `trace` and default name is `default`."""
    trace = Trace(exporter=TraceExporterType.NONE)
    assert trace.kind == "trace"
    assert trace.name == "default"


def test_trace_is_singleton() -> None:
    """`Trace` installs the global tracer provider, so a second one is refused."""
    with pytest.raises(ComponentAlreadyRegisteredError, match="singleton"):
        Grelmicro(
            uses=[
                Trace(exporter=TraceExporterType.NONE),
                Trace(exporter=TraceExporterType.NONE, name="audit"),
            ]
        )


def test_trace_name_is_read_only() -> None:
    """`Trace.name` is a read-only property."""
    trace = Trace(exporter=TraceExporterType.NONE)
    with pytest.raises(AttributeError):
        trace.name = "other"  # ty: ignore[invalid-assignment]


def test_trace_config_unavailable_before_enter() -> None:
    """`Trace.config` raises before the component has been entered."""
    trace = Trace(exporter=TraceExporterType.NONE)
    with pytest.raises(RuntimeError, match="only available inside"):
        _ = trace.config


async def test_trace_installs_provider_on_enter() -> None:
    """Entering the app installs a TracerProvider as the global provider."""
    prior = otel_trace.get_tracer_provider()
    micro = Grelmicro(
        uses=[
            Trace(
                exporter=TraceExporterType.NONE,
                service_name="test-svc",
            )
        ]
    )
    async with micro:
        installed = otel_trace.get_tracer_provider()
        assert isinstance(installed, TracerProvider)
        assert micro.trace.provider is installed
    assert otel_trace.get_tracer_provider() is prior


async def test_trace_accepts_prebuilt_config() -> None:
    """`Trace(config=...)` uses the pre-built `TraceConfig` as-is."""
    config = TraceConfig(
        exporter=TraceExporterType.NONE, service_name="payments"
    )
    micro = Grelmicro(uses=[Trace(config=config)])
    async with micro:
        assert micro.trace.config is config


def test_trace_from_config_matches_config_kwarg() -> None:
    """`Trace.from_config(cfg)` matches `Trace(config=cfg)`."""
    config = TraceConfig(
        exporter=TraceExporterType.NONE, service_name="payments"
    )
    trace = Trace.from_config(config)
    assert trace._explicit_config is config
    assert trace.name == "default"


def test_trace_from_config_keeps_name() -> None:
    """`Trace.from_config(..., name=...)` keeps the registration name."""
    config = TraceConfig(exporter=TraceExporterType.NONE)
    trace = Trace.from_config(config, name="audit")
    assert trace.name == "audit"


def test_trace_provider_unavailable_before_enter() -> None:
    """`Trace.provider` raises before the component has been entered."""
    trace = Trace(exporter=TraceExporterType.NONE)
    with pytest.raises(RuntimeError, match="only available inside"):
        _ = trace.provider


async def test_trace_console_exporter() -> None:
    """`exporter=console` builds a console exporter pipeline."""
    micro = Grelmicro(uses=[Trace(exporter=TraceExporterType.CONSOLE)])
    async with micro:
        assert isinstance(micro.trace.provider, TracerProvider)


async def test_trace_always_off_sampler() -> None:
    """`sampler=always_off` drops all spans."""
    micro = Grelmicro(
        uses=[
            Trace(
                exporter=TraceExporterType.NONE,
                sampler=TraceSamplerType.ALWAYS_OFF,
            )
        ]
    )
    async with micro:
        assert micro.trace.config.sampler == TraceSamplerType.ALWAYS_OFF


async def test_trace_always_on_sampler() -> None:
    """`sampler=always_on` keeps all spans."""
    micro = Grelmicro(
        uses=[
            Trace(
                exporter=TraceExporterType.NONE,
                sampler=TraceSamplerType.ALWAYS_ON,
            )
        ]
    )
    async with micro:
        assert micro.trace.config.sampler == TraceSamplerType.ALWAYS_ON


async def test_trace_sampler_ratio() -> None:
    """`sampler=traceidratio` builds a TraceIdRatioBased sampler."""
    micro = Grelmicro(
        uses=[
            Trace(
                exporter=TraceExporterType.NONE,
                sampler=TraceSamplerType.TRACEIDRATIO,
                sample_ratio=0.25,
            )
        ]
    )
    async with micro:
        assert micro.trace.config.sample_ratio == 0.25  # noqa: PLR2004


async def test_trace_shutdown_timeout_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A slow `TracerProvider.shutdown()` is bounded by `shutdown_timeout`.

    Real path: the daemon thread keeps running past the timeout but does
    not block the asyncio loop's executor teardown (verified by this
    test exiting cleanly without the timeout-on-teardown error).
    """
    import time  # noqa: PLC0415

    caplog.set_level("WARNING", logger="grelmicro.trace._component")

    micro = Grelmicro(
        uses=[
            Trace(
                exporter=TraceExporterType.NONE,
                service_name="slow-svc",
                shutdown_timeout=0.05,
            )
        ]
    )
    async with micro:
        provider = micro.trace.provider
        # Replace shutdown with a sleep that outlives the configured
        # timeout. The daemon-thread wrapper means this is safe.
        provider.shutdown = lambda: time.sleep(0.3)  # type: ignore[method-assign]

    assert any(
        "TracerProvider.shutdown timed out" in record.message
        for record in caplog.records
    )


async def test_trace_shutdown_exception_logged_not_propagated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raise from `TracerProvider.shutdown` is captured and logged."""
    caplog.set_level("WARNING", logger="grelmicro.trace._component")

    def _raise() -> None:
        msg = "exporter broken"
        raise RuntimeError(msg)

    micro = Grelmicro(uses=[Trace(exporter=TraceExporterType.NONE)])
    async with micro:
        micro.trace.provider.shutdown = _raise  # type: ignore[method-assign]

    assert any(
        "TracerProvider.shutdown raised an exception" in record.message
        and record.exc_info is not None
        and isinstance(record.exc_info[1], RuntimeError)
        for record in caplog.records
    )


async def test_trace_raises_when_private_otel_global_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A future OTel that drops `_TRACER_PROVIDER` surfaces a clear error."""
    monkeypatch.delattr(otel_trace, "_TRACER_PROVIDER", raising=False)
    micro = Grelmicro(uses=[Trace(exporter=TraceExporterType.NONE)])
    with pytest.raises(TraceError, match="_TRACER_PROVIDER"):
        async with micro:
            pass


async def test_trace_invalid_env_config_raises_settings_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid env config raises a catchable `TraceSettingsValidationError`."""
    monkeypatch.setenv("GREL_TRACE_EXPORTER", "bogus")
    micro = Grelmicro(uses=[Trace(env_load=True)])
    with pytest.raises(TraceSettingsValidationError) as exc_info:
        async with micro:
            pass
    assert isinstance(exc_info.value, SettingsValidationError)


def test_trace_settings_error_is_settings_validation_error() -> None:
    """`TraceSettingsValidationError` is a `SettingsValidationError`."""
    assert issubclass(TraceSettingsValidationError, SettingsValidationError)


def test_exporter_defaults_to_auto() -> None:
    """The exporter field defaults to `auto`."""
    assert TraceConfig().exporter == TraceExporterType.AUTO


def test_auto_exporter_resolves_none_without_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`auto` becomes `none` when no endpoint is configured."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    assert _resolve_exporter_type(TraceConfig()) == TraceExporterType.NONE


def test_auto_exporter_resolves_otlp_http_with_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`auto` becomes `otlp-http` when the endpoint field is set."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    config = TraceConfig(endpoint="https://obs.example.com/v1/traces")
    assert _resolve_exporter_type(config) == TraceExporterType.OTLP_HTTP


@pytest.mark.parametrize(
    "env_var",
    ["OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"],
)
def test_auto_exporter_resolves_otlp_http_with_otel_env(
    env_var: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`auto` honours the standard OTEL endpoint env vars."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.setenv(env_var, "https://obs.example.com")
    assert _resolve_exporter_type(TraceConfig()) == TraceExporterType.OTLP_HTTP


def test_explicit_exporter_unchanged_without_endpoint() -> None:
    """An explicit exporter is never downgraded by auto-resolution."""
    config = TraceConfig(exporter=TraceExporterType.CONSOLE)
    assert _resolve_exporter_type(config) == TraceExporterType.CONSOLE


async def test_auto_exporter_enters_inert_without_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A default `Trace()` enters cleanly and exports nothing."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    micro = Grelmicro(uses=[Trace(service_name="orders")])
    async with micro:
        assert micro.trace.config.exporter == TraceExporterType.AUTO


def test_basic_auth_header_is_base64_encoded() -> None:
    """`authorization_header` encodes `username:password` per RFC 7617."""
    config = TraceConfig(
        basic_auth_username="me@example.com", basic_auth_password="s3cret"
    )
    expected = b64encode(b"me@example.com:s3cret").decode("ascii")
    assert config.authorization_header == f"Basic {expected}"


def test_basic_auth_header_none_when_unset() -> None:
    """`authorization_header` is `None` without credentials."""
    assert TraceConfig().authorization_header is None


@pytest.mark.parametrize(
    "field",
    ["basic_auth_username", "basic_auth_password"],
)
def test_basic_auth_requires_both_fields(field: str) -> None:
    """Username and password must be set together."""
    with pytest.raises(ValueError, match="must be set together"):
        TraceConfig.model_validate({field: "value"})


def test_basic_auth_conflicts_with_authorization_header() -> None:
    """`basic_auth` plus an explicit Authorization header is rejected."""
    with pytest.raises(ValueError, match="Authorization header"):
        TraceConfig(
            basic_auth_username="me",
            basic_auth_password="secret",
            headers={"authorization": "Bearer abc"},
        )


def test_exporter_kwargs_injects_authorization() -> None:
    """`_exporter_kwargs` attaches the Basic header alongside other headers."""
    config = TraceConfig(
        endpoint="https://obs.example.com/v1/traces",
        headers={"X-Org": "default"},
        basic_auth_username="me",
        basic_auth_password="secret",
    )
    kwargs = _exporter_kwargs(config)
    assert kwargs["endpoint"] == "https://obs.example.com/v1/traces"
    assert kwargs["headers"]["X-Org"] == "default"
    assert kwargs["headers"]["Authorization"].startswith("Basic ")


def test_exporter_kwargs_empty_without_endpoint_or_headers() -> None:
    """`_exporter_kwargs` is empty when nothing is configured."""
    assert _exporter_kwargs(TraceConfig()) == {}


def test_trace_basic_auth_kwarg_splits_into_fields() -> None:
    """`Trace(basic_auth=(u, p))` maps onto the two config fields."""
    trace = Trace(exporter=TraceExporterType.NONE, basic_auth=("me", "secret"))
    assert trace._kwargs["basic_auth_username"] == "me"
    assert trace._kwargs["basic_auth_password"] == "secret"


def test_trace_basic_auth_rejects_non_pair() -> None:
    """`Trace(basic_auth=...)` requires a 2-tuple."""
    with pytest.raises(TypeError, match="username, password"):
        Trace(basic_auth=("only",))  # ty: ignore[invalid-argument-type]


async def test_trace_basic_auth_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credentials resolve from `GREL_TRACE_BASIC_AUTH_*`."""
    monkeypatch.setenv("GREL_TRACE_BASIC_AUTH_USERNAME", "me@example.com")
    monkeypatch.setenv("GREL_TRACE_BASIC_AUTH_PASSWORD", "s3cret")
    micro = Grelmicro(
        uses=[Trace(exporter=TraceExporterType.NONE, env_load=True)]
    )
    async with micro:
        assert micro.trace.config.basic_auth_username == "me@example.com"
        assert micro.trace.config.basic_auth_password == "s3cret"
