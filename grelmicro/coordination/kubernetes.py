"""Kubernetes Coordination Adapters."""

import asyncio
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from math import ceil
from types import TracebackType
from typing import Annotated, ClassVar, Self

from lightkube import AsyncClient, KubeConfig
from lightkube.core.exceptions import ApiError
from lightkube.models.coordination_v1 import LeaseSpec
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.coordination_v1 import Lease
from pydantic_settings import BaseSettings
from typing_extensions import Doc

from grelmicro.coordination._protocol import (
    LeaderRecord,
    LockBackend,
    ReadWriteLockBackend,
    ReadWriteLockState,
    WriteGrant,
)
from grelmicro.coordination.errors import CoordinationSettingsValidationError
from grelmicro.errors import OutOfContextError
from grelmicro.types import BackendScope

_LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
_LABEL_MANAGED_BY_VALUE = "grelmicro"
_METADATA_ANNOTATION_PREFIX = "grelmicro.io/"
_INVALID_CHARS = re.compile(r"[^a-z0-9-]")
_CONSECUTIVE_HYPHENS = re.compile(r"-{2,}")
_MAX_NAME_LENGTH = 253


class _KubernetesSettings(BaseSettings):
    """Kubernetes settings from the environment variables."""

    KUBE_NAMESPACE: str | None = None


def _get_kube_namespace() -> str:
    """Get the Kubernetes namespace from the environment variables.

    Raises:
        CoordinationSettingsValidationError: If KUBE_NAMESPACE is not set.
    """
    settings = _KubernetesSettings()

    if settings.KUBE_NAMESPACE:
        return settings.KUBE_NAMESPACE

    msg = "KUBE_NAMESPACE must be set"
    raise CoordinationSettingsValidationError(msg)


def _sanitize_lease_name(name: str) -> str:
    """Sanitize an election name to a valid Kubernetes resource name.

    RFC 1123: lowercase, alphanumeric and hyphens, max 253 chars, must start
    and end with alphanumeric.

    Raises:
        ValueError: If the name contains no valid characters.

    Examples:
        ``"election:my-service"`` -> ``"election-my-service"``
        ``"UPPER_CASE"``          -> ``"upper-case"``
    """
    sanitized = _INVALID_CHARS.sub("-", name.lower())
    sanitized = _CONSECUTIVE_HYPHENS.sub("-", sanitized)
    sanitized = sanitized[:_MAX_NAME_LENGTH].strip("-")
    if not sanitized:
        msg = f"Name produces an empty Kubernetes resource name: {name!r}"
        raise ValueError(msg)
    return sanitized


def _annotations_to_metadata(
    annotations: dict[str, str] | None,
) -> dict[str, str]:
    """Read the free-form metadata map back from Lease annotations.

    Only annotations under the grelmicro namespaced prefix are returned, with
    the prefix stripped from each key.
    """
    if not annotations:
        return {}
    return {
        key[len(_METADATA_ANNOTATION_PREFIX) :]: value
        for key, value in annotations.items()
        if key.startswith(_METADATA_ANNOTATION_PREFIX)
    }


def _metadata_to_annotations(metadata: dict[str, str]) -> dict[str, str]:
    """Map the free-form metadata into namespaced Lease annotations."""
    return {
        f"{_METADATA_ANNOTATION_PREFIX}{key}": value
        for key, value in metadata.items()
    }


