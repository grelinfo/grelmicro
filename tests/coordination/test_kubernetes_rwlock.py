"""Tests for the Kubernetes read-write lock adapter, mocked.

The conformance suite runs this adapter against a real API server. These
tests cover what a live server will not produce on demand: a conflicting
writer on every retry, a Lease that vanishes mid-operation, and an API
error that is not a conflict.
"""

import json
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from unittest.mock import AsyncMock

import pytest
from lightkube.core.exceptions import ApiError
from lightkube.models.coordination_v1 import LeaseSpec
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.coordination_v1 import Lease

from grelmicro.coordination.kubernetes import (
    _INTENTS_ANNOTATION,
    _READERS_ANNOTATION,
    KubernetesReadWriteLockAdapter,
)
from grelmicro.errors import OutOfContextError

pytestmark = [pytest.mark.timeout(10)]

TOKEN = "test-token"
OTHER = "other-token"


def _api_error(code: int) -> ApiError:
    """Build an `ApiError` carrying `code`."""
    return ApiError(
        status={"code": code, "message": "error", "status": "Failure"}
    )


def _lease(
    *,
    holder: str | None = None,
    expired: bool = False,
    readers: dict[str, float] | None = None,
    intents: dict[str, float] | None = None,
    transitions: int = 0,
) -> Lease:
    """Build a Lease in the shape the adapter writes."""
    now = datetime.now(tz=UTC)
    renew_time = now - timedelta(seconds=10) if expired else now
    return Lease(
        metadata=ObjectMeta(
            name="catalog",
            namespace="default",
            resourceVersion="1",
            annotations={
                _READERS_ANNOTATION: json.dumps(readers or {}),
                _INTENTS_ANNOTATION: json.dumps(intents or {}),
            },
        ),
        spec=LeaseSpec(
            holderIdentity=holder,
            leaseDurationSeconds=1 if holder else None,
            acquireTime=renew_time if holder else None,
            renewTime=renew_time if holder else None,
            leaseTransitions=transitions,
        ),
    )


def _adapter(**client_attrs: AsyncMock) -> KubernetesReadWriteLockAdapter:
    """Build an adapter with a mocked client."""
    adapter = KubernetesReadWriteLockAdapter(namespace="default")
    client = AsyncMock()
    for attr, mock in client_attrs.items():
        setattr(client, attr, mock)
    adapter._client = client
    return adapter


def _live(seconds: float = 60) -> float:
    """Return an expiry epoch that is still in the future."""
    return (datetime.now(tz=UTC) + timedelta(seconds=seconds)).timestamp()


async def test_out_of_context_errors() -> None:
    """Every operation refuses to run before the backend is open."""
    adapter = KubernetesReadWriteLockAdapter(namespace="default")

    with pytest.raises(OutOfContextError):
        await adapter.acquire_read(name="catalog", token=TOKEN, duration=1)
    with pytest.raises(OutOfContextError):
        await adapter.acquire_write(name="catalog", token=TOKEN, duration=1)
    with pytest.raises(OutOfContextError):
        await adapter.state(name="catalog")


