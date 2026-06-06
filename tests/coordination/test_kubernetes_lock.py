"""Tests for Kubernetes Backend."""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from lightkube.core.exceptions import ApiError
from lightkube.models.coordination_v1 import LeaseSpec
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.coordination_v1 import Lease

from grelmicro.coordination.errors import CoordinationSettingsValidationError
from grelmicro.coordination.kubernetes import (
    _MAX_NAME_LENGTH,
    KubernetesLockAdapter,
    _get_expire_at,
    _sanitize_lease_name,
)
from grelmicro.errors import OutOfContextError

pytestmark = [pytest.mark.timeout(1)]

TOKEN = "test-token"


def _make_api_error(code: int) -> ApiError:
    """Create an ApiError with the given status code."""
    return ApiError(
        status={"code": code, "message": "error", "status": "Failure"}
    )


def _make_lease(
    name: str = "test",
    holder: str | None = TOKEN,
    *,
    expired: bool = False,
    resource_version: str = "1",
    transitions: int = 1,
) -> Lease:
    """Create a Lease for testing."""
    if expired:
        renew_time = datetime.min.replace(tzinfo=UTC)
        duration_seconds = 1
    else:
        renew_time = datetime.now(tz=UTC)
        duration_seconds = 999999
    return Lease(
        metadata=ObjectMeta(
            name=name,
            namespace="default",
            resourceVersion=resource_version,
        ),
        spec=LeaseSpec(
            holderIdentity=holder,
            leaseDurationSeconds=duration_seconds,
            renewTime=renew_time,
            leaseTransitions=transitions,
        ),
    )


def _make_mocked_backend(
    **client_overrides: AsyncMock,
) -> KubernetesLockAdapter:
    """Create a KubernetesLockAdapter with a mocked client."""
    backend = KubernetesLockAdapter(namespace="default")
    mock_client = AsyncMock()
    for attr, mock in client_overrides.items():
        setattr(mock_client, attr, mock)
    backend._client = mock_client
    return backend


async def _async_iter(items: list) -> AsyncIterator:
    """Create an async iterator from a list."""
    for item in items:
        yield item


# --- Out of context ---


async def test_sync_backend_out_of_context_errors() -> None:
    """Test Synchronization Backend Out Of Context Errors."""
    # Arrange
    backend = KubernetesLockAdapter(namespace="default")
    name = "lock"
    key = "token"

    # Act / Assert
    with pytest.raises(OutOfContextError):
        await backend.acquire(name=name, token=key, duration=1)
    with pytest.raises(OutOfContextError):
        await backend.release(name=name, token=key)
    with pytest.raises(OutOfContextError):
        await backend.locked(name=name)
    with pytest.raises(OutOfContextError):
        await backend.owned(name=name, token=key)


# --- Settings ---


def test_kubernetes_env_var_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test Kubernetes Settings from Environment Variables."""
    # Arrange
    monkeypatch.setenv("KUBE_NAMESPACE", "my-namespace")

    # Act
    backend = KubernetesLockAdapter()

    # Assert
    assert backend._namespace == "my-namespace"


def test_kubernetes_env_var_settings_validation_error() -> None:
    """Test Kubernetes Settings Validation Error."""
    # Assert / Act
    with pytest.raises(
        CoordinationSettingsValidationError,
        match=(r"Could not validate settings:\n"),
    ):
        KubernetesLockAdapter()


def test_sync_backend_prefix() -> None:
    """Test Synchronization Backend Prefix."""
    # Act
    backend = KubernetesLockAdapter(namespace="default", prefix="myapp-")

    # Assert
    assert backend._prefix == "myapp-"


# --- Name sanitization ---


@pytest.mark.parametrize(
    ("input_name", "expected"),
    [
        ("simple", "simple"),
        ("lock:my-resource", "lock-my-resource"),
        ("UPPER_CASE", "upper-case"),
        ("with spaces", "with-spaces"),
        ("special!@#chars", "special-chars"),
        ("---leading-trailing---", "leading-trailing"),
        ("multiple---hyphens", "multiple-hyphens"),
        ("a" * 300, "a" * _MAX_NAME_LENGTH),
        ("a" * 252 + "!b", "a" * 252),  # truncation strips trailing hyphen
    ],
)
def test_sanitize_lease_name(input_name: str, expected: str) -> None:
    """Test lease name sanitization."""
    assert _sanitize_lease_name(input_name) == expected


def test_sanitize_lease_name_empty() -> None:
    """Test lease name sanitization raises on empty result."""
    with pytest.raises(ValueError, match="empty Kubernetes resource name"):
        _sanitize_lease_name("!!!")


# --- _get_expire_at ---


@pytest.mark.parametrize(
    ("lease", "expected"),
    [
        (
            Lease(
                metadata=ObjectMeta(name="test"),
                spec=LeaseSpec(leaseDurationSeconds=1),
            ),
            None,
        ),
        (
            Lease(
                metadata=ObjectMeta(name="test"),
                spec=LeaseSpec(renewTime=datetime.now(tz=UTC)),
            ),
            None,
        ),
    ],
    ids=["missing-renewTime", "missing-leaseDurationSeconds"],
)
def test_get_expire_at_edge_cases(
    lease: Lease, expected: datetime | None
) -> None:
    """Test _get_expire_at edge cases."""
    assert _get_expire_at(lease) == expected


def test_get_expire_at_computes_correctly() -> None:
    """Test _get_expire_at computes renewTime + leaseDurationSeconds."""
    # Arrange
    renew_time = datetime(2024, 1, 1, tzinfo=UTC)
    lease = Lease(
        metadata=ObjectMeta(name="test"),
        spec=LeaseSpec(renewTime=renew_time, leaseDurationSeconds=60),
    )

    # Act
    result = _get_expire_at(lease)

    # Assert
    assert result == renew_time + timedelta(seconds=60)


# --- __aenter__ / __aexit__ ---


async def test_aenter_sets_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test __aenter__ creates an AsyncClient."""
    # Arrange
    backend = KubernetesLockAdapter(namespace="default")
    monkeypatch.setattr(
        "grelmicro.coordination.kubernetes.AsyncClient",
        lambda **_kwargs: AsyncMock(),
    )

    # Act / Assert
    assert backend._client is None
    await backend.__aenter__()
    assert backend._client is not None


