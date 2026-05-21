"""Shared fixtures for Shield tests."""

from __future__ import annotations

import asyncio as _asyncio

import pytest


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch `asyncio.sleep` to a no-op to keep tests fast."""

    async def _async_noop(_seconds: float) -> None:
        return

    monkeypatch.setattr(_asyncio, "sleep", _async_noop)


@pytest.fixture
def deterministic_random() -> _DeterministicRandom:
    """Return a deterministic source for the backoff jitter factor."""
    return _DeterministicRandom()


class _DeterministicRandom:
    """Returns the same value every call, default 0.5."""

    def __init__(self, value: float = 0.5) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _FakeClock:
    """Manually advanced monotonic clock for adaptive-gate tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
