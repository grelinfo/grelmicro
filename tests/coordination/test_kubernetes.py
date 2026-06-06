"""Tests for the Kubernetes leader election backend."""

import tempfile
import time as time_module
from asyncio import sleep
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from lightkube.core.exceptions import ApiError
from lightkube.models.coordination_v1 import LeaseSpec
from lightkube.models.meta_v1 import ObjectMeta
from lightkube.resources.coordination_v1 import Lease
from testcontainers.core.container import DockerContainer

from grelmicro.coordination.abc import LeaderElectionBackend
from grelmicro.coordination.kubernetes import (
    _MAX_NAME_LENGTH,
    KubernetesLeaderElectionBackend,
    _annotations_to_metadata,
    _lease_to_record,
    _metadata_to_annotations,
    _sanitize_lease_name,
)
from grelmicro.errors import OutOfContextError, SettingsValidationError

TOKEN = "test-token"
OTHER = "other-token"

# Named values keep ruff's magic-value rule quiet in assertions.
_DURATION_SECONDS = 10.0
_MAPPED_DURATION_SECONDS = 15.0
_MAPPED_TRANSITIONS = 3
_RENEW_TRANSITIONS = 2
_LIVE_OTHER_TRANSITIONS = 5
_EXPIRED_OTHER_TRANSITIONS = 4
_OWN_EXPIRED_TRANSITIONS = 7
_CREATE_WINNER_TRANSITIONS = 1
_REPLACE_WINNER_TRANSITIONS = 3


def _make_api_error(code: int) -> ApiError:
    """Create an ApiError with the given status code."""
    return ApiError(
        status={"code": code, "message": "error", "status": "Failure"}
    )


def _make_lease(
    name: str = "test",
    holder: str = TOKEN,
    *,
    expired: bool = False,
    transitions: int = 0,
    resource_version: str = "1",
    annotations: dict[str, str] | None = None,
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
            annotations=annotations or {},
        ),
        spec=LeaseSpec(
            holderIdentity=holder,
            leaseDurationSeconds=duration_seconds,
            acquireTime=renew_time,
            renewTime=renew_time,
            leaseTransitions=transitions,
        ),
    )


def _make_mocked_backend(
    **client_overrides: AsyncMock,
) -> KubernetesLeaderElectionBackend:
    """Create a backend with a mocked client."""
    backend = KubernetesLeaderElectionBackend(namespace="default")
    mock_client = AsyncMock()
    for attr, mock in client_overrides.items():
        setattr(mock_client, attr, mock)
    backend._client = mock_client
    return backend


# --- Out of context ---


@pytest.mark.timeout(1)
async def test_out_of_context_errors() -> None:
    """Backend methods raise when called outside the context manager."""
    backend = KubernetesLeaderElectionBackend(namespace="default")

    with pytest.raises(OutOfContextError):
        await backend.acquire_or_renew(name="election", token=TOKEN, duration=1)
    with pytest.raises(OutOfContextError):
        await backend.release(name="election", token=TOKEN)
    with pytest.raises(OutOfContextError):
        await backend.get(name="election")


# --- __aenter__ / __aexit__ ---


@pytest.mark.timeout(1)
async def test_aenter_sets_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """`__aenter__` creates an AsyncClient."""
    backend = KubernetesLeaderElectionBackend(namespace="default")
    monkeypatch.setattr(
        "grelmicro.coordination.kubernetes.AsyncClient",
        lambda **_kwargs: AsyncMock(),
    )

    assert backend._client is None
    await backend.__aenter__()
    assert backend._client is not None


@pytest.mark.timeout(1)
async def test_aexit_closes_client() -> None:
    """`__aexit__` closes and clears the client."""
    backend = KubernetesLeaderElectionBackend(namespace="default")
    mock_client = AsyncMock()
    backend._client = mock_client

    await backend.__aexit__(None, None, None)

    mock_client.close.assert_awaited_once()
    assert backend._client is None


# --- Protocol ---


@pytest.mark.timeout(1)
def test_satisfies_protocol() -> None:
    """The backend satisfies the LeaderElectionBackend protocol."""
    backend = KubernetesLeaderElectionBackend(namespace="default")

    assert isinstance(backend, LeaderElectionBackend)


# --- Settings ---


