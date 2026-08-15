"""Tests for `Metrics` configuration resolution and env loading."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from grelmicro import Grelmicro
from grelmicro.metrics import Metrics, MetricsConfig, MetricsExporterType


def test_config_defaults() -> None:
    """Defaults match the documented values."""
    config = MetricsConfig()
    assert config.exporter == MetricsExporterType.AUTO
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


def test_endpoint_repr_redacts_embedded_credentials() -> None:
    """An endpoint carrying userinfo credentials is displayed redacted."""
    config = MetricsConfig(endpoint="https://usr:s3cret@otlp.example.com/v1")

    assert "s3cret" not in repr(config)
    assert "s3cret" not in config.model_dump_json()
    assert "s3cret" not in repr(config.model_dump())


def test_endpoint_without_credentials_stays_readable() -> None:
    """An ordinary endpoint is displayed in full."""
    config = MetricsConfig(endpoint="http://otel-collector:4318")

    assert "otel-collector:4318" in repr(config)


def test_endpoint_accepts_scheme_less_host_port() -> None:
    """The OTLP gRPC `host:port` form is still accepted."""
    config = MetricsConfig(endpoint="otel-collector:4317")

    assert config.endpoint is not None
    assert config.endpoint.get_secret_value() == "otel-collector:4317"


def test_headers_never_expose_their_values() -> None:
    """Header values carry API keys, so they are masked everywhere."""
    config = MetricsConfig(headers={"api-key": "s3cret"})

    assert "s3cret" not in repr(config)
    assert "s3cret" not in config.model_dump_json()
    assert "s3cret" not in repr(config.model_dump())
    assert config.headers["api-key"].get_secret_value() == "s3cret"


def test_endpoint_scheme_less_userinfo_is_redacted() -> None:
    """A scheme-less endpoint carrying credentials is still redacted."""
    config = MetricsConfig(endpoint="usr:s3cret@collector:4317")

    assert "s3cret" not in repr(config)
    assert "s3cret" not in config.model_dump_json()


def test_endpoint_is_not_normalized_for_display() -> None:
    """An endpoint with nothing to redact is displayed exactly as given."""
    config = MetricsConfig(endpoint="http://otel-collector:4318")

    assert str(config.endpoint) == "http://otel-collector:4318"


def test_invalid_value_does_not_echo_the_credential() -> None:
    """A rejected field reports the failure without the input."""
    with pytest.raises(ValidationError) as excinfo:
        MetricsConfig(
            export_interval=-1, endpoint="https://usr:s3cret@collector"
        )

    assert "s3cret" not in str(excinfo.value)