class KubernetesLockAdapter(LockBackend):
    """Kubernetes Lock Adapter.

    Holds each lock in a `coordination.k8s.io/v1` Lease object, one per lock.
    The Lease spec carries the holder token and duration, and atomicity comes
    from Kubernetes optimistic concurrency: a Lease is read with its
    `resourceVersion` and written back with it, so a concurrent writer loses
    the race with a 409 Conflict.

    Fencing tokens use the Lease `spec.leaseTransitions` counter. It is
    incremented by one on every free-to-held transition (a fresh acquire or a
    takeover of a vacated or expired Lease) and kept on a same-holder extend.
    Release vacates the holder in place (clearing `holderIdentity`,
    `acquireTime`, and `renewTime`) but keeps the Lease object and its
    transitions counter, so fencing tokens are strictly monotonic per name
    across release and re-acquire cycles. The Lease is never deleted while the
    backend is open.
    """

    scope: ClassVar[BackendScope] = "cluster"
    """State is shared by every process that connects to it."""

    def __init__(
        self,
        namespace: Annotated[
            str | None,
            Doc("""
                The Kubernetes namespace.

                If not provided, the namespace will be taken from the
                environment variable KUBE_NAMESPACE.
                """),
        ] = None,
        *,
        prefix: Annotated[
            str,
            Doc("""
                Prefix prepended to lease names to avoid conflicts
                with other applications in the same namespace.

                By default no prefix is added.
                """),
        ] = "",
        kubeconfig: Annotated[
            str | None,
            Doc("Path to the kubeconfig file."),
        ] = None,
    ) -> None:
        """Initialize the lock backend."""
        self._namespace = namespace or _get_kube_namespace()
        self._prefix = prefix
        self._kubeconfig = kubeconfig
        self._client: AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self) -> Self:
        """Open the lock backend."""
        self._loop = asyncio.get_running_loop()
        config = (
            KubeConfig.from_file(self._kubeconfig) if self._kubeconfig else None
        )
        self._client = AsyncClient(config=config)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the lock backend.

        Vacates the holder of any expired Lease in place rather than deleting
        it, so the `leaseTransitions` fence counter survives for the next
        acquire. A still-live Lease is left untouched.
        """
        if self._client:  # pragma: no branch
            now = datetime.now(tz=UTC)
            async for lease in self._client.list(
                Lease,
                namespace=self._namespace,
                labels={_LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE},
            ):
                expire_at = _get_expire_at(lease)
                holder = lease.spec.holderIdentity if lease.spec else None
                if (
                    holder is not None
                    and expire_at is not None
                    and expire_at < now
                ):
                    await self._vacate_lease(lease)
            await self._client.close()
            self._client = None

    async def acquire(
        self, *, name: str, token: str, duration: float
    ) -> int | None:
        """Acquire a lock, returning the fencing token or `None`."""
        if not self._client:
            raise OutOfContextError(self, "acquire")

        lease_name = _sanitize_lease_name(f"{self._prefix}{name}")
        now = datetime.now(tz=UTC)

        try:
            lease = await self._client.get(
                Lease, name=lease_name, namespace=self._namespace
            )
        except ApiError as e:
            if e.status.code != HTTPStatus.NOT_FOUND:
                raise
            return await self._create_lease(lease_name, token, duration)

        current_expire_at = _get_expire_at(lease)
        current_holder = lease.spec.holderIdentity if lease.spec else None
        live = current_expire_at is not None and current_expire_at >= now

        if live and current_holder != token:
            return None

        return await self._replace_lease(
            lease, token, duration, live=live, holder=current_holder
        )

    async def release(self, *, name: str, token: str) -> bool:
        """Release the lock by vacating the holder in place.

        Clears `holderIdentity`, `acquireTime`, and `renewTime` so the Lease
        reads as free, while keeping the Lease object and its
        `leaseTransitions` counter so fencing tokens keep climbing on the next
        acquire.
        """
        if not self._client:
            raise OutOfContextError(self, "release")

        lease_name = _sanitize_lease_name(f"{self._prefix}{name}")
        now = datetime.now(tz=UTC)

        try:
            lease = await self._client.get(
                Lease, name=lease_name, namespace=self._namespace
            )
        except ApiError as e:
            if e.status.code == HTTPStatus.NOT_FOUND:
                return False
            raise

        current_expire_at = _get_expire_at(lease)
        current_holder = lease.spec.holderIdentity if lease.spec else None

        if (
            current_holder != token
            or current_expire_at is None
            or current_expire_at < now
        ):
            return False

        return await self._vacate_lease(lease)

    async def locked(self, *, name: str) -> bool:
        """Check if the lock is acquired."""
        if not self._client:
            raise OutOfContextError(self, "locked")

        lease_name = _sanitize_lease_name(f"{self._prefix}{name}")

        try:
            lease = await self._client.get(
                Lease, name=lease_name, namespace=self._namespace
            )
        except ApiError as e:
            if e.status.code == HTTPStatus.NOT_FOUND:
                return False
            raise

        expire_at = _get_expire_at(lease)
        return expire_at is not None and expire_at >= datetime.now(tz=UTC)

    async def owned(self, *, name: str, token: str) -> bool:
        """Check if the lock is owned."""
        if not self._client:
            raise OutOfContextError(self, "owned")

        lease_name = _sanitize_lease_name(f"{self._prefix}{name}")

        try:
            lease = await self._client.get(
                Lease, name=lease_name, namespace=self._namespace
            )
        except ApiError as e:
            if e.status.code == HTTPStatus.NOT_FOUND:
                return False
            raise

        expire_at = _get_expire_at(lease)
        current_holder = lease.spec.holderIdentity if lease.spec else None
        return (
            current_holder == token
            and expire_at is not None
            and expire_at >= datetime.now(tz=UTC)
        )

    async def _create_lease(
        self,
        lease_name: str,
        token: str,
        duration: float,
    ) -> int | None:
        """Create a new Lease resource, returning fencing token `1`."""
        assert self._client  # noqa: S101

        now_dt = datetime.now(tz=UTC)
        fence = 1
        lease = Lease(
            metadata=ObjectMeta(
                name=lease_name,
                namespace=self._namespace,
                labels={
                    _LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE,
                },
            ),
            spec=LeaseSpec(
                holderIdentity=token,
                leaseDurationSeconds=ceil(duration),
                acquireTime=now_dt,
                renewTime=now_dt,
                leaseTransitions=fence,
            ),
        )

        try:
            await self._client.create(lease)
        except ApiError as e:
            if e.status.code == HTTPStatus.CONFLICT:
                return None
            raise

        return fence

    async def _replace_lease(
        self,
        existing_lease: Lease,
        token: str,
        duration: float,
        *,
        live: bool,
        holder: str | None,
    ) -> int | None:
        """Replace an existing Lease using optimistic concurrency.

        A live same-holder extend keeps `leaseTransitions`. A takeover of a
        vacated or expired Lease bumps it by one. The new value is the fencing
        token, written back under the read `resourceVersion`.
        """
        assert self._client  # noqa: S101
        assert existing_lease.metadata  # noqa: S101

        now_dt = datetime.now(tz=UTC)
        current_transitions = (
            existing_lease.spec.leaseTransitions
            if existing_lease.spec and existing_lease.spec.leaseTransitions
            else 0
        )
        fence = (
            current_transitions
            if live and holder == token
            else current_transitions + 1
        )

        updated_lease = Lease(
            metadata=ObjectMeta(
                name=existing_lease.metadata.name,
                namespace=self._namespace,
                resourceVersion=existing_lease.metadata.resourceVersion,
                labels={
                    _LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE,
                },
            ),
            spec=LeaseSpec(
                holderIdentity=token,
                leaseDurationSeconds=ceil(duration),
                acquireTime=now_dt,
                renewTime=now_dt,
                leaseTransitions=fence,
            ),
        )

        try:
            await self._client.replace(updated_lease)
        except ApiError as e:
            if e.status.code == HTTPStatus.CONFLICT:
                return None
            raise

        return fence

    async def _vacate_lease(self, existing_lease: Lease) -> bool:
        """Clear the holder of a Lease in place, keeping its transitions.

        Writes back under the read `resourceVersion`. Returns False on a 409
        Conflict (a concurrent writer changed the Lease first) so the caller
        sees the release as not applied.
        """
        assert self._client  # noqa: S101
        assert existing_lease.metadata  # noqa: S101

        transitions = (
            existing_lease.spec.leaseTransitions
            if existing_lease.spec and existing_lease.spec.leaseTransitions
            else 0
        )
        vacated = Lease(
            metadata=ObjectMeta(
                name=existing_lease.metadata.name,
                namespace=self._namespace,
                resourceVersion=existing_lease.metadata.resourceVersion,
                labels={
                    _LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE,
                },
            ),
            spec=LeaseSpec(
                holderIdentity=None,
                leaseDurationSeconds=None,
                acquireTime=None,
                renewTime=None,
                leaseTransitions=transitions,
            ),
        )

        try:
            await self._client.replace(vacated)
        except ApiError as e:
            if e.status.code in (HTTPStatus.NOT_FOUND, HTTPStatus.CONFLICT):
                return False
            raise

        return True


@dataclass(frozen=True, slots=True)
class _WriterSpec:
    """The Lease spec fields that describe the writer."""

    holder: str
    duration_seconds: int | None
    acquire_time: datetime | None
    renew_time: datetime | None


_READERS_ANNOTATION = f"{_METADATA_ANNOTATION_PREFIX}readers"
_INTENTS_ANNOTATION = f"{_METADATA_ANNOTATION_PREFIX}intents"
_CONFLICT_RETRIES = 5


class KubernetesReadWriteLockAdapter(ReadWriteLockBackend):
    """Kubernetes Read-Write Lock Adapter.

    Holds each lock in a `coordination.k8s.io/v1` Lease object, one per lock.
    The writer sits in the Lease spec, the same place a plain lock sits:
    `holderIdentity` carries the token, `renewTime` and
    `leaseDurationSeconds` carry the lease, and `leaseTransitions` is the
    generation. Reader leases and writer intents live in two annotations, a
    JSON map of token to expiry epoch each.

    Atomicity comes from Kubernetes optimistic concurrency. Every operation
    reads the Lease with its `resourceVersion` and writes it back with the
    same one, so a concurrent writer loses with a 409 Conflict and the call
    retries. A refused acquire that needs no write costs one read.

    This backend is coarse-grained on purpose. Every reader acquire and
    renewal is a full object write against the API server and etcd, and the
    annotation size limit caps the reader set in the hundreds. Use it for a
    Kubernetes-native deployment with few, long-lived readers. Reach for
    Redis or PostgreSQL for a hot read path.
    """

    scope: ClassVar[BackendScope] = "cluster"
    """State is shared by every process that connects to it."""

    def __init__(
        self,
        namespace: Annotated[
            str | None,
            Doc("""
                The Kubernetes namespace.

                If not provided, the namespace will be taken from the
                environment variable KUBE_NAMESPACE.
                """),
        ] = None,
        *,
        prefix: Annotated[
            str,
            Doc("""
                Prefix prepended to lease names to avoid conflicts with other
                applications in the same namespace.

                By default no prefix is added.
                """),
        ] = "",
        kubeconfig: Annotated[
            str | None,
            Doc("Path to the kubeconfig file."),
        ] = None,
    ) -> None:
        """Initialize the read-write lock backend."""
        self._namespace = namespace or _get_kube_namespace()
        self._prefix = prefix
        self._kubeconfig = kubeconfig
        self._client: AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self) -> Self:
        """Open the read-write lock backend."""
        self._loop = asyncio.get_running_loop()
        config = (
            KubeConfig.from_file(self._kubeconfig) if self._kubeconfig else None
        )
        self._client = AsyncClient(config=config)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the read-write lock backend."""
        if self._client:  # pragma: no branch
            await self._client.close()
            self._client = None

    def _require_client(self, operation: str) -> AsyncClient:
        """Return the open client.

        Raises:
            OutOfContextError: The backend is not open.
        """
        if not self._client:
            raise OutOfContextError(self, operation)
        return self._client

    def _lease_name(self, name: str) -> str:
        """Return the Lease name for a lock name."""
        return _sanitize_lease_name(f"{self._prefix}{name}")

    async def _read(self, lease_name: str) -> Lease | None:
        """Return the Lease, or `None` when it does not exist yet."""
        client = self._require_client("read")
        try:
            return await client.get(
                Lease, name=lease_name, namespace=self._namespace
            )
        except ApiError as e:
            if e.status.code == HTTPStatus.NOT_FOUND:
                return None
            raise

    @staticmethod
    def _live_holders(
        lease: Lease, annotation: str, now: datetime
    ) -> dict[str, float]:
        """Read one holder map from an annotation, without expired entries."""
        annotations = (
            (lease.metadata.annotations or {}) if lease.metadata else {}
        )
        raw = annotations.get(annotation)
        if not raw:
            return {}
        stored: Mapping[str, float] = json.loads(raw)
        cutoff = now.timestamp()
        return {
            token: expire_at
            for token, expire_at in stored.items()
            if expire_at > cutoff
        }

    async def _write(
        self,
        lease_name: str,
        lease: Lease | None,
        *,
        writer: _WriterSpec | None,
        transitions: int,
        readers: dict[str, float],
        intents: dict[str, float],
    ) -> bool:
        """Write the Lease back, returning `False` on a conflict.

        Every operation writes the whole Lease, so an operation that only
        touches readers or intents passes the stored writer back verbatim.
        Rebuilding it would push a live writer's lease out, and dropping an
        expired one would lose the poison the next writer needs to see.
        """
        client = self._require_client("write")
        annotations = {
            _READERS_ANNOTATION: json.dumps(readers),
            _INTENTS_ANNOTATION: json.dumps(intents),
        }
        metadata = ObjectMeta(
            name=lease_name,
            namespace=self._namespace,
            labels={_LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE},
            annotations=annotations,
        )
        spec = LeaseSpec(
            holderIdentity=writer.holder if writer else None,
            leaseDurationSeconds=writer.duration_seconds if writer else None,
            acquireTime=writer.acquire_time if writer else None,
            renewTime=writer.renew_time if writer else None,
            leaseTransitions=transitions,
        )
        try:
            if lease is None:
                await client.create(Lease(metadata=metadata, spec=spec))
            else:
                assert lease.metadata  # noqa: S101
                metadata.resourceVersion = lease.metadata.resourceVersion
                await client.replace(Lease(metadata=metadata, spec=spec))
        except ApiError as e:
            if e.status.code in (HTTPStatus.CONFLICT, HTTPStatus.NOT_FOUND):
                return False
            raise
        return True

    @staticmethod
    def _stored_writer(lease: Lease | None) -> _WriterSpec | None:
        """Return the writer fields exactly as stored, expired or not."""
        if (
            lease is None
            or lease.spec is None
            or lease.spec.holderIdentity is None
        ):
            return None
        return _WriterSpec(
            holder=lease.spec.holderIdentity,
            duration_seconds=lease.spec.leaseDurationSeconds,
            acquire_time=lease.spec.acquireTime,
            renew_time=lease.spec.renewTime,
        )

    @staticmethod
    def _writer(lease: Lease | None, now: datetime) -> tuple[str | None, bool]:
        """Return the stored writer token and whether its lease is live."""
        if lease is None or lease.spec is None:
            return None, False
        holder = lease.spec.holderIdentity
        expire_at = _get_expire_at(lease)
        return holder, holder is not None and expire_at is not None and (
            expire_at > now
        )

    @staticmethod
    def _generation(lease: Lease | None) -> int:
        """Return the generation counter stored on the Lease."""
        if lease is None or lease.spec is None:
            return 0
        return lease.spec.leaseTransitions or 0

    async def acquire_read(
        self, *, name: str, token: str, duration: float
    ) -> int | None:
        """Acquire a read lease, returning the generation or `None`."""
        lease_name = self._lease_name(name)
        for _ in range(_CONFLICT_RETRIES):
            lease = await self._read(lease_name)
            now = datetime.now(tz=UTC)
            readers = (
                self._live_holders(lease, _READERS_ANNOTATION, now)
                if lease
                else {}
            )
            intents = (
                self._live_holders(lease, _INTENTS_ANNOTATION, now)
                if lease
                else {}
            )
            _holder, writing = self._writer(lease, now)
            generation = self._generation(lease)
            if token not in readers and (writing or intents):
                return None
            readers[token] = (now + timedelta(seconds=duration)).timestamp()
            if await self._write(
                lease_name,
                lease,
                writer=self._stored_writer(lease),
                transitions=generation,
                readers=readers,
                intents=intents,
            ):
                return generation
        return None

    async def acquire_write(
        self, *, name: str, token: str, duration: float, intent: bool = True
    ) -> WriteGrant | None:
        """Acquire the write lease, returning the grant or `None`."""
        lease_name = self._lease_name(name)
        for _ in range(_CONFLICT_RETRIES):
            lease = await self._read(lease_name)
            now = datetime.now(tz=UTC)
            readers = (
                self._live_holders(lease, _READERS_ANNOTATION, now)
                if lease
                else {}
            )
            intents = (
                self._live_holders(lease, _INTENTS_ANNOTATION, now)
                if lease
                else {}
            )
            holder, writing = self._writer(lease, now)
            generation = self._generation(lease)

            if writing and holder == token:
                acquired = (
                    lease.spec.acquireTime
                    if lease and lease.spec and lease.spec.acquireTime
                    else now
                )
                if await self._write(
                    lease_name,
                    lease,
                    writer=_WriterSpec(
                        holder=token,
                        duration_seconds=ceil(duration),
                        acquire_time=acquired,
                        renew_time=now,
                    ),
                    transitions=generation,
                    readers=readers,
                    intents=intents,
                ):
                    return WriteGrant(fencing_token=generation, poisoned=False)
                continue

            if writing or readers:
                if not intent:
                    return None
                intents[token] = (now + timedelta(seconds=duration)).timestamp()
                if await self._write(
                    lease_name,
                    lease,
                    writer=self._stored_writer(lease),
                    transitions=generation,
                    readers=readers,
                    intents=intents,
                ):
                    return None
                continue

            intents.pop(token, None)
            if await self._write(
                lease_name,
                lease,
                writer=_WriterSpec(
                    holder=token,
                    duration_seconds=ceil(duration),
                    acquire_time=now,
                    renew_time=now,
                ),
                transitions=generation + 1,
                readers=readers,
                intents=intents,
            ):
                return WriteGrant(
                    fencing_token=generation + 1, poisoned=holder is not None
                )
        return None

    async def _drop_holder(
        self, name: str, token: str, annotation: str
    ) -> bool:
        """Drop `token` from one holder map, returning whether it was there."""
        lease_name = self._lease_name(name)
        for _ in range(_CONFLICT_RETRIES):
            lease = await self._read(lease_name)
            if lease is None:
                return False
            now = datetime.now(tz=UTC)
            readers = self._live_holders(lease, _READERS_ANNOTATION, now)
            intents = self._live_holders(lease, _INTENTS_ANNOTATION, now)
            target = readers if annotation == _READERS_ANNOTATION else intents
            if token not in target:
                return False
            del target[token]
            if await self._write(
                lease_name,
                lease,
                writer=self._stored_writer(lease),
                transitions=self._generation(lease),
                readers=readers,
                intents=intents,
            ):
                return True
        return False

    async def release_read(self, *, name: str, token: str) -> bool:
        """Drop a read lease."""
        return await self._drop_holder(name, token, _READERS_ANNOTATION)

    async def cancel_intent(self, *, name: str, token: str) -> bool:
        """Withdraw a writer intent."""
        return await self._drop_holder(name, token, _INTENTS_ANNOTATION)

    async def release_write(self, *, name: str, token: str) -> bool:
        """Drop the write lease, leaving the Lease object and its counter."""
        lease_name = self._lease_name(name)
        for _ in range(_CONFLICT_RETRIES):
            lease = await self._read(lease_name)
            if lease is None:
                return False
            now = datetime.now(tz=UTC)
            holder, writing = self._writer(lease, now)
            if not writing or holder != token:
                return False
            if await self._write(
                lease_name,
                lease,
                writer=None,
                transitions=self._generation(lease),
                readers=self._live_holders(lease, _READERS_ANNOTATION, now),
                intents=self._live_holders(lease, _INTENTS_ANNOTATION, now),
            ):
                return True
        return False

    async def downgrade(
        self, *, name: str, token: str, duration: float
    ) -> int | None:
        """Turn a held write lease into a read lease."""
        lease_name = self._lease_name(name)
        for _ in range(_CONFLICT_RETRIES):
            lease = await self._read(lease_name)
            if lease is None:
                return None
            now = datetime.now(tz=UTC)
            holder, writing = self._writer(lease, now)
            if not writing or holder != token:
                return None
            readers = self._live_holders(lease, _READERS_ANNOTATION, now)
            readers[token] = (now + timedelta(seconds=duration)).timestamp()
            generation = self._generation(lease)
            if await self._write(
                lease_name,
                lease,
                writer=None,
                transitions=generation,
                readers=readers,
                intents=self._live_holders(lease, _INTENTS_ANNOTATION, now),
            ):
                return generation
        return None

    async def state(self, *, name: str) -> ReadWriteLockState:
        """Return a point-in-time view of the lock."""
        lease = await self._read(self._lease_name(name))
        now = datetime.now(tz=UTC)
        if lease is None:
            return ReadWriteLockState(
                generation=0, writing=False, readers=0, waiting_writers=0
            )
        _holder, writing = self._writer(lease, now)
        return ReadWriteLockState(
            generation=self._generation(lease),
            writing=writing,
            readers=len(self._live_holders(lease, _READERS_ANNOTATION, now)),
            waiting_writers=len(
                self._live_holders(lease, _INTENTS_ANNOTATION, now)
            ),
        )

    async def owned_read(self, *, name: str, token: str) -> bool:
        """Check whether `token` holds a live read lease."""
        lease = await self._read(self._lease_name(name))
        if lease is None:
            return False
        now = datetime.now(tz=UTC)
        return token in self._live_holders(lease, _READERS_ANNOTATION, now)

    async def owned_write(self, *, name: str, token: str) -> bool:
        """Check whether `token` holds the live write lease."""
        lease = await self._read(self._lease_name(name))
        if lease is None:
            return False
        holder, writing = self._writer(lease, datetime.now(tz=UTC))
        return writing and holder == token