# --- Happy path tests (mocked client) ---


async def test_acquire_creates_when_not_found() -> None:
    """Test acquire creates a new lease when none exists, fence starts at 1."""
    # Arrange
    create = AsyncMock()
    backend = _make_mocked_backend(
        get=AsyncMock(side_effect=_make_api_error(404)),
        create=create,
    )

    # Act
    result = await backend.acquire(name="lock", token=TOKEN, duration=1)

    # Assert
    assert result == 1
    assert create.await_args is not None
    created = create.await_args.args[0]
    assert created.spec.leaseTransitions == 1
    assert created.spec.holderIdentity == TOKEN


async def test_acquire_replaces_expired_lease_bumps_transitions() -> None:
    """Test acquire takes over an expired lease and bumps transitions."""
    # Arrange
    replace = AsyncMock()
    backend = _make_mocked_backend(
        get=AsyncMock(
            return_value=_make_lease(
                holder="other-token", expired=True, transitions=4
            )
        ),
        replace=replace,
    )

    # Act
    result = await backend.acquire(name="lock", token=TOKEN, duration=1)

    # Assert
    assert result == 5  # noqa: PLR2004
    assert replace.await_args is not None
    written = replace.await_args.args[0]
    assert written.spec.leaseTransitions == 5  # noqa: PLR2004
    assert written.spec.holderIdentity == TOKEN


async def test_acquire_extend_same_holder_keeps_transitions() -> None:
    """Test a same-holder live extend keeps the fencing token."""
    # Arrange
    replace = AsyncMock()
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=_make_lease(holder=TOKEN, transitions=3)),
        replace=replace,
    )

    # Act
    result = await backend.acquire(name="lock", token=TOKEN, duration=1)

    # Assert
    assert result == 3  # noqa: PLR2004
    assert replace.await_args is not None
    written = replace.await_args.args[0]
    assert written.spec.leaseTransitions == 3  # noqa: PLR2004


async def test_acquire_takeover_vacated_lease_bumps_transitions() -> None:
    """Test acquire takes over a vacated (released) lease and bumps the fence."""
    # Arrange
    vacated = Lease(
        metadata=ObjectMeta(
            name="lock", namespace="default", resourceVersion="9"
        ),
        spec=LeaseSpec(leaseTransitions=2),
    )
    replace = AsyncMock()
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=vacated),
        replace=replace,
    )

    # Act
    result = await backend.acquire(name="lock", token=TOKEN, duration=1)

    # Assert
    assert result == 3  # noqa: PLR2004
    assert replace.await_args is not None
    written = replace.await_args.args[0]
    assert written.spec.leaseTransitions == 3  # noqa: PLR2004
    assert written.spec.holderIdentity == TOKEN


async def test_acquire_returns_none_when_held_by_other() -> None:
    """Test acquire returns None when held by another token."""
    # Arrange
    other_token = "other-token"
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=_make_lease(holder=other_token)),
    )

    # Act
    result = await backend.acquire(name="lock", token=TOKEN, duration=1)

    # Assert
    assert result is None


