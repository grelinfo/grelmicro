"""Tests for liveness checks and the loop watchdog."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Self

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.status import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE

from grelmicro import Grelmicro
from grelmicro.health import (
    HealthChecks,
    HealthError,
    Liveness,
    _liveness,
    health_asgi,
)
from grelmicro.health._liveness import Watchdog
from grelmicro.integrations.fastapi import health_router

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.timeout(10)]

_STALLED_EXIT_CODE = 70
"""The exit code a worker uses when its event loop stalls."""


class _Exits:
    """Record the exits the health component asks for, instead of exiting."""

    def __init__(self) -> None:
        self.stopped = threading.Event()
        self.exited = threading.Event()

    def stop(self) -> None:
        self.stopped.set()

    def exit(self) -> None:
        self.exited.set()


@pytest.fixture
def exits(monkeypatch: pytest.MonkeyPatch) -> _Exits:
    """Patch the process exits so a test observes them."""
    recorder = _Exits()
    monkeypatch.setattr(
        "grelmicro.health._liveness._stop_process", recorder.stop
    )
    monkeypatch.setattr(
        "grelmicro.health._liveness._exit_process", recorder.exit
    )
    return recorder


def _progress_check(
    state: dict[str, bool],
) -> Callable[[], Awaitable[None]]:
    async def check() -> None:
        if not state["healthy"]:
            msg = "no progress"
            raise HealthError(msg)

    return check


async def _wait_for(condition: Callable[[], bool], within: float = 2.0) -> None:
    deadline = time.monotonic() + within
    while not condition():
        if time.monotonic() > deadline:
            msg = "condition never became true"
            raise AssertionError(msg)
        await asyncio.sleep(0.01)


async def test_liveness_failures_stop_the_process(exits: _Exits) -> None:
    """A liveness check failing failure_threshold times in a row stops the worker."""
    # Arrange
    health = HealthChecks(
        liveness=Liveness(
            interval=0.01, failure_threshold=3, shutdown_timeout=0.05
        )
    )
    health.add("progress", _progress_check({"healthy": False}), liveness=True)

    # Act
    async with health:
        await _wait_for(exits.stopped.is_set)
        await _wait_for(exits.exited.is_set)

    # Assert
    assert exits.stopped.is_set()


async def test_liveness_recovery_resets_the_count(exits: _Exits) -> None:
    """A passing round resets the failures, so the worker keeps running."""
    # Arrange
    state = {"healthy": False}
    health = HealthChecks(
        liveness=Liveness(interval=0.01, failure_threshold=1000)
    )
    health.add("progress", _progress_check(state), liveness=True)

    # Act
    async with health:
        await _wait_for(lambda: not health.is_alive)
        state["healthy"] = True
        await _wait_for(lambda: health.is_alive)

    # Assert
    assert not exits.stopped.is_set()


async def test_livez_answers_503_while_a_liveness_check_fails(
    exits: _Exits,
) -> None:
    """The ASGI /livez answers 503 while the last liveness round failed."""
    # Arrange
    state = {"healthy": False}
    health = HealthChecks(
        liveness=Liveness(interval=0.01, failure_threshold=1000)
    )
    health.add("progress", _progress_check(state), liveness=True)
    transport = httpx.ASGITransport(app=health_asgi(health))

    # Act
    async with (
        health,
        httpx.AsyncClient(transport=transport, base_url="http://p") as client,
    ):
        await _wait_for(lambda: not health.is_alive)
        failing = await client.get("/livez")
        state["healthy"] = True
        await _wait_for(lambda: health.is_alive)
        passing = await client.get("/livez")

    # Assert
    assert failing.status_code == HTTP_503_SERVICE_UNAVAILABLE
    assert passing.status_code == HTTP_200_OK
    assert not exits.stopped.is_set()


def test_fastapi_livez_answers_503_while_a_liveness_check_fails(
    exits: _Exits,
) -> None:
    """The FastAPI /livez answers 503 while the last liveness round failed."""
    # Arrange
    health = HealthChecks(
        liveness=Liveness(interval=0.01, failure_threshold=1000)
    )
    health.add("progress", _progress_check({"healthy": False}), liveness=True)
    micro = Grelmicro(uses=[health])
    app = FastAPI()
    micro.install(app)
    app.include_router(health_router())

    # Act
    with TestClient(app) as client:
        deadline = time.monotonic() + 2
        while health.is_alive and time.monotonic() < deadline:
            time.sleep(0.01)
        response = client.get("/livez")

    # Assert
    assert response.status_code == HTTP_503_SERVICE_UNAVAILABLE
    assert not exits.stopped.is_set()


async def test_liveness_checks_stay_out_of_readyz_and_healthz(
    exits: _Exits,
) -> None:
    """A liveness check runs for /livez only, never for /readyz or /healthz."""
    # Arrange
    health = HealthChecks(cache_ttl=0)
    health.add("progress", _progress_check({"healthy": False}), liveness=True)

    # Act
    report = await health.run(critical_only=False)

    # Assert
    assert report["checks"] == {}
    assert not exits.stopped.is_set()


async def test_watchdog_exits_when_the_loop_stalls(exits: _Exits) -> None:
    """A loop blocked past stall_timeout makes the watchdog exit the process."""
    # Arrange
    health = HealthChecks(liveness=Liveness(stall_timeout=0.2))

    # Act
    async with health:
        await asyncio.sleep(0.05)  # let the watchdog arm
        time.sleep(0.8)  # noqa: ASYNC251  # blocks the event loop on purpose
        await asyncio.sleep(0)

    # Assert
    assert exits.exited.is_set()


async def test_watchdog_stays_quiet_while_the_loop_runs(exits: _Exits) -> None:
    """A loop that keeps running never trips the watchdog."""
    # Arrange
    health = HealthChecks(liveness=Liveness(stall_timeout=0.2))

    # Act
    async with health:
        await asyncio.sleep(0.6)

    # Assert
    assert not exits.exited.is_set()


async def test_without_liveness_nothing_starts(exits: _Exits) -> None:
    """With no Liveness, no thread starts and /livez stays 200."""
    # Arrange
    health = HealthChecks()
    health.add("progress", _progress_check({"healthy": False}), liveness=True)
    threads_before = threading.active_count()

    # Act
    async with health:
        await asyncio.sleep(0.05)
        threads_during = threading.active_count()

    # Assert
    assert threads_during == threads_before
    assert health.is_alive
    assert not exits.stopped.is_set()


_STALLED_WORKER = """
import asyncio, time
from grelmicro.health import HealthChecks, Liveness

