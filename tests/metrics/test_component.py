"""Tests for the `Metrics` component (Grelmicro app integration)."""

from __future__ import annotations

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider

from grelmicro import Component, ComponentAlreadyRegisteredError, Grelmicro
from grelmicro.errors import SettingsValidationError
from grelmicro.health import HealthChecks
from grelmicro.metrics import (
    Metrics,
    MetricsConfig,
    MetricsExporterType,
)
from grelmicro.metrics import _hub as hub
from grelmicro.metrics.errors import (
    MetricsError,
    MetricsSettingsValidationError,
)


def test_metrics_config_accepts_case_insensitive_enums() -> None:
    """Enum fields accept any-case strings via `_missing_`."""
    config = MetricsConfig.model_validate({"exporter": "PROMETHEUS"})
    assert config.exporter == MetricsExporterType.PROMETHEUS


def test_metrics_config_rejects_unknown_enum_value() -> None:
    """Enum fields reject values that no member matches."""
    with pytest.raises(ValueError, match="exporter"):
        MetricsConfig.model_validate({"exporter": "bogus"})


def test_metrics_config_rejects_none_for_enum() -> None:
    """`None` does not silently resolve to `MetricsExporterType.NONE`."""
    with pytest.raises(ValueError, match="exporter"):
        MetricsConfig.model_validate({"exporter": None})


def test_metrics_config_rejects_extra_field() -> None:
    """The config forbids unknown fields."""
    with pytest.raises(ValueError, match="bogus"):
        MetricsConfig.model_validate({"bogus": 1})


def test_metrics_satisfies_component_protocol() -> None:
    """`Metrics` is a runtime-checkable `Component`."""
    assert isinstance(Metrics(exporter=MetricsExporterType.NONE), Component)


def test_metrics_default_kind_and_name() -> None:
    """Default kind is `metrics` and default name is `default`."""
    metrics = Metrics(exporter=MetricsExporterType.NONE)
    assert metrics.kind == "metrics"
    assert metrics.name == "default"


def test_metrics_is_singleton() -> None:
    """`Metrics` installs the global meter provider, so a second one is refused."""
    with pytest.raises(ComponentAlreadyRegisteredError, match="singleton"):
        Grelmicro(
            uses=[
                Metrics(exporter=MetricsExporterType.NONE),
                Metrics(exporter=MetricsExporterType.NONE, name="audit"),
            ]
        )


def test_metrics_name_is_read_only() -> None:
    """`Metrics.name` is a read-only property."""
    metrics = Metrics(exporter=MetricsExporterType.NONE)
    with pytest.raises(AttributeError):
        metrics.name = "other"  # ty: ignore[invalid-assignment]


def test_metrics_config_unavailable_before_enter() -> None:
    """`Metrics.config` raises before the component has been entered."""
    metrics = Metrics(exporter=MetricsExporterType.NONE)
    with pytest.raises(RuntimeError, match="only available inside"):
        _ = metrics.config


def test_metrics_provider_unavailable_before_enter() -> None:
    """`Metrics.provider` raises before the component has been entered."""
    metrics = Metrics(exporter=MetricsExporterType.NONE)
    with pytest.raises(RuntimeError, match="only available inside"):
        _ = metrics.provider


def test_metrics_meter_unavailable_before_enter() -> None:
    """`Metrics.meter` raises before the component has been entered."""
    metrics = Metrics(exporter=MetricsExporterType.NONE)
    with pytest.raises(RuntimeError, match="only available inside"):
        metrics.meter("x")


async def test_metrics_installs_provider_on_enter() -> None:
    """Entering the app installs a MeterProvider as the global provider."""
    prior = otel_metrics.get_meter_provider()
    micro = Grelmicro(
        uses=[
            Metrics(
                exporter=MetricsExporterType.NONE,
                service_name="test-svc",
            )
        ]
    )
    async with micro:
        installed = otel_metrics.get_meter_provider()
        assert isinstance(installed, MeterProvider)
        assert micro.metrics.provider is installed
        assert hub.active() is micro.metrics
    assert otel_metrics.get_meter_provider() is prior
    assert hub.active() is None


async def test_metrics_accepts_prebuilt_config() -> None:
    """`Metrics(config=...)` uses the pre-built `MetricsConfig` as-is."""
    config = MetricsConfig(
        exporter=MetricsExporterType.NONE, service_name="payments"
    )
    micro = Grelmicro(uses=[Metrics(config=config)])
    async with micro:
        assert micro.metrics.config is config


def test_metrics_from_config_matches_config_kwarg() -> None:
    """`Metrics.from_config(cfg)` matches `Metrics(config=cfg)`."""
    config = MetricsConfig(
        exporter=MetricsExporterType.NONE, service_name="payments"
    )
    metrics = Metrics.from_config(config)
    assert metrics._explicit_config is config
    assert metrics.name == "default"


def test_metrics_from_config_keeps_name() -> None:
    """`Metrics.from_config(..., name=...)` keeps the registration name."""
    config = MetricsConfig(exporter=MetricsExporterType.NONE)
    metrics = Metrics.from_config(config, name="audit")
    assert metrics.name == "audit"