@pytest.mark.timeout(1)
def test_env_var_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The namespace is read from the environment variable."""
    monkeypatch.setenv("KUBE_NAMESPACE", "my-namespace")

    backend = KubernetesLeaderElectionBackend()

    assert backend._namespace == "my-namespace"


@pytest.mark.timeout(1)
def test_env_var_settings_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing namespace raises a settings validation error."""
    monkeypatch.delenv("KUBE_NAMESPACE", raising=False)

    with pytest.raises(SettingsValidationError):
        KubernetesLeaderElectionBackend()


@pytest.mark.timeout(1)
def test_prefix() -> None:
    """The prefix is stored on the backend."""
    backend = KubernetesLeaderElectionBackend(
        namespace="default", prefix="myapp-"
    )

    assert backend._prefix == "myapp-"


# --- Name sanitization ---


@pytest.mark.timeout(1)
@pytest.mark.parametrize(
    ("input_name", "expected"),
    [
        ("simple", "simple"),
        ("election:my-service", "election-my-service"),
        ("UPPER_CASE", "upper-case"),
        ("with spaces", "with-spaces"),
        ("special!@#chars", "special-chars"),
        ("---leading-trailing---", "leading-trailing"),
        ("multiple---hyphens", "multiple-hyphens"),
        ("a" * 300, "a" * _MAX_NAME_LENGTH),
        ("a" * 252 + "!b", "a" * 252),
    ],
)
def test_sanitize_lease_name(input_name: str, expected: str) -> None:
    """Lease names are sanitized to valid Kubernetes resource names."""
    assert _sanitize_lease_name(input_name) == expected


@pytest.mark.timeout(1)
def test_sanitize_lease_name_empty() -> None:
    """An all-invalid name raises rather than producing an empty name."""
    with pytest.raises(ValueError, match="empty Kubernetes resource name"):
        _sanitize_lease_name("!!!")


# --- Metadata and annotations ---


@pytest.mark.timeout(1)
def test_metadata_annotations_round_trip() -> None:
    """Metadata maps to namespaced annotations and back."""
    metadata = {"pod": "worker-0", "region": "eu"}

    annotations = _metadata_to_annotations(metadata)
    restored = _annotations_to_metadata(annotations)

    assert annotations == {
        "grelmicro.io/pod": "worker-0",
        "grelmicro.io/region": "eu",
    }
    assert restored == metadata


@pytest.mark.timeout(1)
def test_annotations_to_metadata_ignores_foreign_keys() -> None:
    """Only namespaced annotations are returned as metadata."""
    annotations = {
        "grelmicro.io/pod": "worker-0",
        "kubectl.kubernetes.io/last-applied": "{}",
    }

    assert _annotations_to_metadata(annotations) == {"pod": "worker-0"}


@pytest.mark.timeout(1)
def test_annotations_to_metadata_empty() -> None:
    """None or empty annotations map to an empty metadata dict."""
    assert _annotations_to_metadata(None) == {}
    assert _annotations_to_metadata({}) == {}


# --- Lease and record mapping ---


@pytest.mark.timeout(1)
def test_lease_to_record_maps_fields() -> None:
    """Lease spec fields map onto a LeaderRecord."""
    acquired = datetime(2024, 1, 1, tzinfo=UTC)
    renewed = datetime(2024, 1, 1, 0, 1, tzinfo=UTC)
    lease = Lease(
        metadata=ObjectMeta(
            name="test",
            annotations={"grelmicro.io/pod": "worker-0"},
        ),
        spec=LeaseSpec(
            holderIdentity="holder-a",
            leaseDurationSeconds=int(_MAPPED_DURATION_SECONDS),
            acquireTime=acquired,
            renewTime=renewed,
            leaseTransitions=_MAPPED_TRANSITIONS,
        ),
    )

    record = _lease_to_record(lease)

    assert record is not None
    assert record.holder == "holder-a"
    assert record.lease_duration == _MAPPED_DURATION_SECONDS
    assert record.acquired_at == acquired
    assert record.renewed_at == renewed
    assert record.transitions == _MAPPED_TRANSITIONS
    assert record.metadata == {"pod": "worker-0"}


@pytest.mark.timeout(1)
@pytest.mark.parametrize(
    "spec",
    [
        LeaseSpec(leaseDurationSeconds=15),
        LeaseSpec(holderIdentity="h", acquireTime=datetime.now(tz=UTC)),
        LeaseSpec(
            holderIdentity="h",
            leaseDurationSeconds=15,
            renewTime=datetime.now(tz=UTC),
        ),
    ],
    ids=["no-holder", "no-duration", "no-acquire"],
)
def test_lease_to_record_returns_none_on_partial(spec: LeaseSpec) -> None:
    """An incomplete Lease maps to None."""
    lease = Lease(metadata=ObjectMeta(name="test"), spec=spec)
    assert _lease_to_record(lease) is None


