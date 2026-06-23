"""Coordination Abstract Base Classes and Protocols."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Annotated,
    Protocol,
    Self,
    runtime_checkable,
)

from pydantic import PositiveFloat
from typing_extensions import Doc

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from types import TracebackType


@runtime_checkable
class LockBackend(Protocol):
    """Lock Backend Protocol.

    This is the low-level API for the distributed lock backend that is platform agnostic.

    Implementations capture the running event loop on ``__aenter__``
    in a ``_loop`` attribute so lock adapters (``Lock.from_thread``,
    ``TaskLock.from_thread``) can dispatch coroutines back into it.
    """

    async def __aenter__(self) -> Self:
        """Open the lock backend."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the lock backend."""
        ...

    async def acquire(
        self,
        *,
        name: Annotated[
            str,
            Doc("Identifier of the lock to acquire."),
        ],
        token: Annotated[
            str,
            Doc(
                "Caller-supplied ownership token. The same token must"
                " be passed to `release` and `owned`."
            ),
        ],
        duration: Annotated[
            float,
            Doc(
                "Seconds the lock is held before the backend may release"
                " it automatically. The acquirer should renew before"
                " this elapses."
            ),
        ],
    ) -> int | None:
        """Acquire the lock.

        Returns the fencing token when the lock was granted, `None` when
        another token already holds it.

        The fencing token is a strictly increasing integer per lock name.
        It increments on every free-to-held transition (a fresh acquire by
        a new holder, or a takeover of an expired lock) and keeps climbing
        across release and re-acquire cycles. The same holder renewing or
        extending its lease receives the same token.
        """
        ...

    async def release(
        self,
        *,
        name: Annotated[
            str,
            Doc("Identifier of the lock to release."),
        ],
        token: Annotated[
            str,
            Doc(
                "Ownership token previously passed to `acquire`. The"
                " backend only releases when the token matches."
            ),
        ],
    ) -> bool:
        """Release the lock.

        Returns `True` when the lock was released, `False` when the
        token did not own the lock.
        """
        ...

    async def locked(
        self,
        *,
        name: Annotated[
            str,
            Doc("Identifier of the lock to inspect."),
        ],
    ) -> bool:
        """Return `True` when the lock is currently held by any token."""
        ...

    async def owned(
        self,
        *,
        name: Annotated[
            str,
            Doc("Identifier of the lock to inspect."),
        ],
        token: Annotated[
            str,
            Doc("Ownership token to compare against the current holder."),
        ],
    ) -> bool:
        """Return `True` when the lock is currently held by `token`."""
        ...


@runtime_checkable
class LockPrimitive(Protocol):
    """Lock Primitive Protocol."""

    async def __aenter__(self) -> object:
        """Enter the lock primitive.

        Implementations return whatever the `async with` block binds. A
        `Lock` binds a `LockHandle`, a `TaskLock` binds itself.
        """

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        """Exit the lock primitive."""
        ...


Seconds = PositiveFloat


@runtime_checkable
class ScheduleBackend(Protocol):
    """Schedule Backend Protocol.

    Durable state for distributed cron. The backend stores one `last_fired`
    epoch per task name and offers a single atomic compare-and-set so exactly
    one worker runs each fire.

    The store survives restarts, so a fire missed while every worker was down
    is detected on restart and replayed once. A vendor backs it with whatever
    native primitive it offers (a Redis value, a Postgres row, a SQLite row).
    Redis, Postgres, and SQLite all ship, with Memory for tests. Kubernetes is
    intentionally not provided: use a native Kubernetes CronJob.

    Implementations capture the running event loop on `__aenter__` in a
    `_loop` attribute when they bridge from threads, mirroring `LockBackend`.
    """

    async def __aenter__(self) -> Self:
        """Open the schedule backend."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the schedule backend."""
        ...

    async def claim(
        self,
        name: Annotated[
            str,
            Doc("Identifier of the schedule to claim a fire for."),
        ],
        due: Annotated[
            float,
            Doc(
                "UTC epoch of the fire being claimed. The most recent"
                " scheduled fire that is due at or before now."
            ),
        ],
    ) -> bool:
        """Atomically claim the fire at `due`.

        Sets the stored `last_fired` to `due` only when no value is stored or
        the stored value is strictly less than `due`. Returns `True` when this
        call performed the set (it won the fire), `False` otherwise.

        The compare-and-set is the single point of coordination: across every
        worker, exactly one `claim` for a given `(name, due)` returns `True`.
        """
        ...

    async def last_fired(
        self,
        name: Annotated[
            str,
            Doc("Identifier of the schedule to read."),
        ],
    ) -> float | None:
        """Return the stored `last_fired` epoch, or `None` when never fired."""
        ...