async def main():
    async with HealthChecks(liveness=Liveness(stall_timeout=0.2)):
        await asyncio.sleep(0.1)
        time.sleep(5)

asyncio.run(main())
"""

_FAILING_WORKER = """
import asyncio
from grelmicro.health import HealthChecks, HealthError, Liveness

health = HealthChecks(liveness=Liveness(interval=0.01, failure_threshold=2))

@health.check("progress", liveness=True)
async def progress():
    raise HealthError("no progress")

async def main():
    async with health:
        await asyncio.sleep(5)

asyncio.run(main())
"""


def _run_worker(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )


def test_a_stalled_worker_process_exits_with_70() -> None:
    """A real worker whose loop stalls exits with 70 and logs where it was."""
    # Act
    result = _run_worker(_STALLED_WORKER)

    # Assert
    assert result.returncode == _STALLED_EXIT_CODE
    assert "Event loop stalled" in result.stderr


def test_a_worker_failing_liveness_ends_by_sigterm() -> None:
    """A real worker that keeps failing a liveness check is ended by SIGTERM."""
    # Act
    result = _run_worker(_FAILING_WORKER)

    # Assert
    assert result.returncode == -signal.SIGTERM
    assert "Liveness check failed" in result.stderr


def test_watchdog_ends_quietly_when_the_loop_is_closed(exits: _Exits) -> None:
    """A watchdog whose loop closed stops watching without exiting."""
    # Arrange
    loop = asyncio.new_event_loop()
    watchdog = Watchdog(loop, stall_timeout=0.1)
    loop.close()

    # Act
    watchdog.start()
    time.sleep(0.2)
    watchdog.stop()

    # Assert
    assert not exits.exited.is_set()


def test_stop_process_sends_sigterm_to_this_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The graceful exit sends SIGTERM to the worker itself."""
    # Arrange
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "grelmicro.health._liveness.os.kill",
        lambda pid, sig: sent.append((pid, sig)),
    )

    # Act
    _liveness._stop_process()

    # Assert
    assert sent == [(os.getpid(), signal.SIGTERM)]


