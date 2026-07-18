"""Relay behavior tests driven by the in-memory backend."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel

from grelmicro.outbox import Cancel, Message, Outbox, Retry
from grelmicro.outbox._config import OutboxConfig
from grelmicro.outbox._registry import OutboxRegistry
from grelmicro.outbox._relay import Relay
from grelmicro.outbox.memory import MemoryOutboxAdapter

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from grelmicro.outbox._message import OutboxRecord

pytestmark = [pytest.mark.timeout(5)]


class WelcomeEmail(BaseModel):
    """A sample payload model."""

    to: str
    user_id: int


def _fast_outbox(*, relay: bool = True, max_attempts: int = 10) -> Outbox:
    """Build an outbox on the memory backend tuned for fast tests."""
    return Outbox(
        MemoryOutboxAdapter(),
        poll_interval=0.05,
        retry_base=0.02,
        retry_jitter=0,
        lease_duration=1,
        relay=relay,
        max_attempts=max_attempts,
    )


async def _wait(
    predicate: Callable[[], object],
    timeout: float = 2.0,  # noqa: ASYNC109
) -> None:
    """Poll until `predicate()` is truthy or the timeout elapses."""
    async with asyncio.timeout(timeout):
        while not predicate():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


async def test_typed_handler_receives_validated_model() -> None:
    """A typed handler gets the payload back as a validated model."""
    outbox = _fast_outbox()
    seen: list[Message[WelcomeEmail]] = []

    @outbox.handler(WelcomeEmail)
    async def handle(message: Message[WelcomeEmail]) -> None:
        seen.append(message)

    async with outbox:
        assert await outbox.publish(None, WelcomeEmail(to="a@b.c", user_id=1))
        await _wait(lambda: seen)

    assert seen[0].data == WelcomeEmail(to="a@b.c", user_id=1)
    assert seen[0].payload == {"to": "a@b.c", "user_id": 1}
    assert seen[0].attempts == 1


async def test_topic_handler_receives_raw_payload() -> None:
    """A topic handler gets the raw payload dict and no model."""
    outbox = _fast_outbox()
    seen: list[Message[object]] = []

    @outbox.handler("email.welcome")
    async def handle(message: Message[object]) -> None:
        seen.append(message)

    async with outbox:
        await outbox.publish(None, "email.welcome", {"to": "a@b.c"})
        await _wait(lambda: seen)

    assert seen[0].data is None
    assert seen[0].payload == {"to": "a@b.c"}


async def test_retry_then_success() -> None:
    """A failing handler is retried and the attempt counter climbs."""
    outbox = _fast_outbox()
    attempts: list[int] = []

    @outbox.handler("job")
    async def handle(message: Message[object]) -> None:
        attempts.append(message.attempts)
        if len(attempts) == 1:
            msg = "boom"
            raise RuntimeError(msg)

    async with outbox:
        await outbox.publish(None, "job", {})
        await _wait(lambda: len(attempts) >= 2)  # noqa: PLR2004

    assert attempts == [1, 2]


async def test_dead_letter_then_redrive() -> None:
    """An always-failing handler dead-letters, then redrive replays it."""
    outbox = _fast_outbox(max_attempts=2)
    calls = 0

    @outbox.handler("job")
    async def handle(message: Message[object]) -> None:  # noqa: ARG001
        nonlocal calls
        calls += 1
        msg = "always"
        raise RuntimeError(msg)

    async with outbox:
        await outbox.publish(None, "job", {})
        await _wait(lambda: calls >= 2)  # noqa: PLR2004
        # After max_attempts the message is dead and no longer delivered.
        await asyncio.sleep(0.2)
        assert calls == 2  # noqa: PLR2004
        moved = await outbox.redrive(topic="job")
        assert moved == 1
        await _wait(lambda: calls >= 3)  # noqa: PLR2004


async def test_cancel_signal_dead_letters_immediately() -> None:
    """A handler that raises Cancel dead-letters without retrying."""
    outbox = _fast_outbox(max_attempts=10)
    calls = 0

    @outbox.handler("job")
    async def handle(message: Message[object]) -> None:  # noqa: ARG001
        nonlocal calls
        calls += 1
        raise Cancel(reason="nope")

    async with outbox:
        await outbox.publish(None, "job", {})
        await _wait(lambda: calls >= 1)
        await asyncio.sleep(0.2)

    assert calls == 1


async def test_retry_signal_reschedules() -> None:
    """A handler that raises Retry is retried on its own delay."""
    outbox = _fast_outbox()
    calls = 0

    @outbox.handler("job")
    async def handle(message: Message[object]) -> None:  # noqa: ARG001
        nonlocal calls
        calls += 1
        if calls == 1:
            raise Retry(delay=0.02)

    async with outbox:
        await outbox.publish(None, "job", {})
        await _wait(lambda: calls >= 2)  # noqa: PLR2004

    assert calls == 2  # noqa: PLR2004


async def test_retry_signal_accepts_timedelta() -> None:
    """Retry accepts a timedelta delay, as the docs show."""
    outbox = _fast_outbox()
    calls = 0

    @outbox.handler("job")
    async def handle(message: Message[object]) -> None:  # noqa: ARG001
        nonlocal calls
        calls += 1
        if calls == 1:
            raise Retry(delay=timedelta(seconds=0.02))

    async with outbox:
        await outbox.publish(None, "job", {})
        await _wait(lambda: calls >= 2)  # noqa: PLR2004

    assert calls == 2  # noqa: PLR2004


async def test_invalid_payload_dead_letters_without_retry() -> None:
    """A payload that fails validation is dead-lettered at once."""
    outbox = _fast_outbox()
    calls = 0

    @outbox.handler(WelcomeEmail)
    async def handle(message: Message[WelcomeEmail]) -> None:  # noqa: ARG001
        nonlocal calls
        calls += 1

    async with outbox:
        # Missing required fields: the payload can never validate.
        await outbox.publish(None, "WelcomeEmail", {"to": "a@b.c"})
        await asyncio.sleep(0.3)

    assert calls == 0


async def test_relay_disabled_does_not_deliver() -> None:
    """With relay=False the message is staged but never delivered."""
    outbox = _fast_outbox(relay=False)
    calls = 0

    @outbox.handler("job")
    async def handle(message: Message[object]) -> None:  # noqa: ARG001
        nonlocal calls
        calls += 1

    async with outbox:
        await outbox.publish(None, "job", {})
        await asyncio.sleep(0.2)

    assert calls == 0


async def test_poison_crash_loop_dead_letters_at_claim() -> None:
    """A message past its attempt budget is dead-lettered before it runs.

    Simulates a handler that crashed the relay before it could settle: the
    row's attempts already exceed max_attempts when it is reclaimed.
    """
    backend = MemoryOutboxAdapter()
    outbox = Outbox(backend, poll_interval=0.05, max_attempts=3)
    calls = 0

    @outbox.handler("job")
    async def handle(message: Message[object]) -> None:  # noqa: ARG001
        nonlocal calls
        calls += 1

    async with outbox:
        await outbox.publish(None, "job", {})
        # Pretend the relay crashed on every prior attempt without settling.
        (row,) = backend._rows.values()
        row.attempts = 3
        await _wait(lambda: row.state == "dead")
        await asyncio.sleep(0.15)

    assert calls == 0


async def test_relay_with_no_handlers_idles() -> None:
    """The relay runs and shuts down cleanly with no handlers registered."""
    outbox = _fast_outbox()
    async with outbox:
        await asyncio.sleep(0.15)


async def test_concurrency_bounds_inflight_handlers() -> None:
    """With `concurrency=1` a second message waits for the first to finish."""
    outbox = Outbox(
        MemoryOutboxAdapter(),
        poll_interval=0.05,
        retry_jitter=0,
        concurrency=1,
    )
    release = asyncio.Event()
    started: list[object] = []

    @outbox.handler("job")
    async def handle(message: Message[object]) -> None:
        started.append(message.id)
        await release.wait()

    async with outbox:
        await outbox.publish(None, "job", {"n": 1})
        await outbox.publish(None, "job", {"n": 2})
        await _wait(lambda: len(started) == 1)
        # The single slot is busy, so the second message cannot start yet.
        await asyncio.sleep(0.2)
        assert len(started) == 1
        release.set()
        await _wait(lambda: len(started) == 2)  # noqa: PLR2004


async def test_shutdown_cancels_a_stuck_handler() -> None:
    """A handler still running at shutdown is cancelled after the grace window."""
    outbox = Outbox(
        MemoryOutboxAdapter(),
        poll_interval=0.05,
        retry_jitter=0,
        shutdown_timeout=0.1,
    )
    started = asyncio.Event()
    cancelled = False

    @outbox.handler("job")
    async def handle(message: Message[object]) -> None:  # noqa: ARG001
        nonlocal cancelled
        started.set()
        try:
            await asyncio.Event().wait()  # block forever
        except asyncio.CancelledError:
            cancelled = True
            raise

    async with outbox:
        await outbox.publish(None, "job", {})
        await started.wait()
    # Exiting drained: waited out shutdown_timeout, then cancelled the straggler.
    assert cancelled is True


async def test_settle_failure_is_logged_not_fatal() -> None:
    """A backend error while marking a message done is logged, not fatal."""

    class _BadComplete(MemoryOutboxAdapter):
        async def complete(self, **_kwargs: object) -> None:
            msg = "db down"
            raise RuntimeError(msg)

    outbox = Outbox(_BadComplete(), poll_interval=0.05, retry_jitter=0)
    calls = 0

    @outbox.handler("job")
    async def handle(message: Message[object]) -> None:  # noqa: ARG001
        nonlocal calls
        calls += 1

    async with outbox:
        await outbox.publish(None, "job", {})
        await _wait(lambda: calls >= 1)
        await asyncio.sleep(0.1)

    # The handler ran and the settle error did not crash the relay.
    assert calls >= 1


async def test_relay_aexit_without_start_is_safe() -> None:
    """Exiting a relay that never started drains cleanly."""
    relay = Relay(
        backend=MemoryOutboxAdapter(),
        registry=OutboxRegistry(),
        config=OutboxConfig(),
    )
    await relay.__aexit__(None, None, None)


async def test_relay_survives_wait_notify_error() -> None:
    """A failing wake wait is logged and the relay keeps running."""

    class _WaitBoom(MemoryOutboxAdapter):
        raised = False

        async def wait_notify(self, *, timeout: float) -> None:  # noqa: ASYNC109
            if not self.raised:
                self.raised = True
                msg = "wait boom"
                raise RuntimeError(msg)
            await super().wait_notify(timeout=timeout)

    backend = _WaitBoom()
    outbox = Outbox(backend, poll_interval=0.05, retry_jitter=0)
    seen: list[object] = []

    @outbox.handler("job")
    async def handle(message: Message[object]) -> None:
        seen.append(message)

    async with outbox:
        await _wait(lambda: backend.raised)
        await outbox.publish(None, "job", {})
        await _wait(lambda: seen)

    assert seen


async def test_relay_survives_claim_error() -> None:
    """A failing claim is logged and the relay keeps delivering."""

    class _FlakyBackend(MemoryOutboxAdapter):
        failed = False

        async def claim(
            self, *, topics: Sequence[str], limit: int, lease: float
        ) -> list[OutboxRecord]:
            if not self.failed:
                self.failed = True
                msg = "claim boom"
                raise RuntimeError(msg)
            return await super().claim(topics=topics, limit=limit, lease=lease)

    outbox = Outbox(_FlakyBackend(), poll_interval=0.05, retry_jitter=0)
    seen: list[object] = []

    @outbox.handler("job")
    async def handle(message: Message[object]) -> None:
        seen.append(message)

    async with outbox:
        await outbox.publish(None, "job", {})
        await _wait(lambda: seen)

    assert len(seen) == 1


async def test_dedup_key_skips_duplicate() -> None:
    """A duplicate dedup_key is skipped and delivered once."""
    outbox = _fast_outbox()
    calls = 0

    @outbox.handler("job")
    async def handle(message: Message[object]) -> None:  # noqa: ARG001
        nonlocal calls
        calls += 1

    async with outbox:
        assert await outbox.publish(None, "job", {"n": 1}, dedup_key="k")
        assert not await outbox.publish(None, "job", {"n": 2}, dedup_key="k")
        await _wait(lambda: calls >= 1)
        await asyncio.sleep(0.2)

    assert calls == 1