async def test_aenter_and_aexit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backend opens a client and closes it on exit."""
    adapter = KubernetesReadWriteLockAdapter(namespace="default")
    client = AsyncMock()
    monkeypatch.setattr(
        "grelmicro.coordination.kubernetes.AsyncClient",
        lambda **_kwargs: client,
    )

    await adapter.__aenter__()
    assert adapter._client is client

    await adapter.__aexit__(None, None, None)
    client.close.assert_awaited_once()
    assert adapter._client is None


async def test_read_reraises_a_non_missing_error() -> None:
    """An API error that is not a 404 propagates."""
    adapter = _adapter(
        get=AsyncMock(side_effect=_api_error(HTTPStatus.INTERNAL_SERVER_ERROR))
    )

    with pytest.raises(ApiError):
        await adapter.state(name="catalog")


async def test_write_reraises_a_non_conflict_error() -> None:
    """An API error that is neither conflict nor missing propagates."""
    adapter = _adapter(
        get=AsyncMock(side_effect=_api_error(HTTPStatus.NOT_FOUND)),
        create=AsyncMock(
            side_effect=_api_error(HTTPStatus.INTERNAL_SERVER_ERROR)
        ),
    )

    with pytest.raises(ApiError):
        await adapter.acquire_read(name="catalog", token=TOKEN, duration=1)


async def test_acquire_read_gives_up_after_repeated_conflicts() -> None:
    """A reader that loses every write race reports a refusal."""
    adapter = _adapter(
        get=AsyncMock(return_value=_lease()),
        replace=AsyncMock(side_effect=_api_error(HTTPStatus.CONFLICT)),
    )

    granted = await adapter.acquire_read(
        name="catalog", token=TOKEN, duration=1
    )

    assert granted is None


async def test_acquire_write_gives_up_after_repeated_conflicts() -> None:
    """A writer that loses every write race reports a refusal."""
    adapter = _adapter(
        get=AsyncMock(return_value=_lease()),
        replace=AsyncMock(side_effect=_api_error(HTTPStatus.CONFLICT)),
    )

    granted = await adapter.acquire_write(
        name="catalog", token=TOKEN, duration=1
    )

    assert granted is None


async def test_write_renewal_retries_on_conflict() -> None:
    """The holder renewing under a conflict retries and then gives up."""
    adapter = _adapter(
        get=AsyncMock(return_value=_lease(holder=TOKEN)),
        replace=AsyncMock(side_effect=_api_error(HTTPStatus.CONFLICT)),
    )

    granted = await adapter.acquire_write(
        name="catalog", token=TOKEN, duration=1
    )

    assert granted is None


async def test_intent_write_retries_on_conflict() -> None:
    """Recording an intent under a conflict retries and then gives up."""
    adapter = _adapter(
        get=AsyncMock(return_value=_lease(readers={OTHER: _live()})),
        replace=AsyncMock(side_effect=_api_error(HTTPStatus.CONFLICT)),
    )

    granted = await adapter.acquire_write(
        name="catalog", token=TOKEN, duration=1
    )

    assert granted is None


async def test_nowait_writer_behind_a_reader_records_nothing() -> None:
    """A try that does not wait returns without writing at all."""
    replace = AsyncMock()
    adapter = _adapter(
        get=AsyncMock(return_value=_lease(readers={OTHER: _live()})),
        replace=replace,
    )

    granted = await adapter.acquire_write(
        name="catalog", token=TOKEN, duration=1, intent=False
    )

    assert granted is None
    replace.assert_not_awaited()


async def test_release_and_downgrade_on_a_missing_lease() -> None:
    """Nothing is held when the Lease does not exist."""
    adapter = _adapter(
        get=AsyncMock(side_effect=_api_error(HTTPStatus.NOT_FOUND))
    )

    assert not await adapter.release_read(name="catalog", token=TOKEN)
    assert not await adapter.release_write(name="catalog", token=TOKEN)
    assert not await adapter.cancel_intent(name="catalog", token=TOKEN)
    assert await adapter.downgrade(name="catalog", token=TOKEN, duration=1) is (
        None
    )
    assert not await adapter.owned_read(name="catalog", token=TOKEN)
    assert not await adapter.owned_write(name="catalog", token=TOKEN)
    state = await adapter.state(name="catalog")
    assert state.generation == 0


async def test_release_write_by_a_non_holder() -> None:
    """A token that does not hold the write lease releases nothing."""
    adapter = _adapter(get=AsyncMock(return_value=_lease(holder=OTHER)))

    assert not await adapter.release_write(name="catalog", token=TOKEN)
    assert await adapter.downgrade(name="catalog", token=TOKEN, duration=1) is (
        None
    )


async def test_release_read_by_a_non_holder() -> None:
    """A token with no reader lease releases nothing."""
    adapter = _adapter(get=AsyncMock(return_value=_lease()))

    assert not await adapter.release_read(name="catalog", token=TOKEN)


async def test_release_write_gives_up_after_repeated_conflicts() -> None:
    """A release that loses every write race reports failure."""
    adapter = _adapter(
        get=AsyncMock(return_value=_lease(holder=TOKEN)),
        replace=AsyncMock(side_effect=_api_error(HTTPStatus.CONFLICT)),
    )

    assert not await adapter.release_write(name="catalog", token=TOKEN)


async def test_release_read_gives_up_after_repeated_conflicts() -> None:
    """A reader release that loses every write race reports failure."""
    adapter = _adapter(
        get=AsyncMock(return_value=_lease(readers={TOKEN: _live()})),
        replace=AsyncMock(side_effect=_api_error(HTTPStatus.CONFLICT)),
    )

    assert not await adapter.release_read(name="catalog", token=TOKEN)


async def test_downgrade_gives_up_after_repeated_conflicts() -> None:
    """A downgrade that loses every write race reports failure."""
    adapter = _adapter(
        get=AsyncMock(return_value=_lease(holder=TOKEN)),
        replace=AsyncMock(side_effect=_api_error(HTTPStatus.CONFLICT)),
    )

    assert await adapter.downgrade(name="catalog", token=TOKEN, duration=1) is (
        None
    )


async def test_an_expired_writer_reads_as_free() -> None:
    """A Lease whose writer expired lets a reader in and poisons the next."""
    adapter = _adapter(
        get=AsyncMock(return_value=_lease(holder=OTHER, expired=True)),
        replace=AsyncMock(),
    )

    generation = await adapter.acquire_read(
        name="catalog", token=TOKEN, duration=1
    )
    grant = await adapter.acquire_write(name="catalog", token=TOKEN, duration=1)

    assert generation == 0
    assert grant is not None
    assert grant.poisoned


async def test_holders_without_annotations() -> None:
    """A Lease written by an older version carries no holder maps."""
    lease = Lease(
        metadata=ObjectMeta(
            name="catalog", namespace="default", resourceVersion="1"
        ),
        spec=LeaseSpec(leaseTransitions=3),
    )
    adapter = _adapter(get=AsyncMock(return_value=lease))

    state = await adapter.state(name="catalog")

    assert state.readers == 0
    assert state.waiting_writers == 0
    assert state.generation == 3  # noqa: PLR2004


async def test_a_reader_release_does_not_extend_the_writer() -> None:
    """Writing the Lease for a reader keeps the writer's own renewal."""
    lease = _lease(holder=OTHER, readers={TOKEN: _live()})
    assert lease.spec is not None
    stored_renew = lease.spec.renewTime
    replace = AsyncMock()
    adapter = _adapter(get=AsyncMock(return_value=lease), replace=replace)

    assert await adapter.release_read(name="catalog", token=TOKEN)

    call = replace.await_args
    assert call is not None
    written = call.args[0]
    assert written.spec.renewTime == stored_renew
    assert written.spec.holderIdentity == OTHER