# --- acquire_or_renew with a mocked client ---


@pytest.mark.timeout(1)
async def test_acquire_creates_when_not_found() -> None:
    """Acquire creates a Lease when none exists."""
    create = AsyncMock()
    backend = _make_mocked_backend(
        get=AsyncMock(side_effect=_make_api_error(404)),
        create=create,
    )

    record = await backend.acquire_or_renew(
        name="election",
        token=TOKEN,
        duration=_DURATION_SECONDS,
        metadata={"pod": "w0"},
    )

    create.assert_awaited_once()
    assert record.holder == TOKEN
    assert record.transitions == 0
    assert record.lease_duration == _DURATION_SECONDS
    assert record.metadata == {"pod": "w0"}


@pytest.mark.timeout(1)
async def test_renew_keeps_transitions_and_acquired_at() -> None:
    """Renewing the same live holder keeps transitions and acquire time."""
    lease = _make_lease(holder=TOKEN, transitions=_RENEW_TRANSITIONS)
    assert lease.spec is not None
    original_acquired = lease.spec.acquireTime
    assert original_acquired is not None
    replace = AsyncMock()
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=lease),
        replace=replace,
    )

    record = await backend.acquire_or_renew(
        name="election", token=TOKEN, duration=_DURATION_SECONDS
    )

    replace.assert_awaited_once()
    assert record.holder == TOKEN
    assert record.transitions == _RENEW_TRANSITIONS
    assert record.acquired_at == original_acquired
    assert record.renewed_at >= original_acquired


@pytest.mark.timeout(1)
async def test_live_lease_held_by_other_is_returned_unchanged() -> None:
    """A live lease held by another token is not written to."""
    replace = AsyncMock()
    backend = _make_mocked_backend(
        get=AsyncMock(
            return_value=_make_lease(
                holder=OTHER, transitions=_LIVE_OTHER_TRANSITIONS
            )
        ),
        replace=replace,
    )

    record = await backend.acquire_or_renew(
        name="election", token=TOKEN, duration=_DURATION_SECONDS
    )

    replace.assert_not_awaited()
    assert record.holder == OTHER
    assert record.transitions == _LIVE_OTHER_TRANSITIONS


@pytest.mark.timeout(1)
async def test_takeover_after_expiry_bumps_transitions() -> None:
    """Taking over an expired lease from another holder bumps transitions."""
    expired = _make_lease(
        holder=OTHER, expired=True, transitions=_EXPIRED_OTHER_TRANSITIONS
    )
    replace = AsyncMock()
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=expired),
        replace=replace,
    )

    record = await backend.acquire_or_renew(
        name="election", token=TOKEN, duration=_DURATION_SECONDS
    )

    replace.assert_awaited_once()
    assert record.holder == TOKEN
    assert record.transitions == _EXPIRED_OTHER_TRANSITIONS + 1


@pytest.mark.timeout(1)
async def test_reacquire_own_expired_lease_keeps_transitions() -> None:
    """Re-acquiring one's own expired lease keeps transitions."""
    expired = _make_lease(
        holder=TOKEN, expired=True, transitions=_OWN_EXPIRED_TRANSITIONS
    )
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=expired),
        replace=AsyncMock(),
    )

    record = await backend.acquire_or_renew(
        name="election", token=TOKEN, duration=_DURATION_SECONDS
    )

    assert record.holder == TOKEN
    assert record.transitions == _OWN_EXPIRED_TRANSITIONS


@pytest.mark.timeout(1)
async def test_acquire_conflict_on_create_rereads() -> None:
    """A create conflict re-reads and returns the winner's record."""
    winner = _make_lease(holder=OTHER, transitions=_CREATE_WINNER_TRANSITIONS)
    backend = _make_mocked_backend(
        get=AsyncMock(side_effect=[_make_api_error(404), winner]),
        create=AsyncMock(side_effect=_make_api_error(409)),
    )

    record = await backend.acquire_or_renew(
        name="election", token=TOKEN, duration=_DURATION_SECONDS
    )

    assert record.holder == OTHER


