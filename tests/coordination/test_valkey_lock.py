"""Tests for the Lock adapter against a Valkey container.

These tests are integration tests that require Docker.
They are marked `integration` and are not run in the unit-test suite.
"""

from collections.abc import AsyncGenerator, Generator

import pytest
from testcontainers.redis import RedisContainer

from grelmicro.coordination.redis import RedisLockAdapter
from grelmicro.providers.valkey import ValkeyProvider

pytestmark = [pytest.mark.timeout(30), pytest.mark.integration]


@pytest.fixture(scope="module")
def container() -> Generator[RedisContainer, None, None]:
    """Valkey test container using the official `valkey/valkey` image."""
    with RedisContainer(image="valkey/valkey:latest") as container:
        yield container


@pytest.fixture
async def provider(
    container: RedisContainer,
) -> AsyncGenerator[ValkeyProvider]:
    """ValkeyProvider bound to the container."""
    port = container.get_exposed_port(6379)
    async with ValkeyProvider(f"redis://localhost:{port}/0") as provider:
        yield provider


@pytest.fixture
async def backend(
    provider: ValkeyProvider,
) -> AsyncGenerator[RedisLockAdapter]:
    """RedisLockAdapter bound to the Valkey container via ValkeyProvider."""
    async with RedisLockAdapter(provider=provider) as backend:
        yield backend


async def test_acquire_returns_fencing_token(
    backend: RedisLockAdapter,
) -> None:
    """Acquiring a free lock returns a positive fencing token."""
    token = "tok-acquire"
    fence = await backend.acquire(
        name="lock:acquire", token=token, duration=5.0
    )
    assert fence is not None
    assert fence >= 1


async def test_acquire_same_token_returns_same_fence(
    backend: RedisLockAdapter,
) -> None:
    """The same holder re-acquiring its live lock gets the same fence token."""
    token = "tok-same"
    fence1 = await backend.acquire(
        name="lock:same-fence", token=token, duration=5.0
    )
    fence2 = await backend.acquire(
        name="lock:same-fence", token=token, duration=5.0
    )
    assert fence1 == fence2


async def test_acquire_other_token_blocked(
    backend: RedisLockAdapter,
) -> None:
    """A different token cannot acquire a live lock."""
    name = "lock:blocked"
    await backend.acquire(name=name, token="holder", duration=5.0)
    fence = await backend.acquire(name=name, token="challenger", duration=5.0)
    assert fence is None


async def test_release_held_lock(
    backend: RedisLockAdapter,
) -> None:
    """The holder can release its lock; a second release returns False."""
    name = "lock:release"
    token = "tok-release"
    await backend.acquire(name=name, token=token, duration=5.0)
    released = await backend.release(name=name, token=token)
    again = await backend.release(name=name, token=token)
    assert released is True
    assert again is False


async def test_release_other_token_denied(
    backend: RedisLockAdapter,
) -> None:
    """A non-holder cannot release the lock."""
    name = "lock:denied"
    await backend.acquire(name=name, token="holder", duration=5.0)
    released = await backend.release(name=name, token="other")
    assert released is False


async def test_locked_true_when_held(
    backend: RedisLockAdapter,
) -> None:
    """`locked` returns True when the lock is held."""
    name = "lock:locked-true"
    await backend.acquire(name=name, token="tok", duration=5.0)
    assert await backend.locked(name=name) is True


async def test_locked_false_when_free(
    backend: RedisLockAdapter,
) -> None:
    """`locked` returns False for a key that was never acquired."""
    assert await backend.locked(name="lock:never-set") is False


async def test_owned_true_for_holder(
    backend: RedisLockAdapter,
) -> None:
    """`owned` returns True for the token that holds the lock."""
    name = "lock:owned-true"
    token = "tok-owned"
    await backend.acquire(name=name, token=token, duration=5.0)
    assert await backend.owned(name=name, token=token) is True


async def test_owned_false_for_other(
    backend: RedisLockAdapter,
) -> None:
    """`owned` returns False for a token that does not hold the lock."""
    name = "lock:owned-false"
    await backend.acquire(name=name, token="holder", duration=5.0)
    assert await backend.owned(name=name, token="other") is False