@dataclass(frozen=True)
class LeaderRecord:
    """The state of a leader election lease.

    Unlike a `Lock`, a leader election lease carries observable state about who
    leads and since when. The shape follows the Kubernetes `LeaderElectionRecord`
    so the same record round-trips through a Redis value, a Postgres row, or a
    Kubernetes Lease.
    """

    holder: Annotated[
        str,
        Doc("Token of the worker that currently holds the lease."),
    ]
    lease_duration: Annotated[
        float,
        Doc("Seconds the lease is valid from `renewed_at` before it expires."),
    ]
    acquired_at: Annotated[
        datetime,
        Doc("When the current holder first acquired the lease."),
    ]
    renewed_at: Annotated[
        datetime,
        Doc("When the current holder last renewed the lease."),
    ]
    transitions: Annotated[
        int,
        Doc("Number of times the lease has changed holder."),
    ]
    metadata: Annotated[
        Mapping[str, str],
        Doc(
            "Free-form key/value pairs the holder attached, for observability"
            " (pod name, version, region). Empty when none were set."
        ),
    ] = field(default_factory=dict)


@runtime_checkable
class LeaderElectionBackend(Protocol):
    """Leader Election Backend Protocol.

    Optimized for leader election rather than general mutual exclusion: one
    renewable lease per election that stores a `LeaderRecord`, held continuously
    by the elected worker and renewed before it expires. A vendor backs it with
    whatever native lease it offers (a Redis value, a Postgres row, a Kubernetes
    Lease), storing the record alongside.
    """

    async def __aenter__(self) -> Self:
        """Open the leader election backend."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the leader election backend."""
        ...

    async def acquire_or_renew(
        self,
        *,
        name: Annotated[
            str,
            Doc("Identifier of the election to acquire or renew."),
        ],
        token: Annotated[
            str,
            Doc(
                "Worker token. The same token renews the lease, a different"
                " token may take over once the lease expires."
            ),
        ],
        duration: Annotated[
            float,
            Doc(
                "Seconds the lease is held before it expires. The leader"
                " renews before this elapses."
            ),
        ],
        metadata: Annotated[
            Mapping[str, str] | None,
            Doc(
                "Free-form key/value pairs to store on the lease while this"
                " worker holds it."
            ),
        ] = None,
    ) -> LeaderRecord:
        """Acquire leadership, or renew it when `token` already holds it.

        Returns the resulting `LeaderRecord`. The caller leads when
        `record.holder == token`, otherwise the record describes the current
        leader. Acquiring from a different (or expired) holder bumps
        `transitions`, renewing the same holder only moves `renewed_at`.
        """
        ...

    async def release(
        self,
        *,
        name: Annotated[
            str,
            Doc("Identifier of the election to release."),
        ],
        token: Annotated[
            str,
            Doc("Worker token. The backend only releases a matching holder."),
        ],
    ) -> bool:
        """Release leadership held by `token`.

        Returns `True` when leadership was released, `False` when `token` did
        not hold it.
        """
        ...

    async def get(
        self,
        *,
        name: Annotated[
            str,
            Doc("Identifier of the election to inspect."),
        ],
    ) -> LeaderRecord | None:
        """Return the current `LeaderRecord`, or `None` when no live lease exists."""
        ...