@pytest.mark.timeout(1)
async def test_acquire_conflict_on_replace_rereads() -> None:
    """A replace conflict re-reads and returns the winner's record."""
    expired = _make_lease(
        holder=OTHER, expired=True, transitions=_RENEW_TRANSITIONS
    )
    winner = _make_lease(
        holder="winner", transitions=_REPLACE_WINNER_TRANSITIONS
    )
    backend = _make_mocked_backend(
        get=AsyncMock(side_effect=[expired, winner]),
        replace=AsyncMock(side_effect=_make_api_error(409)),
    )

    record = await backend.acquire_or_renew(
        name="election", token=TOKEN, duration=_DURATION_SECONDS
    )

    assert record.holder == "winner"


@pytest.mark.timeout(1)
async def test_acquire_raises_on_non_404_get() -> None:
    """Acquire re-raises a non-404 GET error."""
    backend = _make_mocked_backend(
        get=AsyncMock(side_effect=_make_api_error(500)),
    )

    with pytest.raises(ApiError):
        await backend.acquire_or_renew(name="election", token=TOKEN, duration=1)


@pytest.mark.timeout(1)
async def test_acquire_raises_on_non_409_create() -> None:
    """Acquire re-raises a non-409 create error."""
    backend = _make_mocked_backend(
        get=AsyncMock(side_effect=_make_api_error(404)),
        create=AsyncMock(side_effect=_make_api_error(500)),
    )

    with pytest.raises(ApiError):
        await backend.acquire_or_renew(name="election", token=TOKEN, duration=1)


@pytest.mark.timeout(1)
async def test_acquire_raises_on_non_409_replace() -> None:
    """Acquire re-raises a non-409 replace error."""
    expired = _make_lease(holder=OTHER, expired=True)
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=expired),
        replace=AsyncMock(side_effect=_make_api_error(500)),
    )

    with pytest.raises(ApiError):
        await backend.acquire_or_renew(name="election", token=TOKEN, duration=1)


@pytest.mark.timeout(1)
async def test_conflict_reread_returns_empty_when_vanished() -> None:
    """A re-read after a conflict returns an empty record when the Lease is gone."""
    expired = _make_lease(holder=OTHER, expired=True)
    backend = _make_mocked_backend(
        get=AsyncMock(side_effect=[expired, _make_api_error(404)]),
        replace=AsyncMock(side_effect=_make_api_error(409)),
    )

    record = await backend.acquire_or_renew(
        name="election", token=TOKEN, duration=_DURATION_SECONDS
    )

    assert record.holder == ""
    assert record.transitions == 0
    assert record.lease_duration == 0.0


@pytest.mark.timeout(1)
async def test_conflict_reread_returns_empty_when_partial() -> None:
    """A re-read returning a half-written Lease falls back to an empty record."""
    expired = _make_lease(holder=OTHER, expired=True)
    partial = Lease(
        metadata=ObjectMeta(name="test", namespace="default"),
        spec=LeaseSpec(leaseDurationSeconds=int(_DURATION_SECONDS)),
    )
    backend = _make_mocked_backend(
        get=AsyncMock(side_effect=[expired, partial]),
        replace=AsyncMock(side_effect=_make_api_error(409)),
    )

    record = await backend.acquire_or_renew(
        name="election", token=TOKEN, duration=_DURATION_SECONDS
    )

    assert record.holder == ""


@pytest.mark.timeout(1)
async def test_conflict_reread_raises_on_non_404_get() -> None:
    """A re-read after a conflict re-raises a non-404 GET error."""
    expired = _make_lease(holder=OTHER, expired=True)
    backend = _make_mocked_backend(
        get=AsyncMock(side_effect=[expired, _make_api_error(500)]),
        replace=AsyncMock(side_effect=_make_api_error(409)),
    )

    with pytest.raises(ApiError):
        await backend.acquire_or_renew(
            name="election", token=TOKEN, duration=_DURATION_SECONDS
        )


# --- release with a mocked client ---


@pytest.mark.timeout(1)
async def test_release_succeeds() -> None:
    """Release deletes the Lease held by the token."""
    delete = AsyncMock()
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=_make_lease(holder=TOKEN)),
        delete=delete,
    )

    result = await backend.release(name="election", token=TOKEN)

    assert result is True
    delete.assert_awaited_once()


@pytest.mark.timeout(1)
async def test_release_wrong_token() -> None:
    """Release returns False when the token does not hold the lease."""
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=_make_lease(holder=OTHER)),
    )

    assert await backend.release(name="election", token=TOKEN) is False


