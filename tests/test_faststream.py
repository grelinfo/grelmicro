"""Tests for the Grelmicro FastStream integration."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Self

import pytest

faststream = pytest.importorskip("faststream")
faststream_redis = pytest.importorskip("faststream.redis")

from faststream import FastStream  # noqa: E402
from faststream.redis import RedisBroker, TestRedisBroker  # noqa: E402
from faststream.redis.opentelemetry import (  # noqa: E402
    RedisTelemetryMiddleware,
)

from grelmicro import AmbientBindingError, Grelmicro  # noqa: E402
from grelmicro.integrations import faststream as integration  # noqa: E402
from grelmicro.resilience import RateLimiter, RateLimiterRegistry  # noqa: E402
from grelmicro.resilience.ratelimiter.memory import (  # noqa: E402
    MemoryRateLimiterAdapter,
)
from grelmicro.trace import Trace, TraceExporterType  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from grelmicro.trace._autoinstrument import InstrumentDirective

pytestmark = [pytest.mark.timeout(5)]


class _RecordingComponent:
    kind = "rec"
    name = "default"

    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> Self:
        self.entered += 1
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.exited += 1


@asynccontextmanager
async def _running(app: FastStream) -> AsyncIterator[None]:
    """Run the FastStream startup and shutdown hooks around the block."""
    await app.start()
    try:
        yield
    finally:
        await app.stop()


async def test_install_wires_lifecycle_and_ambient_binding() -> None:
    """`micro.install(app)` opens micro and binds it inside a subscriber."""
    micro = Grelmicro(uses=[RateLimiterRegistry(MemoryRateLimiterAdapter())])
    broker = RedisBroker()
    app = FastStream(broker)
    results: list[bool] = []

    @broker.subscriber("limited")
    async def handler(msg: str) -> bool:  # noqa: ARG001
        limiter = RateLimiter.sliding_window("api", limit=10, window=1.0)
        result = await limiter.acquire(key="client")
        results.append(result.allowed)
        return result.allowed

    micro.install(app)

    async with TestRedisBroker(broker), _running(app):
        response = await broker.request("ping", "limited")

    assert results == [True]
    assert response.body == b"true"


def test_install_registers_one_broker_middleware() -> None:
    """`install` adds the binding middleware by default, none with `ambient=False`."""
    on_broker = RedisBroker()
    Grelmicro().install(FastStream(on_broker))
    off_broker = RedisBroker()
    Grelmicro().install(FastStream(off_broker), ambient=False)

    assert len(tuple(on_broker.middlewares)) == 1
    assert len(tuple(off_broker.middlewares)) == 0


def _has_telemetry_middleware(broker: RedisBroker) -> bool:
    """Whether the broker carries the FastStream Redis telemetry middleware.

    The telemetry middleware is added as a configured instance (it holds the
    tracer), so match an instance or, defensively, a subclass type.
    """
    return any(
        isinstance(mw, RedisTelemetryMiddleware)
        or (isinstance(mw, type) and issubclass(mw, RedisTelemetryMiddleware))
        for mw in broker.middlewares
    )


def test_install_wires_faststream_telemetry_with_trace() -> None:
    """A registered `Trace` wires the broker's telemetry middleware by default."""
    broker = RedisBroker()
    micro = Grelmicro(uses=[Trace(exporter=TraceExporterType.NONE)])
    micro.install(FastStream(broker))
    assert _has_telemetry_middleware(broker)


def test_install_no_faststream_telemetry_without_trace() -> None:
    """Without a `Trace` component, no telemetry middleware is added."""
    broker = RedisBroker()
    Grelmicro().install(FastStream(broker))
    assert not _has_telemetry_middleware(broker)


@pytest.mark.parametrize("directive", [False, {"faststream": False}])
def test_install_faststream_telemetry_respects_directive(
    directive: InstrumentDirective,
) -> None:
    """`instrument=False` or a deny map skips the telemetry middleware."""
    broker = RedisBroker()
    micro = Grelmicro(
        uses=[Trace(exporter=TraceExporterType.NONE, instrument=directive)]
    )
    micro.install(FastStream(broker))
    assert not _has_telemetry_middleware(broker)


