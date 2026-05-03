"""Tests for the internal from_thread bridge."""

import asyncio
from threading import Thread

import pytest

from grelmicro import _from_thread

pytestmark = pytest.mark.anyio


def test_run_without_loop_raises() -> None:
    """run() raises when no loop is captured and none is in the contextvar."""
    error: list[BaseException] = []

    def worker() -> None:
        try:
            _from_thread.run(None, asyncio.sleep, 0)
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)

    thread = Thread(target=worker)
    thread.start()
    thread.join(timeout=1)

    assert len(error) == 1
    assert isinstance(error[0], RuntimeError)
    assert "captured event loop" in str(error[0])


def test_run_with_closed_loop_falls_back_to_contextvar() -> None:
    """A closed explicit loop falls back to the contextvar (which is unset here)."""
    closed_loop = asyncio.new_event_loop()
    closed_loop.close()

    error: list[BaseException] = []

    def worker() -> None:
        try:
            _from_thread.run(closed_loop, asyncio.sleep, 0)
        except BaseException as exc:  # noqa: BLE001
            error.append(exc)

    thread = Thread(target=worker)
    thread.start()
    thread.join(timeout=1)

    assert len(error) == 1
    assert isinstance(error[0], RuntimeError)


def test_capture_running_loop_outside_loop_returns_none() -> None:
    """capture_running_loop returns None when no loop is running."""
    assert _from_thread.capture_running_loop() is None


async def test_remember_running_loop_inside_loop_returns_loop() -> None:
    """remember_running_loop captures the running loop and returns it."""
    loop = _from_thread.remember_running_loop()
    assert loop is asyncio.get_running_loop()