@pytest.mark.timeout(1)
async def test_release_not_found() -> None:
    """Release returns False when the Lease is absent."""
    backend = _make_mocked_backend(
        get=AsyncMock(side_effect=_make_api_error(404)),
    )

    assert await backend.release(name="election", token=TOKEN) is False


@pytest.mark.timeout(1)
async def test_release_expired() -> None:
    """Release returns False when the lease has expired."""
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=_make_lease(holder=TOKEN, expired=True)),
    )

    assert await backend.release(name="election", token=TOKEN) is False


@pytest.mark.timeout(1)
async def test_release_raises_on_non_404_get() -> None:
    """Release re-raises a non-404 GET error."""
    backend = _make_mocked_backend(
        get=AsyncMock(side_effect=_make_api_error(500)),
    )

    with pytest.raises(ApiError):
        await backend.release(name="election", token=TOKEN)


@pytest.mark.timeout(1)
async def test_release_delete_not_found_returns_false() -> None:
    """Release returns False when the Lease vanishes before the delete."""
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=_make_lease(holder=TOKEN)),
        delete=AsyncMock(side_effect=_make_api_error(404)),
    )

    assert await backend.release(name="election", token=TOKEN) is False


@pytest.mark.timeout(1)
async def test_release_raises_on_non_404_delete() -> None:
    """Release re-raises a non-404 delete error."""
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=_make_lease(holder=TOKEN)),
        delete=AsyncMock(side_effect=_make_api_error(500)),
    )

    with pytest.raises(ApiError):
        await backend.release(name="election", token=TOKEN)


# --- get with a mocked client ---


@pytest.mark.timeout(1)
async def test_get_returns_record_when_live() -> None:
    """Get returns the record for a live lease."""
    backend = _make_mocked_backend(
        get=AsyncMock(
            return_value=_make_lease(
                holder=TOKEN, annotations={"grelmicro.io/pod": "w0"}
            )
        ),
    )

    record = await backend.get(name="election")

    assert record is not None
    assert record.holder == TOKEN
    assert record.metadata == {"pod": "w0"}


@pytest.mark.timeout(1)
async def test_get_returns_none_when_expired() -> None:
    """Get returns None when the lease has expired."""
    backend = _make_mocked_backend(
        get=AsyncMock(return_value=_make_lease(holder=TOKEN, expired=True)),
    )

    assert await backend.get(name="election") is None


@pytest.mark.timeout(1)
async def test_get_returns_none_when_not_found() -> None:
    """Get returns None when the Lease is absent."""
    backend = _make_mocked_backend(
        get=AsyncMock(side_effect=_make_api_error(404)),
    )

    assert await backend.get(name="election") is None


@pytest.mark.timeout(1)
async def test_get_raises_on_non_404_get() -> None:
    """Get re-raises a non-404 GET error."""
    backend = _make_mocked_backend(
        get=AsyncMock(side_effect=_make_api_error(500)),
    )

    with pytest.raises(ApiError):
        await backend.get(name="election")


# --- Integration tests ---

_DURATION = 1.0
_EXPIRE_WAIT = _DURATION + 2.0


def _wait_for_k3s(
    container: DockerContainer,
    timeout: float = 60,
) -> None:
    """Wait for k3s to become ready."""
    start = time_module.time()
    while time_module.time() - start < timeout:
        exit_code, _ = container.exec("kubectl get --raw /readyz")
        if exit_code == 0:
            return
        time_module.sleep(1)
    msg = "k3s did not become ready"
    raise TimeoutError(msg)


def _extract_kubeconfig(container: DockerContainer) -> str:
    """Extract the kubeconfig from the k3s container."""
    exit_code, output = container.exec("cat /etc/rancher/k3s/k3s.yaml")
    if exit_code != 0:
        msg = "Failed to extract kubeconfig"
        raise RuntimeError(msg)
    return output.decode()