async def test_metrics_console_exporter() -> None:
    """`exporter=console` builds a periodic console reader pipeline."""
    micro = Grelmicro(uses=[Metrics(exporter=MetricsExporterType.CONSOLE)])
    async with micro:
        assert isinstance(micro.metrics.provider, MeterProvider)
        assert micro.metrics.prometheus_registry is None


async def test_metrics_none_exporter_has_no_reader() -> None:
    """`exporter=none` installs a provider with no readers."""
    micro = Grelmicro(uses=[Metrics(exporter=MetricsExporterType.NONE)])
    async with micro:
        assert isinstance(micro.metrics.provider, MeterProvider)
        assert micro.metrics.prometheus_registry is None


async def test_metrics_prometheus_registry_present() -> None:
    """`exporter=prometheus` keeps a CollectorRegistry on the component."""
    from prometheus_client import CollectorRegistry  # noqa: PLC0415

    micro = Grelmicro(uses=[Metrics(exporter=MetricsExporterType.PROMETHEUS)])
    async with micro:
        registry = micro.metrics.prometheus_registry
        assert isinstance(registry, CollectorRegistry)
    assert micro.metrics.prometheus_registry is None


async def test_metrics_meter_cached_per_name() -> None:
    """`meter(name)` returns the same instance for the same scope name."""
    micro = Grelmicro(uses=[Metrics(exporter=MetricsExporterType.NONE)])
    async with micro:
        first = micro.metrics.meter("scope")
        second = micro.metrics.meter("scope")
        assert first is second


async def test_metrics_convenience_instruments() -> None:
    """Convenience accessors build OTel instruments."""
    from opentelemetry.metrics import (  # noqa: PLC0415
        Counter,
        Histogram,
        UpDownCounter,
    )

    micro = Grelmicro(uses=[Metrics(exporter=MetricsExporterType.NONE)])
    async with micro:
        counter = micro.metrics.counter("c", unit="1", description="d")
        hist = micro.metrics.histogram("h", unit="s")
        udc = micro.metrics.up_down_counter("u")
        assert isinstance(counter, Counter)
        assert isinstance(hist, Histogram)
        assert isinstance(udc, UpDownCounter)


async def test_metrics_shutdown_timeout_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A slow `MeterProvider.shutdown()` is bounded by `shutdown_timeout`."""
    import time  # noqa: PLC0415

    caplog.set_level("WARNING", logger="grelmicro.metrics._component")

    micro = Grelmicro(
        uses=[
            Metrics(
                exporter=MetricsExporterType.NONE,
                shutdown_timeout=0.05,
            )
        ]
    )
    async with micro:
        provider = micro.metrics.provider

        def _slow(*_a: object, **_k: object) -> None:
            time.sleep(0.3)

        provider.shutdown = _slow  # type: ignore[method-assign]

    assert any(
        "MeterProvider.shutdown timed out" in record.message
        for record in caplog.records
    )


async def test_metrics_shutdown_exception_logged_not_propagated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raise from `MeterProvider.shutdown` is captured and logged."""
    caplog.set_level("WARNING", logger="grelmicro.metrics._component")

    def _raise(*_a: object, **_k: object) -> None:
        msg = "exporter broken"
        raise RuntimeError(msg)

    micro = Grelmicro(uses=[Metrics(exporter=MetricsExporterType.NONE)])
    async with micro:
        micro.metrics.provider.shutdown = _raise  # type: ignore[method-assign]

    assert any(
        "MeterProvider.shutdown raised an exception" in record.message
        and record.exc_info is not None
        and isinstance(record.exc_info[1], RuntimeError)
        for record in caplog.records
    )


async def test_metrics_raises_when_private_otel_global_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A future OTel that drops `_METER_PROVIDER` surfaces a clear error."""
    import opentelemetry.metrics._internal as otel_internal  # noqa: PLC0415

    monkeypatch.delattr(otel_internal, "_METER_PROVIDER", raising=False)
    micro = Grelmicro(uses=[Metrics(exporter=MetricsExporterType.NONE)])
    with pytest.raises(MetricsError, match="_METER_PROVIDER"):
        async with micro:
            pass


def test_metrics_singleton_skips_other_kinds() -> None:
    """The singleton check passes over components of a different kind."""
    micro = Grelmicro(
        uses=[
            HealthChecks(),
            Metrics(exporter=MetricsExporterType.NONE),
        ]
    )
    assert micro is not None


async def test_metrics_invalid_env_config_raises_settings_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid env config raises a catchable `MetricsSettingsValidationError`."""
    monkeypatch.setenv("GREL_METRICS_EXPORTER", "bogus")
    micro = Grelmicro(uses=[Metrics(env_load=True)])
    with pytest.raises(MetricsSettingsValidationError) as exc_info:
        async with micro:
            pass
    assert isinstance(exc_info.value, SettingsValidationError)


def test_metrics_settings_error_is_settings_validation_error() -> None:
    """`MetricsSettingsValidationError` is a `SettingsValidationError`."""
    assert issubclass(MetricsSettingsValidationError, SettingsValidationError)
