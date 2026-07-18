"""Outbox relay: claims due messages and runs their handlers."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from datetime import timedelta
from logging import getLogger
from typing import TYPE_CHECKING, Self

from pydantic import ValidationError

from grelmicro.metrics import _emit
from grelmicro.outbox._control import Cancel, Retry
from grelmicro.outbox._otel import consumer_span

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from grelmicro.outbox._config import OutboxConfig
    from grelmicro.outbox._message import OutboxRecord
    from grelmicro.outbox._protocol import OutboxBackend
    from grelmicro.outbox._registry import OutboxRegistry

logger = getLogger("grelmicro.outbox")

_RANDOM = secrets.SystemRandom()
_MAX_BACKOFF_EXPONENT = 63


class Relay:
    """Background worker that delivers staged messages.

    Claims due messages for the registered topics with a visibility lease,
    runs their handlers outside any transaction, and marks them delivered.
    Failures are retried with backoff and dead-lettered after
    `max_attempts`.
    """

    def __init__(
        self,
        *,
        backend: OutboxBackend,
        registry: OutboxRegistry,
        config: OutboxConfig,
        shutdown_timeout: float = 30.0,
    ) -> None:
        """Initialize the relay."""
        self._backend = backend
        self._registry = registry
        self._config = config
        self._shutdown_timeout = shutdown_timeout
        # A literal False deletes delivered rows on success. True or a
        # retention timedelta keeps them (the janitor trims the timedelta).
        self._keep = config.keep_delivered is not False
        self._retention_seconds = (
            config.keep_delivered.total_seconds()
            if isinstance(config.keep_delivered, timedelta)
            else None
        )
        self._stop = asyncio.Event()
        self._slot_freed = asyncio.Event()
        self._inflight: set[asyncio.Task[None]] = set()
        self._loop_task: asyncio.Task[None] | None = None
        self._purge_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        """Start the relay loop and, when set, the retention janitor."""
        self._stop = asyncio.Event()
        self._loop_task = asyncio.create_task(self._run(), name="outbox-relay")
        if self._retention_seconds is not None:
            self._purge_task = asyncio.create_task(
                self._purge_loop(self._retention_seconds), name="outbox-purge"
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Stop the loop and drain in-flight handlers."""
        self._stop.set()
        self._slot_freed.set()
        if self._loop_task is not None:
            # `wait` returns when the loop task ends and never raises for the
            # task's own error or cancellation. An external cancellation of
            # this await still propagates, as it should.
            await asyncio.wait({self._loop_task})
            self._loop_task = None
        if self._purge_task is not None:
            await asyncio.wait({self._purge_task})
            self._purge_task = None
        await self._drain()

    async def _drain(self) -> None:
        """Let in-flight handlers finish, cancelling stragglers after the timeout.

        A cancelled handler leaves its message claimed. The lease lapses and
        another relay reclaims it, so cancelling is always safe.
        """
        handles = list(self._inflight)
        if handles and self._shutdown_timeout > 0:
            _, pending = await asyncio.wait(
                handles, timeout=self._shutdown_timeout
            )
        else:
            pending = set(handles)
        for handle in pending:
            handle.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._inflight.clear()

    async def _run(self) -> None:
        """Claim and dispatch messages until stopped."""
        cfg = self._config
        while not self._stop.is_set():
            topics = self._registry.topics()
            if not topics:
                await self._idle(cfg.poll_interval)
                continue
            free = cfg.concurrency - len(self._inflight)
            if free <= 0:
                await self._wait_slot()
                continue
            try:
                claimed = await self._backend.claim(
                    topics=topics,
                    limit=min(cfg.batch_size, free),
                    lease=cfg.lease_duration,
                )
            except Exception:
                logger.exception("Outbox claim failed, backing off")
                await self._idle(cfg.poll_interval)
                continue
            if not claimed:
                await self._idle(cfg.poll_interval)
                continue
            for record in claimed:
                self._dispatch(record)

    async def _purge_loop(self, retention: float) -> None:
        """Purge delivered rows past the retention window on an interval.

        Runs only when `keep_delivered` is a timedelta. Dead rows are never
        touched, since a dead-letter is a failure to inspect and redrive.
        The delete is idempotent, so every replica running it is safe.
        """
        interval = max(60.0, min(retention / 10, 3600.0))
        while not self._stop.is_set():
            try:
                removed = await self._backend.purge(
                    before_seconds=retention, states=("delivered",)
                )
            except Exception:
                logger.exception("Outbox purge failed, backing off")
            else:
                logger.debug(
                    "Outbox purged %d delivered rows past retention", removed
                )
            await self._race_stop(asyncio.sleep(interval))

    def _dispatch(self, record: OutboxRecord) -> None:
        """Start a handler task for a claimed record."""
        task = asyncio.create_task(
            self._deliver(record), name=f"outbox:{record.topic}"
        )
        self._inflight.add(task)
        task.add_done_callback(self._on_done)

    def _on_done(self, task: asyncio.Task[None]) -> None:
        """Free the handler slot when a delivery finishes."""
        self._inflight.discard(task)
        self._slot_freed.set()

    async def _deliver(self, record: OutboxRecord) -> None:
        """Run one handler and settle the message."""
        cfg = self._config
        if cfg.dead_letter and record.attempts > cfg.max_attempts:
            # A message reclaimed past its attempt budget crashed the relay
            # before it could settle in-process. Dead-letter it without
            # running the handler again, so a poison message that kills the
            # process cannot loop forever.
            await self._settle_dead(
                record, f"exceeded max_attempts ({cfg.max_attempts})"
            )
            return
        try:
            message = self._registry.build_message(record)
            entry = self._registry.get(record.topic)
            started = time.monotonic()
            with consumer_span(
                topic=record.topic,
                message_id=record.id,
                headers=record.headers,
            ):
                await entry.fn(message)
        except asyncio.CancelledError:
            raise
        except Cancel as cancel:
            await self._settle_dead(record, cancel.reason)
        except ValidationError as error:
            # A payload that cannot validate never will, so retrying only
            # burns attempts. Dead-letter it at once.
            await self._settle_dead(record, f"invalid payload: {error}")
        except Retry as retry:
            await self._settle_retry(record, "retry requested", retry.delay)
        except Exception as error:  # noqa: BLE001
            await self._settle_retry(
                record, f"{type(error).__name__}: {error}", None
            )
        else:
            _emit.record_duration(
                "grelmicro.outbox.handler_duration",
                time.monotonic() - started,
                topic=record.topic,
            )
            _emit.incr("grelmicro.outbox.delivered", topic=record.topic)
            await self._settle(
                self._backend.complete(
                    message_id=record.id,
                    attempts=record.attempts,
                    keep=self._keep,
                ),
                record,
                "mark delivered",
            )

    async def _settle_retry(
        self, record: OutboxRecord, error: str, delay: float | None
    ) -> None:
        """Reschedule a failed message or dead-letter it when exhausted."""
        cfg = self._config
        if cfg.dead_letter and record.attempts >= cfg.max_attempts:
            logger.error(
                "Outbox message %s on %r dead-lettered after %d attempts: %s",
                record.id,
                record.topic,
                record.attempts,
                error,
            )
            await self._settle_dead(record, error)
            return
        backoff = delay if delay is not None else self._backoff(record.attempts)
        logger.warning(
            "Outbox message %s on %r failed (attempt %d), retrying in %.1fs: %s",
            record.id,
            record.topic,
            record.attempts,
            backoff,
            error,
        )
        _emit.incr("grelmicro.outbox.retried", topic=record.topic)
        await self._settle(
            self._backend.reschedule(
                message_id=record.id,
                attempts=record.attempts,
                delay=backoff,
                error=error,
                dead=False,
            ),
            record,
            "reschedule",
        )

    async def _settle_dead(self, record: OutboxRecord, error: str) -> None:
        """Move a message to the dead state."""
        _emit.incr("grelmicro.outbox.dead_lettered", topic=record.topic)
        await self._settle(
            self._backend.reschedule(
                message_id=record.id,
                attempts=record.attempts,
                delay=0,
                error=error,
                dead=True,
            ),
            record,
            "dead-letter",
        )

    async def _settle(
        self, action: Awaitable[None], record: OutboxRecord, what: str
    ) -> None:
        """Run a settle action, logging failures instead of hiding them.

        A settle failure (the database is briefly unreachable) is not fatal:
        the lease lapses and another relay reclaims the message. It must be
        visible though, so it is logged rather than suppressed silently.
        """
        try:
            await action
        except Exception:
            logger.exception(
                "Outbox failed to %s message %s on %r",
                what,
                record.id,
                record.topic,
            )

    def _backoff(self, attempts: int) -> float:
        """Return the next delay with capped exponential backoff and jitter."""
        cfg = self._config
        exponent = min(attempts - 1, _MAX_BACKOFF_EXPONENT)
        raw = min(cfg.retry_max, cfg.retry_base * 2.0**exponent)
        floor = raw * (1 - cfg.retry_jitter)
        return floor + _RANDOM.random() * (raw - floor)

    async def _idle(self, timeout: float) -> None:  # noqa: ASYNC109
        """Wait for a wake notification, the timeout, or a stop signal."""
        await self._race_stop(self._backend.wait_notify(timeout=timeout))

    async def _wait_slot(self) -> None:
        """Wait for a free handler slot or a stop signal."""
        self._slot_freed.clear()
        await self._race_stop(self._slot_freed.wait())

    async def _race_stop(self, awaitable: Awaitable[object]) -> None:
        """Await `awaitable` but return early when the stop signal fires."""
        stop_wait = asyncio.ensure_future(self._stop.wait())
        other = asyncio.ensure_future(awaitable)
        try:
            await asyncio.wait(
                {stop_wait, other}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (stop_wait, other):
                if task.done():
                    # Retrieve any exception so it is not reported as never
                    # retrieved.
                    if not task.cancelled() and task.exception() is not None:
                        logger.debug(
                            "Outbox relay wait task errored",
                            exc_info=task.exception(),
                        )
                else:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