async def test_release_vacates_in_place_keeping_transitions() -> None:
    """Test release clears the holder in place and keeps leaseTransitions."""
    # Arrange
    replace = AsyncMock()
    delete = AsyncMock()
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=_make_lease(holder=TOKEN, transitions=7)),
        replace=replace,
        delete=delete,
    )

    # Act
    result = await backend.release(name="test", token=TOKEN)

    # Assert
    assert result is True
    delete.assert_not_called()
    replace.assert_awaited_once()
    assert replace.await_args is not None
    written = replace.await_args.args[0]
    assert written.spec.holderIdentity is None
    assert written.spec.renewTime is None
    assert written.spec.acquireTime is None
    assert written.spec.leaseTransitions == 7  # noqa: PLR2004


async def test_release_not_found() -> None:
    """Test release returns False when lease not found."""
    # Arrange
    backend = _make_mocked_backend(
        get=AsyncMock(side_effect=_make_api_error(404)),
    )

    # Act
    result = await backend.release(name="test", token=TOKEN)

    # Assert
    assert result is False


async def test_release_wrong_token() -> None:
    """Test release returns False when token doesn't match."""
    # Arrange
    other_token = "other-token"
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=_make_lease(holder=other_token)),
    )

    # Act
    result = await backend.release(name="test", token=TOKEN)

    # Assert
    assert result is False


async def test_locked_not_found() -> None:
    """Test locked returns False when lease not found."""
    # Arrange
    backend = _make_mocked_backend(
        get=AsyncMock(side_effect=_make_api_error(404)),
    )

    # Act
    result = await backend.locked(name="lock")

    # Assert
    assert result is False


async def test_locked_active() -> None:
    """Test locked returns True when lease is active."""
    # Arrange
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=_make_lease()),
    )

    # Act
    result = await backend.locked(name="lock")

    # Assert
    assert result is True


async def test_owned_not_found() -> None:
    """Test owned returns False when lease not found."""
    # Arrange
    backend = _make_mocked_backend(
        get=AsyncMock(side_effect=_make_api_error(404)),
    )

    # Act
    result = await backend.owned(name="lock", token=TOKEN)

    # Assert
    assert result is False


async def test_owned_active() -> None:
    """Test owned returns True when lease is owned by token."""
    # Arrange
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=_make_lease(holder=TOKEN)),
    )

    # Act
    result = await backend.owned(name="lock", token=TOKEN)

    # Assert
    assert result is True


async def test_owned_wrong_token() -> None:
    """Test owned returns False when lease is held by another."""
    # Arrange
    other_token = "other-token"
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=_make_lease(holder=other_token)),
    )

    # Act
    result = await backend.owned(name="lock", token=TOKEN)

    # Assert
    assert result is False


# --- API error handling tests (mocked client) ---


@pytest.mark.parametrize(
    "method_caller",
    [
        lambda b: b.acquire(name="lock", token=TOKEN, duration=1),
        lambda b: b.release(name="lock", token=TOKEN),
        lambda b: b.locked(name="lock"),
        lambda b: b.owned(name="lock", token=TOKEN),
    ],
    ids=["acquire", "release", "locked", "owned"],
)
async def test_raises_on_non_404_get_error(
    method_caller: Callable[[KubernetesLockAdapter], Awaitable[bool]],
) -> None:
    """Test all methods re-raise non-404 API errors on GET."""
    # Arrange
    backend = _make_mocked_backend(
        get=AsyncMock(side_effect=_make_api_error(500)),
    )

    # Act / Assert
    with pytest.raises(ApiError):
        await method_caller(backend)


async def test_acquire_conflict_on_create() -> None:
    """Test acquire returns None on 409 Conflict during CREATE."""
    # Arrange
    backend = _make_mocked_backend(
        get=AsyncMock(side_effect=_make_api_error(404)),
        create=AsyncMock(side_effect=_make_api_error(409)),
    )

    # Act
    result = await backend.acquire(name="lock", token=TOKEN, duration=1)

    # Assert
    assert result is None


async def test_acquire_raises_on_create_non_409() -> None:
    """Test acquire re-raises non-409 API errors during CREATE."""
    # Arrange
    backend = _make_mocked_backend(
        get=AsyncMock(side_effect=_make_api_error(404)),
        create=AsyncMock(side_effect=_make_api_error(500)),
    )

    # Act / Assert
    with pytest.raises(ApiError):
        await backend.acquire(name="lock", token=TOKEN, duration=1)


async def test_acquire_conflict_on_replace() -> None:
    """Test acquire returns None on 409 Conflict during REPLACE."""
    # Arrange
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=_make_lease(expired=True)),
        replace=AsyncMock(side_effect=_make_api_error(409)),
    )

    # Act
    result = await backend.acquire(name="lock", token=TOKEN, duration=1)

    # Assert
    assert result is None


