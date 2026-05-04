"""Kubernetes Synchronization Backend."""

import asyncio
import re
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

from grelmicro.errors import OutOfContextError
from grelmicro.sync.abc import SyncBackend
from grelmicro.sync.errors import SyncSettingsValidationError

_LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
_LABEL_MANAGED_BY_VALUE = "grelmicro"
_INVALID_CHARS = re.compile(r"[^a-z0-9-]")
_CONSECUTIVE_HYPHENS = re.compile(r"-{2,}")
_MAX_NAME_LENGTH = 253


class _KubernetesSettings(BaseSettings):
    """Kubernetes settings from the environment variables."""

    KUBE_NAMESPACE: str | None = None


def _get_kube_namespace() -> str:
    """Get the Kubernetes namespace from the environment variables.

    Raises:
        SyncSettingsValidationError: If KUBE_NAMESPACE is not set.
    """
    settings = _KubernetesSettings()

    if settings.KUBE_NAMESPACE:
        return settings.KUBE_NAMESPACE

    msg = "KUBE_NAMESPACE must be set"
    raise SyncSettingsValidationError(msg)


def _sanitize_lease_name(name: str) -> str:
    """Sanitize a lock name to be a valid Kubernetes resource name.

    RFC 1123: lowercase, alphanumeric and hyphens, max 253 chars,
    must start and end with alphanumeric.

    Raises:
        ValueError: If the name contains no valid characters.

    Examples:
        ``"lock:my-resource"`` → ``"lock-my-resource"``
        ``"UPPER_CASE"``       → ``"upper-case"``
    """
    sanitized = _INVALID_CHARS.sub("-", name.lower())
    sanitized = _CONSECUTIVE_HYPHENS.sub("-", sanitized)
    sanitized = sanitized[:_MAX_NAME_LENGTH].strip("-")
    if not sanitized:
        msg = f"Name produces an empty Kubernetes resource name: {name!r}"
        raise ValueError(msg)
    return sanitized


class KubernetesSyncBackend(SyncBackend):
    """Kubernetes Synchronization Backend."""

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
        if self._client:
            # Clean up expired leases managed by grelmicro
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
            # Lease does not exist, create it
            return await self._create_lease(lease_name, token, duration)

        # Lease exists - check if we can acquire
        current_expire_at = _get_expire_at(lease)
        current_holder = lease.spec.holderIdentity if lease.spec else None

        if (
            current_expire_at is not None
            and current_expire_at >= now
            and current_holder != token
        ):
            # Held by another token and not expired
            return False

        # Expired or same token -> replace
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
