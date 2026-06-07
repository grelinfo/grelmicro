"""Tests for `Metrics` configuration resolution and env loading."""

from __future__ import annotations

import pytest

from grelmicro import Grelmicro
from grelmicro.metrics import Metrics, MetricsConfig, MetricsExporterType


def test_config_defaults() -> None:
    """Defaults match the documented values."""
    config = MetricsConfig()
    assert config.exporter == MetricsExporterType.OTLP_HTTP
    assert config.export_interval == 60.0  # noqa: PLR2004
    assert config.export_timeout == 30.0  # noqa: PLR2004
    assert config.shutdown_timeout == 5.0  # noqa: PLR2004
    assert config.headers == {}
    assert config.resource_attributes == {}


def test_config_rejects_non_positive_interval() -> None:
    """`export_interval` must be positive."""
    with pytest.raises(ValueError, match="export_interval"):
        MetricsConfig.model_validate({"export_interval": 0})


async def test_config_env_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    """`GREL_METRICS_*` env vars feed the config when env reads are on."""
    monkeypatch.setenv("GREL_METRICS_EXPORTER", "none")
    monkeypatch.setenv("GREL_METRICS_SERVICE_NAME", "from-env")
    monkeypatch.setenv("GREL_METRICS_EXPORT_INTERVAL", "12.5")

    micro = Grelmicro(uses=[Metrics(env_load=True)])
    async with micro:
        config = micro.metrics.config
        assert config.exporter == MetricsExporterType.NONE
        assert config.service_name == "from-env"
        assert config.export_interval == 12.5  # noqa: PLR2004


async def test_kwargs_win_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit kwargs override environment variables."""
    monkeypatch.setenv("GREL_METRICS_SERVICE_NAME", "from-env")

    micro = Grelmicro(
        uses=[
            Metrics(
                exporter=MetricsExporterType.NONE,
                service_name="explicit",
                env_load=True,
            )
        ]
    )
    async with micro:
        assert micro.metrics.config.service_name == "explicit"


async def test_prebuilt_config_rejects_kwargs() -> None:
    """Passing both a pre-built config and kwargs raises on enter."""
    config = MetricsConfig(exporter=MetricsExporterType.NONE)
    micro = Grelmicro(uses=[Metrics(config=config, service_name="x")])
    with pytest.raises(TypeError, match="pre-built config OR"):
        async with micro:
            pass