@pytest.fixture(scope="module")
def k3s_container() -> Generator[str]:
    """Start a k3s container and yield a kubeconfig path pointing at it."""
    with (
        DockerContainer("rancher/k3s:v1.31.4-k3s1")
        .with_command(
            "server --disable=traefik,metrics-server --tls-san=127.0.0.1"
        )
        .with_kwargs(
            privileged=True,
            tmpfs={"/run": "", "/var/run": ""},
        )
        .with_exposed_ports(6443) as container
    ):
        _wait_for_k3s(container)
        kubeconfig_content = _extract_kubeconfig(container)
        port = container.get_exposed_port(6443)
        kubeconfig_content = kubeconfig_content.replace(
            "https://127.0.0.1:6443",
            f"https://127.0.0.1:{port}",
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(kubeconfig_content)
            kubeconfig_path = f.name
        yield kubeconfig_path


@pytest.fixture
async def backend(
    k3s_container: str,
) -> AsyncGenerator[KubernetesLeaderElectionBackend]:
    """Open a backend connected to the k3s container."""
    async with KubernetesLeaderElectionBackend(
        namespace="default", kubeconfig=k3s_container
    ) as backend:
        yield backend


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_acquire(backend: KubernetesLeaderElectionBackend) -> None:
    """A fresh election is acquired and creates a live Lease."""
    name = "acquire-" + uuid4().hex
    token = uuid4().hex

    record = await backend.acquire_or_renew(
        name=name, token=token, duration=_DURATION, metadata={"pod": "w-a"}
    )
    fetched = await backend.get(name=name)

    assert record.holder == token
    assert record.transitions == 0
    assert fetched is not None
    assert fetched.holder == token
    assert fetched.metadata == {"pod": "w-a"}


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_renew_keeps_transitions(
    backend: KubernetesLeaderElectionBackend,
) -> None:
    """Renewing the same holder moves renewed_at but not transitions."""
    name = "renew-" + uuid4().hex
    token = uuid4().hex

    first = await backend.acquire_or_renew(
        name=name, token=token, duration=_DURATION
    )
    second = await backend.acquire_or_renew(
        name=name, token=token, duration=_DURATION
    )

    assert second.holder == token
    assert second.transitions == first.transitions == 0
    assert second.acquired_at == first.acquired_at
    assert second.renewed_at >= first.renewed_at


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_live_lease_not_taken(
    backend: KubernetesLeaderElectionBackend,
) -> None:
    """A live lease cannot be taken by another holder."""
    name = "live-" + uuid4().hex
    token1 = uuid4().hex
    token2 = uuid4().hex

    first = await backend.acquire_or_renew(
        name=name, token=token1, duration=_DURATION
    )
    second = await backend.acquire_or_renew(
        name=name, token=token2, duration=_DURATION
    )

    assert first.holder == token1
    assert second.holder == token1
    assert second.transitions == 0


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_takeover_after_expiry(
    backend: KubernetesLeaderElectionBackend,
) -> None:
    """A new holder takes over after expiry and bumps transitions."""
    name = "takeover-" + uuid4().hex
    token1 = uuid4().hex
    token2 = uuid4().hex

    await backend.acquire_or_renew(name=name, token=token1, duration=_DURATION)
    await sleep(_EXPIRE_WAIT)
    record = await backend.acquire_or_renew(
        name=name, token=token2, duration=_DURATION
    )

    assert record.holder == token2
    assert record.transitions == 1


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_release(backend: KubernetesLeaderElectionBackend) -> None:
    """Release returns True for the holder, False for non-holders."""
    name = "release-" + uuid4().hex
    token = uuid4().hex
    other = uuid4().hex

    await backend.acquire_or_renew(name=name, token=token, duration=_DURATION)

    assert await backend.release(name=name, token=other) is False
    assert await backend.release(name=name, token=token) is True
    assert await backend.get(name=name) is None


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_get_live_and_expired(
    backend: KubernetesLeaderElectionBackend,
) -> None:
    """Get returns the live record then None after expiry."""
    name = "get-" + uuid4().hex
    token = uuid4().hex

    assert await backend.get(name=name) is None
    await backend.acquire_or_renew(name=name, token=token, duration=_DURATION)
    live = await backend.get(name=name)
    assert live is not None
    assert live.holder == token

    await sleep(_EXPIRE_WAIT)
    assert await backend.get(name=name) is None


@pytest.mark.integration
@pytest.mark.timeout(60)
async def test_metadata_roundtrip(
    backend: KubernetesLeaderElectionBackend,
) -> None:
    """Metadata round-trips through Lease annotations."""
    name = "metadata-" + uuid4().hex
    token = uuid4().hex
    metadata = {"pod": "worker-0", "region": "eu-west", "version": "1.2.3"}

    record = await backend.acquire_or_renew(
        name=name, token=token, duration=_DURATION, metadata=metadata
    )

    assert dict(record.metadata) == metadata
    fetched = await backend.get(name=name)
    assert fetched is not None
    assert dict(fetched.metadata) == metadata
