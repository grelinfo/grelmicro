"""Read-write lock guards."""

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING

from grelmicro.coordination.errors import LockNotOwnedError

if TYPE_CHECKING:
    from grelmicro.coordination.readwritelock import ReadMode, WriteMode


class _BaseGuard:
    """State shared by the read and write guards."""

    __slots__ = ("_expires_at", "_name", "_released", "_token")

    def __init__(self, *, name: str, token: str, expires_at: float) -> None:
        """Initialize the guard."""
        self._name = name
        self._token = token
        self._expires_at = expires_at
        self._released = False

    @property
    def name(self) -> str:
        """The lock name, as passed to `ReadWriteLock(name)`."""
        return self._name

    @property
    def token(self) -> str:
        """The opaque ownership token the backend stored for this holder."""
        return self._token

    @property
    def expires_in(self) -> float:
        """Seconds left on the lease, `0.0` once it has run out."""
        return max(0.0, self._expires_at - monotonic())

    @property
    def valid(self) -> bool:
        """Whether the guard still holds the lock, as far as this process knows.

        `False` once the guard was released or its lease ran out. A lease
        taken over on the backend while this process was paused still reads
        as valid until it runs out, which is what the fencing token is for.
        """
        return not self._released and self._expires_at > monotonic()

    def _renewed(self, expires_at: float) -> None:
        """Record a renewed lease deadline."""
        self._expires_at = expires_at

    def _invalidate(self) -> None:
        """Mark the guard as no longer holding the lock."""
        self._released = True

    def _check(self) -> None:
        """Raise unless the guard still holds the lock.

        Raises:
            LockNotOwnedError: The guard was released or its lease ran out.
        """
        if not self.valid:
            raise LockNotOwnedError(name=self._name)


class ReadGuard(_BaseGuard):
    """The result of a granted read acquisition.

    Bound by `async with lock.read`, and returned by `acquire`,
    `acquire_nowait`, and `WriteGuard.downgrade`. Each acquisition produces
    its own guard, so a `ReadWriteLock` shared by several tasks gives each
    reader a distinct guard.
    """

    __slots__ = ("_generation", "_owner")

    def __init__(
        self,
        *,
        owner: ReadMode,
        name: str,
        token: str,
        generation: int,
        expires_at: float,
    ) -> None:
        """Initialize the read guard."""
        super().__init__(name=name, token=token, expires_at=expires_at)
        self._owner = owner
        self._generation = generation

    @property
    def generation(self) -> int:
        """The fencing token of the write this read observes.

        Compare it across two reads to tell whether a writer landed in
        between.

        Raises:
            LockNotOwnedError: The guard was released or its lease ran out.
        """
        self._check()
        return self._generation

    async def extend(self) -> None:
        """Renew this reader's lease for another `lease_duration`.

        Raises:
            LockNotOwnedError: This reader no longer holds a live lease.
            LockAcquireError: The backend call failed.
        """
        self._check()
        await self._owner.do_extend(self)

    def __repr__(self) -> str:
        """Return the guard representation."""
        return (
            f"ReadGuard(name={self._name!r}, generation={self._generation},"
            f" valid={self.valid})"
        )


class WriteGuard(_BaseGuard):
    """The result of a granted write acquisition.

    Bound by `async with lock.write`, and returned by `acquire` and
    `acquire_nowait`.
    """

    __slots__ = ("_fencing_token", "_owner", "_poisoned")

    def __init__(
        self,
        *,
        owner: WriteMode,
        name: str,
        token: str,
        fencing_token: int,
        poisoned: bool,
        expires_at: float,
    ) -> None:
        """Initialize the write guard."""
        super().__init__(name=name, token=token, expires_at=expires_at)
        self._owner = owner
        self._fencing_token = fencing_token
        self._poisoned = poisoned

    @property
    def fencing_token(self) -> int:
        """A strictly increasing integer minted by the backend for this name.

        Pass it to the protected resource so the resource can reject any
        write that carries a lower or equal token.

        Raises:
            LockNotOwnedError: The guard was released or its lease ran out.
        """
        self._check()
        return self._fencing_token

    @property
    def poisoned(self) -> bool:
        """Whether the previous writer's lease expired without a release.

        Stays readable after release, because it describes how this
        acquisition came about rather than whether the lock is still held.
        """
        return self._poisoned

    async def extend(self) -> None:
        """Renew the write lease for another `lease_duration`.

        The fencing token is unchanged.

        Raises:
            LockNotOwnedError: This writer no longer holds the lock.
            LockAcquireError: The backend call failed.
        """
        self._check()
        await self._owner.do_extend(self)

    async def downgrade(self) -> ReadGuard:
        """Turn this write lease into a read lease in one step.

        No other writer takes the lock in between. This guard is spent
        afterwards, and the returned `ReadGuard` is released when the
        `async with lock.write` block exits.

        Raises:
            LockNotOwnedError: This writer no longer holds the lock.
            LockAcquireError: The backend call failed.
        """
        self._check()
        return await self._owner.do_downgrade(self)

    def __repr__(self) -> str:
        """Return the guard representation."""
        return (
            f"WriteGuard(name={self._name!r},"
            f" fencing_token={self._fencing_token},"
            f" poisoned={self._poisoned}, valid={self.valid})"
        )
