"""Tests for the Grelmicro FastStream integration."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest

faststream = pytest.importorskip("faststream")
faststream_redis = pytest.importorskip("faststream.redis")

from faststream import FastStream  # noqa: E402
from faststream.redis import RedisBroker, TestRedisBroker  # noqa: E402

from grelmicro import Grelmicro  # noqa: E402
from grelmicro.resilience import RateLimiter, RateLimiterRegistry  # noqa: E402
from grelmicro.resilience.ratelimiter.memory import (  # noqa: E402
    MemoryRateLimiterAdapter,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = [pytest.mark.timeout(5)]


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

    micro.install(app, ambient=False)

    async with TestRedisBroker(broker), _running(app):
        await broker.request("ping", "limited")

    assert opened == [True]
