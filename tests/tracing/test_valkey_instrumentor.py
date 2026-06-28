"""Tests for the first-party `ValkeyInstrumentor`."""

from collections.abc import Iterator

import pytest

pytest.importorskip("valkey")
pytest.importorskip("opentelemetry.instrumentation.redis")

import valkey
import valkey.asyncio
import valkey.asyncio.client
import valkey.asyncio.cluster
import valkey.client
import valkey.cluster
from opentelemetry.sdk.trace import TracerProvider

from grelmicro.trace._valkey import ValkeyInstrumentor

_TARGETS = [
    (valkey.Valkey, "execute_command"),
    (valkey.client.Pipeline, "execute"),
    (valkey.client.Pipeline, "immediate_execute_command"),
    (valkey.cluster.ValkeyCluster, "execute_command"),
    (valkey.cluster.ClusterPipeline, "execute"),
    (valkey.asyncio.Valkey, "execute_command"),
    (valkey.asyncio.client.Pipeline, "execute"),
    (valkey.asyncio.client.Pipeline, "immediate_execute_command"),
    (valkey.asyncio.cluster.ValkeyCluster, "execute_command"),
    (valkey.asyncio.cluster.ClusterPipeline, "execute"),
]


def _is_wrapped(klass: type, method: str) -> bool:
    return hasattr(getattr(klass, method), "__wrapped__")


@pytest.fixture
def _uninstrumented() -> Iterator[None]:
    """Ensure the singleton instrumentor is detached before and after a test."""
    ValkeyInstrumentor().uninstrument()
    yield
    ValkeyInstrumentor().uninstrument()


def test_instrumentation_dependencies() -> None:
    """The instrumentor self-skips unless a supported valkey-py is installed."""
    assert ValkeyInstrumentor().instrumentation_dependencies() == (
        "valkey >= 6.0.0",
    )


@pytest.mark.usefixtures("_uninstrumented")
def test_instrument_wraps_every_valkey_client_class() -> None:
    """Instrumenting wraps the sync, async, pipeline, and cluster classes."""
    ValkeyInstrumentor().instrument(tracer_provider=TracerProvider())
    assert all(_is_wrapped(klass, method) for klass, method in _TARGETS)


@pytest.mark.usefixtures("_uninstrumented")
def test_uninstrument_unwraps_every_valkey_client_class() -> None:
    """Uninstrumenting reverses every wrap."""
    instrumentor = ValkeyInstrumentor()
    instrumentor.instrument(tracer_provider=TracerProvider())
    instrumentor.uninstrument()
    assert not any(_is_wrapped(klass, method) for klass, method in _TARGETS)


@pytest.mark.usefixtures("_uninstrumented")
def test_instrument_then_uninstrument_is_clean_round_trip() -> None:
    """A second uninstrument after a clean detach stays a no-op."""
    instrumentor = ValkeyInstrumentor()
    instrumentor.instrument(tracer_provider=TracerProvider())
    instrumentor.uninstrument()
    instrumentor.uninstrument()
    assert not _is_wrapped(valkey.Valkey, "execute_command")