def test_exit_process_exits_with_70(monkeypatch: pytest.MonkeyPatch) -> None:
    """The immediate exit leaves with code 70."""
    # Arrange
    codes: list[int] = []
    monkeypatch.setattr("grelmicro.health._liveness.os._exit", codes.append)

    # Act
    _liveness._exit_process()

    # Assert
    assert codes == [_STALLED_EXIT_CODE]


def test_livez_answers_200_with_no_health_component() -> None:
    """A health router with no registered HealthChecks still answers 200."""
    # Arrange
    app = FastAPI()
    app.include_router(health_router())

    # Act
    with TestClient(app) as client:
        response = client.get("/livez")

    # Assert
    assert response.status_code == HTTP_200_OK


async def test_asgi_livez_answers_200_with_no_health_component() -> None:
    """health_asgi() with no component and no app answers 200 on /livez."""
    # Arrange
    transport = httpx.ASGITransport(app=health_asgi())

    # Act
    async with httpx.AsyncClient(
        transport=transport, base_url="http://p"
    ) as client:
        response = await client.get("/livez")

    # Assert
    assert response.status_code == HTTP_200_OK


async def test_a_liveness_check_added_after_opening_runs(exits: _Exits) -> None:
    """A liveness check registered after the component opened still runs."""
    # Arrange
    health = HealthChecks(
        liveness=Liveness(
            interval=0.01, failure_threshold=2, shutdown_timeout=0.05
        )
    )

    # Act
    async with health:
        health.add("late", _progress_check({"healthy": False}), liveness=True)
        await _wait_for(exits.stopped.is_set)
        await _wait_for(exits.exited.is_set)

    # Assert
    assert exits.stopped.is_set()


async def test_a_worker_that_does_not_stop_exits_after_shutdown_timeout(
    exits: _Exits,
) -> None:
    """A worker still running shutdown_timeout after SIGTERM exits at once."""
    # Arrange
    health = HealthChecks(
        liveness=Liveness(
            interval=0.01, failure_threshold=1, shutdown_timeout=0.1
        )
    )
    health.add("progress", _progress_check({"healthy": False}), liveness=True)

    # Act
    async with health:
        await _wait_for(exits.stopped.is_set)
        await _wait_for(exits.exited.is_set)

    # Assert
    assert exits.exited.is_set()


class _SlowToOpen:
    """A component whose opening blocks the event loop."""

    kind = "slow"
    name = "default"

    async def __aenter__(self) -> Self:
        time.sleep(0.5)  # noqa: ASYNC251  # blocks the event loop on purpose
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


async def test_watchdog_waits_for_the_app_to_open(exits: _Exits) -> None:
    """A component that blocks while the app opens never trips the watchdog."""
    # Arrange
    health = HealthChecks(liveness=Liveness(stall_timeout=0.2))
    micro = Grelmicro(uses=[health, _SlowToOpen()])

    # Act
    async with micro:
        await asyncio.sleep(0.4)

    # Assert
    assert not exits.exited.is_set()


async def test_liveness_without_a_liveness_check_stays_alive(
    exits: _Exits,
) -> None:
    """With Liveness set and no liveness check, every round passes."""
    # Arrange
    health = HealthChecks(liveness=Liveness(interval=0.01, failure_threshold=1))
    health.add("db", _progress_check({"healthy": False}))

    # Act
    async with health:
        await asyncio.sleep(0.1)

    # Assert
    assert health.is_alive
    assert not exits.stopped.is_set()