def _get_expire_at(lease: Lease) -> datetime | None:
    """Get the expire_at timestamp from Lease spec fields."""
    if (
        lease.spec
        and lease.spec.renewTime
        and lease.spec.leaseDurationSeconds is not None
    ):
        return lease.spec.renewTime + timedelta(
            seconds=lease.spec.leaseDurationSeconds
        )
    return None


def _lease_to_record(lease: Lease) -> LeaderRecord | None:
    """Map a Kubernetes Lease to a `LeaderRecord`.

    Returns `None` when the Lease lacks the spec fields required to describe a
    holder, so a half-written Lease never produces a partial record.
    """
    spec = lease.spec
    if (
        spec is None
        or spec.holderIdentity is None
        or spec.leaseDurationSeconds is None
        or spec.acquireTime is None
        or spec.renewTime is None
    ):
        return None
    annotations = lease.metadata.annotations if lease.metadata else None
    return LeaderRecord(
        holder=spec.holderIdentity,
        lease_duration=float(spec.leaseDurationSeconds),
        acquired_at=spec.acquireTime,
        renewed_at=spec.renewTime,
        transitions=spec.leaseTransitions or 0,
        metadata=_annotations_to_metadata(annotations),
    )


def _is_live(record: LeaderRecord, now: datetime) -> bool:
    """Return whether the record's lease is still valid at `now`."""
    expires_at = record.renewed_at + timedelta(seconds=record.lease_duration)
    return now < expires_at


