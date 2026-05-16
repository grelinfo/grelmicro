"""Tests for the `Trace` component (Grelmicro app integration)."""

from __future__ import annotations

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider

from grelmicro import Component, Grelmicro
from grelmicro.trace import (
    Trace,
    TracingConfig,
    TracingExporterType,
    TracingSamplerType,
)


def test_tracing_config_accepts_case_insensitive_enums() -> None:
    """Enum fields accept any-case strings via `_missing_`."""
    config = TracingConfig.model_validate(
        {"exporter": "NONE", "sampler": "ALWAYS_OFF"}
    )
    assert config.exporter == TracingExporterType.NONE
    assert config.sampler == TracingSamplerType.ALWAYS_OFF


def test_tracing_config_rejects_unknown_enum_value() -> None:
    """Enum fields reject values that no member matches."""
    with pytest.raises(ValueError, match="exporter"):
        TracingConfig.model_validate({"exporter": "bogus"})


def test_tracing_config_rejects_none_for_enum() -> None:
    """`None` does not silently resolve to `TracingExporterType.NONE`."""
    with pytest.raises(ValueError, match="exporter"):
        TracingConfig.model_validate({"exporter": None})


def test_trace_satisfies_component_protocol() -> None:
    """`Trace` is a runtime-checkable `Component`."""
    assert isinstance(Trace(exporter=TracingExporterType.NONE), Component)


def test_trace_default_kind_and_name() -> None:
    """Default kind is `trace` and default name is `default`."""
    trace = Trace(exporter=TracingExporterType.NONE)
    assert trace.kind == "trace"
    assert trace.name == "default"


def test_trace_named_registration() -> None:
    """A named `Trace` component coexists with the default one."""
    micro = Grelmicro(
        uses=[
            Trace(exporter=TracingExporterType.NONE),
            Trace(exporter=TracingExporterType.NONE, name="audit"),
        ]
    )
    assert micro.get("trace", "default").name == "default"
    assert micro.get("trace", "audit").name == "audit"


def test_trace_config_unavailable_before_enter() -> None:
    """`Trace.config` raises before the component has been entered."""
    trace = Trace(exporter=TracingExporterType.NONE)
    with pytest.raises(RuntimeError, match="only available inside"):
        _ = trace.config


async def test_trace_installs_provider_on_enter() -> None:
    """Entering the app installs a TracerProvider as the global provider."""
    prior = otel_trace.get_tracer_provider()
    micro = Grelmicro(
        uses=[
            Trace(
                exporter=TracingExporterType.NONE,
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
    """`Trace(config=...)` uses the pre-built `TracingConfig` as-is."""
    config = TracingConfig(
        exporter=TracingExporterType.NONE, service_name="payments"
    )
    micro = Grelmicro(uses=[Trace(config=config)])
    async with micro:
        assert micro.trace.config is config


def test_trace_provider_unavailable_before_enter() -> None:
    """`Trace.provider` raises before the component has been entered."""
    trace = Trace(exporter=TracingExporterType.NONE)
    with pytest.raises(RuntimeError, match="only available inside"):
        _ = trace.provider


async def test_trace_console_exporter() -> None:
    """`exporter=console` builds a console exporter pipeline."""
    micro = Grelmicro(uses=[Trace(exporter=TracingExporterType.CONSOLE)])
    async with micro:
        assert isinstance(micro.trace.provider, TracerProvider)


async def test_trace_always_off_sampler() -> None:
    """`sampler=always_off` drops all spans."""
    micro = Grelmicro(
        uses=[
            Trace(
                exporter=TracingExporterType.NONE,
                sampler=TracingSamplerType.ALWAYS_OFF,
            )
        ]
    )
    async with micro:
        assert micro.trace.config.sampler == TracingSamplerType.ALWAYS_OFF


async def test_trace_always_on_sampler() -> None:
    """`sampler=always_on` keeps all spans."""
    micro = Grelmicro(
        uses=[
            Trace(
                exporter=TracingExporterType.NONE,
                sampler=TracingSamplerType.ALWAYS_ON,
            )
        ]
    )
    async with micro:
        assert micro.trace.config.sampler == TracingSamplerType.ALWAYS_ON


async def test_trace_sampler_ratio() -> None:
    """`sampler=traceidratio` builds a TraceIdRatioBased sampler."""
    micro = Grelmicro(
        uses=[
            Trace(
                exporter=TracingExporterType.NONE,
                sampler=TracingSamplerType.TRACEIDRATIO,
                sample_ratio=0.25,
            )
        ]
    )
    async with micro:
        assert micro.trace.config.sample_ratio == 0.25  # noqa: PLR2004