def test_install_faststream_telemetry_missing_extra_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A named faststream target whose telemetry module is absent warns, not crashes."""

    def _missing(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr(integration.importlib, "import_module", _missing)
    broker = RedisBroker()
    micro = Grelmicro(
        uses=[Trace(exporter=TraceExporterType.NONE, instrument=["faststream"])]
    )
    caplog.set_level("WARNING")
    micro.install(FastStream(broker))
    assert not _has_telemetry_middleware(broker)
    assert "faststream" in caplog.text


def test_install_faststream_telemetry_missing_extra_silent_by_default(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing telemetry module is a silent no-op under the default directive."""

    def _missing(name: str) -> object:
        raise ImportError(name)

    monkeypatch.setattr(integration.importlib, "import_module", _missing)
    broker = RedisBroker()
    micro = Grelmicro(uses=[Trace(exporter=TraceExporterType.NONE)])
    caplog.set_level("WARNING")
    micro.install(FastStream(broker))
    assert not _has_telemetry_middleware(broker)
    assert caplog.text == ""


async def test_install_ambient_false_still_opens_lifecycle() -> None:
    """`ambient=False` still opens micro so components are registered."""
    micro = Grelmicro(uses=[RateLimiterRegistry(MemoryRateLimiterAdapter())])
    broker = RedisBroker()
    app = FastStream(broker)
    opened: list[bool] = []

    @broker.subscriber("limited")
    async def handler(msg: str) -> bool:  # noqa: ARG001
        opened.append(bool(micro.components))
        return True

    with pytest.warns(UserWarning, match="ambient=False"):
        micro.install(app, ambient=False)

    async with TestRedisBroker(broker), _running(app):
        await broker.request("ping", "limited")

    assert opened == [True]


async def test_install_closes_micro_when_later_startup_hook_fails() -> None:
    """A later startup failure rolls back an opened micro app."""
    component = _RecordingComponent()
    broker = RedisBroker()
    app = FastStream(broker)
    micro = Grelmicro(uses=[component])
    micro.install(app)

    @app.on_startup
    async def fail_after_micro_open() -> None:
        msg = "later startup failed"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="later startup failed"):
        await app.start()

    assert component.entered == 1
    assert component.exited == 1


async def test_install_leaves_micro_closed_when_startup_fails_before_open() -> (
    None
):
    """A startup failure before micro opens leaves nothing to roll back."""
    component = _RecordingComponent()
    broker = RedisBroker()
    app = FastStream(broker)
    micro = Grelmicro(uses=[component])

    @app.on_startup
    async def fail_before_micro_open() -> None:
        msg = "early startup failed"
        raise RuntimeError(msg)

    micro.install(app)

    with pytest.raises(RuntimeError, match="early startup failed"):
        await app.start()

    assert component.entered == 0
    assert component.exited == 0


def test_install_ambient_false_strict_raises() -> None:
    """`strict=True` turns the ambient-binding warning into an error."""
    micro = Grelmicro(
        strict=True, uses=[RateLimiterRegistry(MemoryRateLimiterAdapter())]
    )
    with pytest.raises(AmbientBindingError, match="ratelimiter:default"):
        micro.install(FastStream(RedisBroker()), ambient=False)


def test_check_ambient_binding_true_when_installed() -> None:
    """`check_ambient_binding` is True once the broker middleware is wired."""
    micro = Grelmicro(uses=[RateLimiterRegistry(MemoryRateLimiterAdapter())])
    app = FastStream(RedisBroker())
    micro.install(app)
    assert micro.check_ambient_binding(app) is True


def test_check_ambient_binding_false_without_middleware() -> None:
    """`check_ambient_binding` is False when the broker middleware is absent."""
    micro = Grelmicro(uses=[RateLimiterRegistry(MemoryRateLimiterAdapter())])
    app = FastStream(RedisBroker())
    assert micro.check_ambient_binding(app) is False
