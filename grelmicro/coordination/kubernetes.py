"""Kubernetes Coordination Adapters."""

import asyncio
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from math import ceil
from types import TracebackType
from typing import Annotated, Self

from lightkube import AsyncClient, KubeConfig
from lightkube.core.exceptions import ApiError
from lightkube.models.coordination_v1 import LeaseSpec
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.coordination_v1 import Lease
from pydantic_settings import BaseSettings
from typing_extensions import Doc

from grelmicro.coordination.abc import LeaderRecord, LockBackend
from grelmicro.errors import OutOfContextError, SettingsValidationError

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
        SettingsValidationError: If KUBE_NAMESPACE is not set.
    """
    settings = _KubernetesSettings()

    if settings.KUBE_NAMESPACE:
        return settings.KUBE_NAMESPACE

    msg = "KUBE_NAMESPACE must be set"
    raise SettingsValidationError(msg)


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
    """

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
        """Close the lock backend."""
        if self._client:  # pragma: no branch
            now = datetime.now(tz=UTC)
            async for lease in self._client.list(
                Lease,
                namespace=self._namespace,
                labels={_LABEL_MANAGED_BY: _LABEL_MANAGED_BY_VALUE},
            ):
                expire_at = _get_expire_at(lease)
                if expire_at is not None and expire_at < now:
                    assert lease.metadata  # noqa: S101
                    assert lease.metadata.name  # noqa: S101
                    try:
                        await self._client.delete(
                            Lease,
                            name=lease.metadata.name,
                            namespace=self._namespace,
                        )
                    except ApiError as e:
                        if e.status.code != HTTPStatus.NOT_FOUND:
                            raise
            await self._client.close()
            self._client = None

    async def acquire(self, *, name: str, token: str, duration: float) -> bool:
        """Acquire a lock."""
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

        if (
            current_expire_at is not None
            and current_expire_at >= now
            and current_holder != token
        ):
            return False

        return await self._replace_lease(lease, token, duration)

    async def release(self, *, name: str, token: str) -> bool:
        """Release the lock."""
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

        try:
            await self._client.delete(
                Lease, name=lease_name, namespace=self._namespace
            )
        except ApiError as e:
            if e.status.code == HTTPStatus.NOT_FOUND:
                return False
            raise

        return True

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
    ) -> bool:
        """Create a new Lease resource."""
        assert self._client  # noqa: S101

        now_dt = datetime.now(tz=UTC)
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
            ),
        )

        try:
            await self._client.create(lease)
        except ApiError as e:
            if e.status.code == HTTPStatus.CONFLICT:
                return False
            raise

        return True

    async def _replace_lease(
        self,
        existing_lease: Lease,
        token: str,
        duration: float,
    ) -> bool:
        """Replace an existing Lease resource using optimistic concurrency."""
        assert self._client  # noqa: S101
        assert existing_lease.metadata  # noqa: S101

        now_dt = datetime.now(tz=UTC)
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
            ),
        )

        try:
            await self._client.replace(updated_lease)
        except ApiError as e:
            if e.status.code == HTTPStatus.CONFLICT:
                return False
            raise

        return True


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


class KubernetesLeaderElectionBackend:
    """Kubernetes Leader Election Backend.

    Stores the `LeaderRecord` in a `coordination.k8s.io/v1` Lease object, one
    per election. The Lease spec carries the holder, durations, and transition
    count, and the free-form metadata is stored under namespaced annotations.
    Atomicity comes from Kubernetes optimistic concurrency: a Lease is read
    with its `resourceVersion` and written back with it, so a concurrent writer
    loses the race with a 409 Conflict.
    """

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