async def test_acquire_raises_on_replace_non_409() -> None:
    """Test acquire re-raises non-409 API errors during REPLACE."""
    # Arrange
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=_make_lease(expired=True)),
        replace=AsyncMock(side_effect=_make_api_error(500)),
    )

    # Act / Assert
    with pytest.raises(ApiError):
        await backend.acquire(name="lock", token=TOKEN, duration=1)


@pytest.mark.parametrize("code", [404, 409])
async def test_release_vacate_conflict_returns_false(code: int) -> None:
    """Test release returns False when the vacate write loses the race."""
    # Arrange
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=_make_lease(holder=TOKEN)),
        replace=AsyncMock(side_effect=_make_api_error(code)),
    )

    # Act
    result = await backend.release(name="test", token=TOKEN)

    # Assert
    assert result is False


async def test_release_raises_on_vacate_non_conflict() -> None:
    """Test release re-raises non-conflict API errors on the vacate write."""
    # Arrange
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=_make_lease(holder=TOKEN)),
        replace=AsyncMock(side_effect=_make_api_error(500)),
    )

    # Act / Assert
    with pytest.raises(ApiError):
        await backend.release(name="test", token=TOKEN)


# --- __aexit__ cleanup ---


async def test_aexit_vacates_expired_lease_keeping_transitions() -> None:
    """Cleanup vacates an expired lease in place and keeps its transitions."""
    # Arrange
    backend = KubernetesLockAdapter(namespace="default")
    expired_lease = _make_lease(holder=TOKEN, expired=True, transitions=6)
    replace = AsyncMock()
    delete = AsyncMock()
    mock_client = AsyncMock()
    mock_client.list = MagicMock(return_value=_async_iter([expired_lease]))
    mock_client.replace = replace
    mock_client.delete = delete
    mock_client.close = AsyncMock()
    backend._client = mock_client

    # Act
    await backend.__aexit__(None, None, None)

    # Assert
    delete.assert_not_called()
    replace.assert_awaited_once()
    assert replace.await_args is not None
    written = replace.await_args.args[0]
    assert written.spec.holderIdentity is None
    assert written.spec.leaseTransitions == 6  # noqa: PLR2004
    assert backend._client is None


@pytest.mark.parametrize(
    ("replace_side_effect", "should_raise"),
    [
        (_make_api_error(404), False),
        (_make_api_error(409), False),
        (_make_api_error(500), True),
    ],
    ids=["404-ignored", "409-ignored", "500-raises"],
)
async def test_aexit_cleanup_vacate_errors(
    replace_side_effect: ApiError,
    *,
    should_raise: bool,
) -> None:
    """Test __aexit__ error handling during the cleanup vacate write."""
    # Arrange
    backend = KubernetesLockAdapter(namespace="default")
    expired_lease = _make_lease(holder=TOKEN, expired=True)
    mock_client = AsyncMock()
    mock_client.list = MagicMock(return_value=_async_iter([expired_lease]))
    mock_client.replace = AsyncMock(side_effect=replace_side_effect)
    mock_client.close = AsyncMock()
    backend._client = mock_client

    # Act / Assert
    if should_raise:
        with pytest.raises(ApiError):
            await backend.__aexit__(None, None, None)
    else:
        await backend.__aexit__(None, None, None)
        assert backend._client is None


async def test_aexit_skips_live_lease() -> None:
    """Cleanup leaves a still-live lease in place and does not vacate it."""
    # Arrange
    backend = KubernetesLockAdapter(namespace="default")
    live_lease = _make_lease(holder=TOKEN, expired=False)
    mock_client = AsyncMock()
    mock_client.list = MagicMock(return_value=_async_iter([live_lease]))
    mock_client.replace = AsyncMock()
    mock_client.close = AsyncMock()
    backend._client = mock_client

    # Act
    await backend.__aexit__(None, None, None)

    # Assert
    mock_client.replace.assert_not_called()
    assert backend._client is None


async def test_aexit_skips_already_vacated_lease() -> None:
    """Cleanup leaves an already-vacated (no holder) lease untouched."""
    # Arrange
    backend = KubernetesLockAdapter(namespace="default")
    vacated = Lease(
        metadata=ObjectMeta(name="test", namespace="default"),
        spec=LeaseSpec(leaseTransitions=2),
    )
    mock_client = AsyncMock()
    mock_client.list = MagicMock(return_value=_async_iter([vacated]))
    mock_client.replace = AsyncMock()
    mock_client.close = AsyncMock()
    backend._client = mock_client

    # Act
    await backend.__aexit__(None, None, None)

    # Assert
    mock_client.replace.assert_not_called()
    assert backend._client is None
