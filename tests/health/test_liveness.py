"""Tests for liveness checks and the loop watchdog."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Self, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
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
from grelmicro.providers._base import Provider

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
    health = HealthChecks(cache_ttl=0, liveness=Liveness())
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
    health.add("db", _progress_check({"healthy": False}))
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


class _YieldsOnOpen:
    """A component whose opening yields to the event loop."""

    kind = "yields"
    name = "default"

    async def __aenter__(self) -> Self:
        await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def test_liveness_reopened_app_on_a_new_loop_runs_its_checks(
    exits: _Exits,
) -> None:
    """An app reopened under a second event loop still runs liveness checks."""
    # Arrange
    calls: list[int] = []
    health = HealthChecks(
        liveness=Liveness(interval=0.01, failure_threshold=1000)
    )

    @health.check("progress", liveness=True)
    async def progress() -> None:
        calls.append(1)

    micro = Grelmicro(uses=[health, _YieldsOnOpen()])

    async def open_and_wait_for_a_round() -> None:
        before = len(calls)
        async with micro:
            await _wait_for(lambda: len(calls) > before)

    # Act
    asyncio.run(open_and_wait_for_a_round())
    asyncio.run(open_and_wait_for_a_round())

    # Assert
    assert health.is_alive
    assert not exits.stopped.is_set()


def _raise_runtime_error(*_args: object) -> None:
    msg = "liveness broke"
    raise RuntimeError(msg)


async def _raise_runtime_error_async(*_args: object) -> None:
    _raise_runtime_error()


async def test_liveness_task_error_is_logged_when_it_ends(
    exits: _Exits,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An error that ends the liveness task is logged at once, not on close."""
    # Arrange
    monkeypatch.setattr(
        "grelmicro.health._checks._app_open", _raise_runtime_error
    )
    health = HealthChecks(liveness=Liveness(interval=0.01))

    # Act
    with caplog.at_level("ERROR", logger="grelmicro.health"):
        async with health:
            await _wait_for(exits.stopped.is_set)

    # Assert
    logged = [
        record
        for record in caplog.records
        if record.exc_info and str(record.exc_info[1]) == "liveness broke"
    ]
    assert len(logged) == 1


