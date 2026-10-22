"""The first run after the app opens finds every backend it uses open.

A claimed task fires as soon as `Tasks` opens, and the outbox relay hands
out a waiting message as soon as `Outbox` opens. Both run user code that
resolves its backends through the app, so they must open after everything
else in `uses=`, whatever order it is listed in.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel

from grelmicro import Grelmicro, LifecycleOrderError, Usable
from grelmicro.coordination import Coordination, Lock
from grelmicro.health import HealthChecks
from grelmicro.outbox import Message, Outbox
from grelmicro.outbox.memory import MemoryOutboxAdapter
from grelmicro.providers import Provider
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.task import Tasks

FIRST_RUN_TIMEOUT = 5
"""Seconds to wait for the first run. A failed first run never comes back
within the 60 s interval, so a miss times out instead of racing a retry."""

pytestmark = [pytest.mark.timeout(30)]

type Wiring = Callable[[Provider, Tasks], list[Usable]]


_ran: list[asyncio.Event] = []
"""The event the running test waits on, set by its claimed task."""


async def _claimed_job() -> None:
    """Mark the first claimed run of the running test."""
    _ran[-1].set()


def _claimed_task(tasks: Tasks) -> asyncio.Event:
    """Register a claimed task that sets the returned event when it runs."""
    _ran.append(asyncio.Event())
    # A unique name per test: tests sharing one database must not find the
    # slot an earlier test claimed.
    name = f"first-claimed-run-{uuid4().hex}"
    tasks.every(interval=60, gate="claim", name=name)(_claimed_job)
    return _ran[-1]


async def _assert_first_claimed_run(
    uses: list[Usable], tasks: Tasks, *, strict: bool = False
) -> None:
    """Open the app and assert the first fire wins its claim and runs."""
    ran = _claimed_task(tasks)
    async with Grelmicro(uses=uses, strict=strict):
        await asyncio.wait_for(ran.wait(), FIRST_RUN_TIMEOUT)


_WIRINGS: dict[str, Wiring] = {
    "provider-first": lambda provider, tasks: [provider, tasks],
    "tasks-first": lambda provider, tasks: [tasks, provider],
    "tasks-before-coordination": lambda provider, tasks: [
        tasks,
        Coordination(provider, requires="host"),
    ],
}


@pytest.mark.parametrize("wiring", _WIRINGS.values(), ids=_WIRINGS.keys())
async def test_first_claimed_run_succeeds_on_sqlite(
    tmp_path: Path, wiring: Wiring
) -> None:
    """The first claimed fire runs on SQLite, whatever the `uses=` order."""
    tasks = Tasks()
    sqlite = SQLiteProvider(tmp_path / "app.db")

    await _assert_first_claimed_run(wiring(sqlite, tasks), tasks)


@pytest.fixture(scope="module")
def postgres_url() -> Generator[str]:
    """Start a Postgres container and yield its URL."""
    from testcontainers.community.postgres import (  # noqa: PLC0415
        PostgresContainer,
    )

    with PostgresContainer() as container:
        port = container.get_exposed_port(5432)
        yield f"postgresql://test:test@localhost:{port}/test"


@pytest.mark.integration
@pytest.mark.timeout(30, func_only=True)
@pytest.mark.parametrize(
    "wiring",
    [_WIRINGS["provider-first"], _WIRINGS["tasks-first"]],
    ids=["provider-first", "tasks-first"],
)
async def test_first_claimed_run_succeeds_on_postgres(
    postgres_url: str, wiring: Wiring
) -> None:
    """The first claimed fire runs on Postgres, whatever the `uses=` order."""
    from grelmicro.providers.postgres import PostgresProvider  # noqa: PLC0415

    tasks = Tasks()
    postgres = PostgresProvider(postgres_url)

    await _assert_first_claimed_run(wiring(postgres, tasks), tasks)


class _Ping(BaseModel):
    """A message the outbox test publishes before the app opens."""

    n: int


_handled: list[int] = []
"""The attempt count of each delivery the outbox handler completed."""


async def _on_ping(message: Message[_Ping]) -> None:
    """Take a lock, then record which attempt got through."""
    async with Lock("first-outbox-delivery"):
        _handled.append(message.attempts)


async def test_first_outbox_delivery_finds_its_lock_open(
    tmp_path: Path,
) -> None:
    """A message waiting at startup is handled on its first attempt.

    The handler takes a lock the provider listed after the outbox serves.
    """
    outbox = Outbox(MemoryOutboxAdapter())
    _handled.clear()
    outbox.handler(_Ping)(_on_ping)

    await outbox.publish(None, _Ping(n=1))
    micro = Grelmicro(uses=[outbox, SQLiteProvider(tmp_path / "app.db")])

    async with micro:
        while not _handled:  # noqa: ASYNC110
            await asyncio.sleep(0.01)

    assert _handled == [1]


async def test_runners_open_last_and_close_first(tmp_path: Path) -> None:
    """The outbox and then the tasks open after every other item.

    The app closes in reverse, so they stop before what they use closes.
    """
    tasks = Tasks()
    outbox = Outbox(MemoryOutboxAdapter())
    sqlite = SQLiteProvider(tmp_path / "app.db")
    micro = Grelmicro(uses=[tasks, outbox, sqlite])

    async with micro:
        order = list(micro._items)

    assert order[0] is sqlite
    assert order[-2:] == [outbox, tasks]


async def test_strict_refuses_tasks_listed_before_a_provider(
    tmp_path: Path,
) -> None:
    """`strict=True` raises rather than open the tasks later than written."""
    tasks = Tasks()
    micro = Grelmicro(
        uses=[tasks, SQLiteProvider(tmp_path / "app.db")], strict=True
    )

    with pytest.raises(LifecycleOrderError, match="Tasks is listed before"):
        async with micro:
            pass


async def test_strict_refuses_tasks_listed_before_the_outbox() -> None:
    """A task may publish to the outbox, so it is listed after it."""
    tasks = Tasks()
    micro = Grelmicro(uses=[tasks, Outbox(MemoryOutboxAdapter())], strict=True)

    with pytest.raises(LifecycleOrderError, match="before Outbox"):
        async with micro:
            pass


async def test_strict_accepts_items_no_run_uses_after_tasks(
    tmp_path: Path,
) -> None:
    """`strict=True` lets `HealthChecks` follow `Tasks`.

    The defaults a bare provider fills are not in the list either, so the
    tasks moving past them reorders nothing that was written.
    """
    tasks = Tasks()

    await _assert_first_claimed_run(
        [SQLiteProvider(tmp_path / "app.db"), tasks, HealthChecks()],
        tasks,
        strict=True,
    )
