from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination, Lock, LockBackend
from grelmicro.providers.postgres import PostgresProvider

# app.py
micro = Grelmicro(uses=[PostgresProvider("postgresql://app:secret@db/app")])


async def reserve(sku: str) -> None:
    async with Lock(f"stock:{sku}"):
        ...


# conftest.py
@pytest.fixture
async def running() -> AsyncIterator[Grelmicro]:
    async with micro.fake(), micro:
        yield micro


# test_stock.py
async def test_reserve(running: Grelmicro) -> None:
    await reserve("sku-1")


async def test_reserve_takes_the_lock(running: Grelmicro) -> None:
    backend = AsyncMock(spec=LockBackend)
    async with running.override(Coordination(lock=backend)):
        await reserve("sku-1")
    backend.acquire.assert_awaited()
