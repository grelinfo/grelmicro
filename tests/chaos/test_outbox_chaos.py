"""Chaos: the outbox loses no message across a real Postgres outage.

Docs claim: delivery is at least once. A message committed by `publish` is
delivered even if Postgres freezes mid-delivery. We stage a committed batch,
start the relay, freeze Postgres with `docker pause` while it is draining,
unfreeze it, and assert every message is eventually delivered.

The relay runs poll-only (`notify=False`) so the frozen `LISTEN` connection
is not part of the test: recovery rides the polling path, which is the
documented source of truth.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from grelmicro.outbox import Message, Outbox
from grelmicro.providers.postgres import PostgresProvider

from .conftest import paused, wait_until

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.timeout(120),
]

testcontainers = pytest.importorskip("testcontainers.postgres")

from testcontainers.postgres import PostgresContainer  # noqa: E402

MESSAGE_COUNT = 20
DELIVERED_BEFORE_OUTAGE = 3
OUTAGE_SECONDS = 3.0
COMMAND_TIMEOUT = 1.0
MAX_FAILURE_LATENCY = 5.0


async def test_outbox_loses_no_message_across_postgres_outage() -> None:
    """Every committed message is delivered despite a Postgres freeze."""
    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        url = f"postgresql://test:test@localhost:{port}/test"
        provider = PostgresProvider(url)
        async with provider:
            outbox = Outbox(
                provider,
                poll_interval=0.2,
                notify=False,
                retry_base=0.1,
                retry_jitter=0,
                concurrency=2,
            )
            delivered: set[int] = set()

            @outbox.handler("job")
            async def handle(message: Message[object]) -> None:
                # A slow handler keeps the relay draining long enough to
                # freeze Postgres mid-flight.
                await asyncio.sleep(0.15)
                delivered.add(message.payload["n"])

            async def delivered_some() -> bool:
                return len(delivered) >= DELIVERED_BEFORE_OUTAGE

            async def delivered_all() -> bool:
                return len(delivered) >= MESSAGE_COUNT

            # Entering the outbox creates the table and starts the relay.
            async with outbox:
                async with (
                    provider.client.acquire() as conn,
                    conn.transaction(),
                ):
                    for n in range(MESSAGE_COUNT):
                        await outbox.publish(conn, "job", {"n": n})
                # Let the relay deliver a few, then freeze Postgres.
                assert await wait_until(delivered_some, timeout=30)
                with paused(container):  # ty: ignore[invalid-argument-type]
                    await asyncio.sleep(OUTAGE_SECONDS)
                # After recovery every message is eventually delivered.
                assert await wait_until(delivered_all, timeout=60)

            assert delivered == set(range(MESSAGE_COUNT))


async def test_publish_fails_loudly_when_postgres_frozen() -> None:
    """With `command_timeout`, publish against a frozen Postgres fails fast.

    The business transaction then rolls back, so no orphan message is left
    and the caller learns the write failed in bounded time instead of
    hanging until the OS TCP timeout.
    """
    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        url = f"postgresql://test:test@localhost:{port}/test"
        provider = PostgresProvider(url, command_timeout=COMMAND_TIMEOUT)
        async with provider:
            outbox = Outbox(provider, relay=False, notify=False)
            # Entering migrates the table so the publish would otherwise
            # succeed. relay=False and notify=False keep the pause clean.
            async with outbox:
                conn = await provider.client.acquire()
                transaction = conn.transaction()
                await transaction.start()
                try:
                    with paused(container):  # ty: ignore[invalid-argument-type]
                        start = time.monotonic()
                        with pytest.raises(TimeoutError):
                            await outbox.publish(conn, "job", {"n": 1})
                        elapsed = time.monotonic() - start
                    assert elapsed < MAX_FAILURE_LATENCY
                finally:
                    # The connection timed out mid-command, so drop it
                    # rather than reuse it. Terminate is local, no server.
                    conn.terminate()
                    await provider.client.release(conn)