class KubernetesLeaderElectionAdapter:
    """Kubernetes Leader Election Adapter.

    Stores the `LeaderRecord` in a `coordination.k8s.io/v1` Lease object, one
    per election. The Lease spec carries the holder, durations, and transition
    count, and the free-form metadata is stored under namespaced annotations.
    Atomicity comes from Kubernetes optimistic concurrency: a Lease is read
    with its `resourceVersion` and written back with it, so a concurrent writer
    loses the race with a 409 Conflict.
    """

    scope: ClassVar[BackendScope] = "cluster"
    """State is shared by every process that connects to it."""

    def __init__(
        self,
        namespace: Annotated[
            str | None,
            Doc("""
                The Kubernetes namespace.

                If not provided, the namespace will be taken from the
                environment variable KUBE_NAMESPACE.
                """),
        ] = None,
        *,
        prefix: Annotated[
            str,
            Doc("""
                Prefix prepended to lease names to avoid conflicts
                with other applications in the same namespace.

                By default no prefix is added.
                """),
        ] = "",
        kubeconfig: Annotated[
            str | None,
            Doc("Path to the kubeconfig file."),
        ] = None,
    ) -> None:
        """Initialize the leader election backend."""
        self._namespace = namespace or _get_kube_namespace()
        self._prefix = prefix
        self._kubeconfig = kubeconfig
        self._client: AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self) -> Self:
        """Open the leader election backend."""
        self._loop = asyncio.get_running_loop()
        config = (
            KubeConfig.from_file(self._kubeconfig) if self._kubeconfig else None
        )
        self._client = AsyncClient(config=config)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the leader election backend."""
        if self._client:  # pragma: no branch
            await self._client.close()
            self._client = None

    async def acquire_or_renew(
        self,
        *,
        name: str,
        token: str,
        duration: float,
        metadata: Mapping[str, str] | None = None,
    ) -> LeaderRecord:
        """Acquire or renew the lease, returning the resulting record."""
        if not self._client:
            raise OutOfContextError(self, "acquire_or_renew")

        lease_name = _sanitize_lease_name(f"{self._prefix}{name}")
        meta = dict(metadata or {})

        try:
            lease = await self._client.get(
                Lease, name=lease_name, namespace=self._namespace
            )
        except ApiError as e:
            if e.status.code != HTTPStatus.NOT_FOUND:
                raise
            return await self._create(lease_name, token, duration, meta)

        return await self._replace(lease, token, duration, meta)

    async def release(self, *, name: str, token: str) -> bool:
        """Release the lease when held by `token`."""
        if not self._client:
            raise OutOfContextError(self, "release")

        lease_name = _sanitize_lease_name(f"{self._prefix}{name}")
        now = datetime.now(tz=UTC)

        try:
            lease = await self._client.get(
                Lease, name=lease_name, namespace=self._namespace
            )
        except ApiError as e:
            if e.status.code == HTTPStatus.NOT_FOUND:
                return False
            raise

        record = _lease_to_record(lease)
        if (
            record is None
            or record.holder != token
            or not _is_live(record, now)
        ):
            return False

        try:
            await self._client.delete(
                Lease, name=lease_name, namespace=self._namespace
            )
        except ApiError as e:
            if e.status.code == HTTPStatus.NOT_FOUND:
                return False
            raise

        return True

    async def get(self, *, name: str) -> LeaderRecord | None:
        """Return the current live record, or `None`."""
        if not self._client:
            raise OutOfContextError(self, "get")

        lease_name = _sanitize_lease_name(f"{self._prefix}{name}")

        try:
            lease = await self._client.get(
                Lease, name=lease_name, namespace=self._namespace
            )
        except ApiError as e:
            if e.status.code == HTTPStatus.NOT_FOUND:
                return None
            raise

        record = _lease_to_record(lease)
        if record is None or not _is_live(record, datetime.now(tz=UTC)):
            return None
        return record

    async def _create(
        self,
        lease_name: str,
        token: str,
        duration: float,
        metadata: dict[str, str],
    ) -> LeaderRecord:
        """Create a new Lease for a fresh election.

        On an AlreadyExists race another writer won, so the Lease is re-read
        and its current record is returned.
        """
        assert self._client  # noqa: S101

        now = datetime.now(tz=UTC)
        record = LeaderRecord(
            holder=token,
            lease_duration=float(ceil(duration)),
            acquired_at=now,
            renewed_at=now,
            transitions=0,
            metadata=metadata,
        )
        lease = Lease(
            metadata=ObjectMeta(
                name=lease_name,
                namespace=self._namespace,
                labels={_LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE},
                annotations=_metadata_to_annotations(metadata),
            ),
            spec=LeaseSpec(
                holderIdentity=record.holder,
                leaseDurationSeconds=ceil(duration),
                acquireTime=now,
                renewTime=now,
                leaseTransitions=0,
            ),
        )

        try:
            await self._client.create(lease)
        except ApiError as e:
            if e.status.code == HTTPStatus.CONFLICT:
                return await self._reread(lease_name)
            raise

        return record

    async def _replace(
        self,
        lease: Lease,
        token: str,
        duration: float,
        metadata: dict[str, str],
    ) -> LeaderRecord:
        """Compute the next state of an existing Lease and write it back.

        A live lease held by another token is returned unchanged. Otherwise the
        record is renewed (same holder) or taken over (different or expired
        holder, bumping transitions), then written with the read
        `resourceVersion`. On a 409 Conflict another writer won, so the Lease
        is re-read and its current record is returned.
        """
        assert self._client  # noqa: S101
        assert lease.metadata  # noqa: S101

        now = datetime.now(tz=UTC)
        current = _lease_to_record(lease)

        if current is not None and _is_live(current, now):
            if current.holder != token:
                return current
            acquired_at = current.acquired_at
            transitions = current.transitions
        else:
            acquired_at = now
            if current is not None and current.holder != token:
                transitions = current.transitions + 1
            else:
                transitions = current.transitions if current else 0

        record = LeaderRecord(
            holder=token,
            lease_duration=float(ceil(duration)),
            acquired_at=acquired_at,
            renewed_at=now,
            transitions=transitions,
            metadata=metadata,
        )
        updated = Lease(
            metadata=ObjectMeta(
                name=lease.metadata.name,
                namespace=self._namespace,
                resourceVersion=lease.metadata.resourceVersion,
                labels={_LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE},
                annotations=_metadata_to_annotations(metadata),
            ),
            spec=LeaseSpec(
                holderIdentity=record.holder,
                leaseDurationSeconds=ceil(duration),
                acquireTime=record.acquired_at,
                renewTime=record.renewed_at,
                leaseTransitions=record.transitions,
            ),
        )

        try:
            await self._client.replace(updated)
        except ApiError as e:
            if e.status.code == HTTPStatus.CONFLICT:
                return await self._reread(lease.metadata.name or "")
            raise

        return record

    async def _reread(self, lease_name: str) -> LeaderRecord:
        """Re-read the Lease after losing a write race, returning its record.

        Falls back to an empty placeholder record only when the Lease has since
        vanished or carries no holder, so the caller never sees itself as
        leader after losing the race.
        """
        assert self._client  # noqa: S101

        try:
            lease = await self._client.get(
                Lease, name=lease_name, namespace=self._namespace
            )
        except ApiError as e:
            if e.status.code != HTTPStatus.NOT_FOUND:
                raise
            return _empty_record()

        record = _lease_to_record(lease)
        return record if record is not None else _empty_record()


def _empty_record() -> LeaderRecord:
    """Build a placeholder record with no holder for lost-race fallbacks."""
    now = datetime.now(tz=UTC)
    return LeaderRecord(
        holder="",
        lease_duration=0.0,
        acquired_at=now,
        renewed_at=now,
        transitions=0,
        metadata={},
    )
