"""Outbox Backend Protocol.

This module defines a `typing.Protocol`. Methods end with `...` because
the protocol describes a structural contract, not an implementation.
Concrete backends (`PostgresOutboxAdapter`) provide the bodies.
"""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
    Annotated,
    Protocol,
    Self,
    runtime_checkable,
)

from typing_extensions import Doc

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType
    from uuid import UUID

    from grelmicro.outbox._message import OutboxRecord


@runtime_checkable
class OutboxBackend(Protocol):
    """Protocol for outbox storage backends."""

    async def __aenter__(self) -> Self:
        """Open the backend, install the schema, start the listener."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the backend and its listener."""
        ...

    async def enqueue(
        self,
        handle: Annotated[
            object,
            Doc(
                "The caller's open connection or session. The insert joins it."
            ),
        ],
        record: Annotated[
            OutboxRecord,
            Doc("The message to stage."),
        ],
    ) -> bool:
        """Stage a record inside the caller's transaction.

        Returns False when a `dedup_key` collides and the row is skipped,
        True when the row is inserted.
        """
        ...

    async def claim(
        self,
        *,
        topics: Annotated[
            Sequence[str],
            Doc("Topics with a registered handler. Only these are claimed."),
        ],
        limit: Annotated[
            int,
            Doc("Maximum number of messages to claim."),
        ],
        lease: Annotated[
            float,
            Doc("Seconds the claimed messages stay invisible."),
        ],
    ) -> list[OutboxRecord]:
        """Claim up to `limit` due messages for the given topics."""
        ...

    async def complete(
        self,
        *,
        message_id: Annotated[UUID, Doc("The delivered message id.")],
        attempts: Annotated[
            int,
            Doc("The claimed attempt count, fencing a stale relay's write."),
        ],
        keep: Annotated[
            bool,
            Doc("Keep the row in the delivered state instead of deleting it."),
        ],
    ) -> None:
        """Mark a message delivered."""
        ...

    async def reschedule(
        self,
        *,
        message_id: Annotated[UUID, Doc("The message id to reschedule.")],
        attempts: Annotated[
            int,
            Doc("The claimed attempt count, fencing a stale relay's write."),
        ],
        delay: Annotated[float, Doc("Seconds until the next attempt.")],
        error: Annotated[str, Doc("The last handler error.")],
        dead: Annotated[
            bool,
            Doc("Move the message to the dead state instead of retrying."),
        ],
    ) -> None:
        """Reschedule a failed message or dead-letter it."""
        ...

    async def purge(
        self,
        *,
        before_seconds: Annotated[
            float | None,
            Doc("Only purge terminal rows older than this many seconds."),
        ] = None,
    ) -> int:
        """Delete delivered and dead rows. Returns the count removed."""
        ...

    async def redrive(
        self,
        *,
        topic: Annotated[
            str | None,
            Doc("Restrict to one topic. None redrives every dead message."),
        ] = None,
    ) -> int:
        """Move dead messages back to pending. Returns the count moved."""
        ...

    async def wait_notify(
        self,
        *,
        timeout: Annotated[  # noqa: ASYNC109
            float, Doc("Maximum seconds to wait for a wake.")
        ],
    ) -> None:
        """Return when a new message is signalled or `timeout` elapses."""
        ...
