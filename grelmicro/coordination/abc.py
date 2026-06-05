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

from typing_extensions import Doc

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from types import TracebackType


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