async def test_liveness_task_error_stops_the_process(
    exits: _Exits,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error that ends the liveness task stops the worker, then exits it."""
    # Arrange
    monkeypatch.setattr(
        "grelmicro.health._checks._app_open", _raise_runtime_error
    )
    health = HealthChecks(liveness=Liveness(shutdown_timeout=0.05))

    # Act
    async with health:
        await _wait_for(exits.stopped.is_set)
        await _wait_for(exits.exited.is_set)

    # Assert
    assert exits.stopped.is_set()


async def test_liveness_task_error_never_escapes_closing(
    exits: _Exits,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the component after the liveness task failed raises nothing."""
    # Arrange
    monkeypatch.setattr(
        "grelmicro.health._checks._app_open", _raise_runtime_error
    )
    health = HealthChecks(liveness=Liveness(interval=0.01))

    # Act
    async with health:
        await _wait_for(exits.stopped.is_set)

    # Assert
    assert health._liveness_task is None


async def test_liveness_task_error_reports_not_alive(
    exits: _Exits,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker whose liveness task died is no longer alive."""
    # Arrange
    monkeypatch.setattr(
        "grelmicro.health._checks._app_open", _raise_runtime_error
    )
    health = HealthChecks(liveness=Liveness(interval=0.01))

    # Act
    async with health:
        task = health._liveness_task
        assert task is not None
        await asyncio.wait({task})
        alive = health.is_alive

    # Assert
    assert not alive
    assert exits.stopped.is_set()


async def test_liveness_task_error_still_stops_the_watchdog(
    exits: _Exits,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing after the liveness task failed still stops the watchdog."""
    # Arrange
    monkeypatch.setattr(
        "grelmicro.health._checks._run_check", _raise_runtime_error_async
    )
    health = HealthChecks(liveness=Liveness(interval=0.01, stall_timeout=0.2))
    health.add("progress", _progress_check({"healthy": True}), liveness=True)

    # Act
    async with health:
        await _wait_for(lambda: not health.is_alive)
        watchdog = health._watchdog
        assert watchdog is not None

    # Assert
    assert not watchdog._thread.is_alive()
    assert exits.stopped.is_set()


class _ReadyProvider(Provider):
    """A provider with a passing readiness check and no backend."""

    short_name = "ready"

    async def check(self) -> None:
        return None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


async def test_liveness_task_error_still_removes_auto_registered_checks(
    exits: _Exits,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing after the liveness task failed still drops auto_health checks."""
    # Arrange
    monkeypatch.setattr(
        "grelmicro.health._checks._app_open", _raise_runtime_error
    )
    health = HealthChecks(
        cache_ttl=0, auto_health=True, liveness=Liveness(interval=0.01)
    )
    micro = Grelmicro(uses=[_ReadyProvider(), health])

    # Act
    async with micro:
        await _wait_for(lambda: not health.is_alive)
    report = await health.run()

    # Assert
    assert report["checks"] == {}
    assert exits.stopped.is_set()


async def test_liveness_closed_run_error_leaves_the_next_run_alive(
    exits: _Exits,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed run's task failing late never marks the next run as dead."""
    # Arrange
    from grelmicro.health import _checks  # noqa: PLC0415

    release = asyncio.Event()
    original = _checks._run_check
    first_call = [True]

    async def hang_then_fail(entry: _checks._Entry) -> object:
        if not first_call[0]:
            return await original(entry)
        first_call[0] = False
        while not release.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await release.wait()
        _raise_runtime_error()
        return None

    monkeypatch.setattr("grelmicro.health._checks._run_check", hang_then_fail)
    health = HealthChecks(liveness=Liveness(interval=0.01, failure_threshold=1))
    health.add("progress", _progress_check({"healthy": True}), liveness=True)
    await health.__aenter__()
    old_task = health._liveness_task
    assert old_task is not None
    await _wait_for(lambda: not first_call[0])
    closing = asyncio.create_task(health.__aexit__(None, None, None))
    await asyncio.sleep(0.05)
    closing.cancel()
    await asyncio.wait({closing})

    # Act
    await health.__aenter__()
    release.set()
    await asyncio.wait({old_task})
    await asyncio.sleep(0.05)
    alive = health.is_alive
    await health.__aexit__(None, None, None)

    # Assert
    assert alive
    assert not exits.stopped.is_set()


class _BlocksOnClose:
    """A component whose closing blocks the event loop."""

    kind = "blocks"
    name = "default"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        time.sleep(0.8)  # noqa: ASYNC251  # blocks the event loop on purpose


async def test_watchdog_stays_quiet_while_a_later_component_closes(
    exits: _Exits,
) -> None:
    """A component closing after the app started closing never trips it."""
    # Arrange
    health = HealthChecks(liveness=Liveness(stall_timeout=0.2))
    micro = Grelmicro(uses=[health, _BlocksOnClose()])

    # Act
    async with micro:
        await _wait_for(lambda: health._watchdog is not None)
        await asyncio.sleep(0.1)

    # Assert
    assert not exits.exited.is_set()


def test_watchdog_stays_quiet_while_the_loop_is_stopped(exits: _Exits) -> None:
    """A loop stopped between two runs, and not closed, never trips it."""
    # Arrange
    health = HealthChecks(liveness=Liveness(stall_timeout=0.2))
    loop = asyncio.new_event_loop()

    async def wait_for_the_watchdog() -> None:
        await _wait_for(lambda: health._watchdog is not None)
        await asyncio.sleep(0.1)

    try:
        loop.run_until_complete(health.__aenter__())
        loop.run_until_complete(wait_for_the_watchdog())

        # Act
        time.sleep(0.8)
        loop.run_until_complete(asyncio.sleep(0.1))
        loop.run_until_complete(health.__aexit__(None, None, None))
    finally:
        loop.close()

    # Assert
    assert not exits.exited.is_set()


class _LoopClosedMidTick:
    """A loop that reads as running, then refuses a callback as closed."""

    def is_running(self) -> bool:
        return True

    def call_soon_threadsafe(self, *_args: object) -> None:
        msg = "Event loop is closed"
        raise RuntimeError(msg)


def test_watchdog_ends_quietly_when_the_loop_closes_mid_tick(
    exits: _Exits,
) -> None:
    """A watchdog whose loop closes while it posts a beat stops watching."""
    # Arrange
    loop = cast("asyncio.AbstractEventLoop", _LoopClosedMidTick())
    watchdog = Watchdog(loop, stall_timeout=0.1)

    # Act
    watchdog.start()
    watchdog._thread.join(timeout=2)

    # Assert
    assert not watchdog._thread.is_alive()
    assert not exits.exited.is_set()


def test_liveness_check_without_liveness_is_refused() -> None:
    """add(liveness=True) on a HealthChecks with no Liveness raises."""
    # Arrange
    health = HealthChecks()

    # Act
    with pytest.raises(ValueError, match="liveness=Liveness") as excinfo:
        health.add(
            "progress", _progress_check({"healthy": True}), liveness=True
        )

    # Assert
    assert excinfo.type is ValueError


def test_liveness_check_decorator_without_liveness_is_refused() -> None:
    """@check(liveness=True) on a HealthChecks with no Liveness raises."""
    # Arrange
    health = HealthChecks()

    # Act
    with pytest.raises(ValueError, match="liveness=Liveness") as excinfo:

        @health.check("progress", liveness=True)
        async def progress() -> None:
            return None

    # Assert
    assert excinfo.type is ValueError


async def test_liveness_non_critical_check_failing_stays_alive(
    exits: _Exits,
) -> None:
    """A failing liveness check with critical=False never flips is_alive."""
    # Arrange
    calls: list[int] = []
    health = HealthChecks(liveness=Liveness(interval=0.01, failure_threshold=1))

    @health.check("progress", critical=False, liveness=True)
    async def progress() -> None:
        calls.append(1)
        msg = "no progress"
        raise HealthError(msg)

    # Act
    async with health:
        await _wait_for(lambda: len(calls) >= 5)  # noqa: PLR2004
        alive = health.is_alive

    # Assert
    assert alive
    assert not exits.stopped.is_set()


async def test_liveness_critical_check_failing_flips_is_alive(
    exits: _Exits,
) -> None:
    """A failing liveness check with critical=True flips is_alive."""
    # Arrange
    health = HealthChecks(
        liveness=Liveness(interval=0.01, failure_threshold=1000)
    )
    health.add(
        "progress",
        _progress_check({"healthy": False}),
        critical=True,
        liveness=True,
    )

    # Act
    async with health:
        await _wait_for(lambda: not health.is_alive)

    # Assert
    assert not exits.stopped.is_set()


def test_fastapi_livez_documents_503() -> None:
    """The FastAPI /livez declares its 503 answer in the OpenAPI schema."""
    # Arrange
    app = FastAPI()
    app.include_router(health_router(include_in_schema=True))

    # Act
    with TestClient(app) as client:
        paths = client.get("/openapi.json").json()["paths"]

    # Assert
    assert "503" in paths["/livez"]["get"]["responses"]


async def test_watchdog_catches_a_stall_after_the_app_reopens(
    exits: _Exits,
) -> None:
    """A stall after the app closed and reopened, health still open, exits."""
    # Arrange
    health = HealthChecks(liveness=Liveness(stall_timeout=0.2))
    micro = Grelmicro(uses=[health])
    async with micro:
        await health.__aenter__()
        await _wait_for(lambda: health._watchdog is not None)
    await asyncio.sleep(0.1)

    # Act
    try:
        async with micro:
            await asyncio.sleep(0.1)
            time.sleep(0.8)  # noqa: ASYNC251  # blocks the event loop on purpose
            await asyncio.sleep(0)
    finally:
        await health.__aexit__(None, None, None)

    # Assert
    assert exits.exited.is_set()


async def test_liveness_rounds_resume_after_the_app_reopens(
    exits: _Exits,
) -> None:
    """Liveness checks run again after the app closed and reopened."""
    # Arrange
    calls: list[int] = []
    health = HealthChecks(
        liveness=Liveness(interval=0.01, failure_threshold=1000)
    )

    @health.check("progress", liveness=True)
    async def progress() -> None:
        calls.append(1)

    micro = Grelmicro(uses=[health])
    async with micro:
        await health.__aenter__()
        await _wait_for(lambda: bool(calls))

    # Act
    try:
        async with micro:
            before = len(calls)
            await _wait_for(lambda: len(calls) > before)
    finally:
        await health.__aexit__(None, None, None)

    # Assert
    assert health.is_alive
    assert not exits.stopped.is_set()


# --- Wait settings ---


@pytest.mark.parametrize(
    "field", ["interval", "stall_timeout", "shutdown_timeout"]
)
@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "inf", "minus-inf"],
)
def test_liveness_non_finite_wait_refused(field: str, value: float) -> None:
    """A liveness wait that is not a finite number is refused, naming it."""
    # Act / Assert
    with pytest.raises(
        ValidationError, match=f"{field} must be a finite number"
    ):
        Liveness(**{field: value})


@pytest.mark.parametrize(
    "field", ["interval", "stall_timeout", "shutdown_timeout"]
)
@pytest.mark.parametrize("value", [0, -1.0])
def test_liveness_wait_of_zero_or_less_refused(
    field: str, value: float
) -> None:
    """A liveness wait of zero or less is refused, naming it."""
    # Act / Assert
    with pytest.raises(
        ValidationError, match=f"{field} must be greater than zero"
    ):
        Liveness(**{field: value})


def test_liveness_stall_timeout_none_starts_no_watchdog() -> None:
    """`stall_timeout=None` is taken, meaning no watchdog."""
    # Act
    liveness = Liveness(stall_timeout=None)

    # Assert
    assert liveness.stall_timeout is None
