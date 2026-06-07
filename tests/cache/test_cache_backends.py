"""Tests for Cache Backends (parametrized across all implementations)."""

from asyncio import sleep
from collections.abc import AsyncGenerator, Generator

import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.postgres import PostgresContainer
from testcontainers.redis import RedisContainer

from grelmicro.cache._protocol import CacheBackend
from grelmicro.cache.cached import cached
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.cache.postgres import PostgresCacheAdapter
from grelmicro.cache.redis import RedisCacheAdapter
from grelmicro.cache.serializers import JsonSerializer
from grelmicro.cache.sqlite import SQLiteCacheAdapter
from grelmicro.cache.ttl import TTLCache
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.sqlite import SQLiteProvider

pytestmark = [pytest.mark.timeout(30)]


# --- Fixtures (parametrized across backends) ---


@pytest.fixture(
    params=[
        "memory",
        "sqlite",
        pytest.param("redis", marks=[pytest.mark.integration]),
        pytest.param("postgres", marks=[pytest.mark.integration]),
    ],
    scope="module",
)
def backend_name(request: pytest.FixtureRequest) -> str:
    """Backend name."""
    return request.param


@pytest.fixture(scope="module")
def container(
    backend_name: str,
) -> Generator[DockerContainer | None, None, None]:
    """Docker container (only for Redis and Postgres)."""
    if backend_name == "redis":
        with RedisContainer() as redis_container:
            yield redis_container
    elif backend_name == "postgres":
        with PostgresContainer() as pg_container:
            yield pg_container
    else:
        yield None


@pytest.fixture(scope="module")
async def backend(
    backend_name: str,
    container: DockerContainer | None,
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncGenerator[CacheBackend]:
    """Cache backend instance."""
    if backend_name == "redis" and container:
        port = container.get_exposed_port(6379)
        async with RedisCacheAdapter(
            provider=RedisProvider(f"redis://localhost:{port}/0"),
            prefix="test:",
        ) as redis_backend:
            yield redis_backend
    elif backend_name == "postgres" and container:
        port = container.get_exposed_port(5432)
        provider = PostgresProvider(
            f"postgresql://test:test@localhost:{port}/test"
        )
        async with (
            provider,
            PostgresCacheAdapter(
                provider=provider, prefix="test:"
            ) as pg_backend,
        ):
            yield pg_backend
    elif backend_name == "sqlite":
        path = tmp_path_factory.mktemp("cache") / "cache.db"
        async with (
            SQLiteProvider(str(path)) as provider,
            SQLiteCacheAdapter(
                provider=provider, prefix="test:"
            ) as sqlite_backend,
        ):
            yield sqlite_backend
    elif backend_name == "memory":
        async with MemoryCacheAdapter() as memory_backend:
            yield memory_backend


# --- Shared tests (run against all backends) ---


async def test_get_set_roundtrip(backend: CacheBackend) -> None:
    """Test that bytes written with set are returned unchanged by get."""
    await backend.set(key="roundtrip", value=b"hello", ttl=60)

    result = await backend.get(key="roundtrip")

    assert result == b"hello"


async def test_get_miss_returns_none(backend: CacheBackend) -> None:
    """Test that a nonexistent key returns None."""
    result = await backend.get(key="nonexistent_key_xyz")

    assert result is None


async def test_ttl_expiry(backend: CacheBackend) -> None:
    """Test that a key becomes unavailable after the TTL elapses."""
    await backend.set(key="expiring", value=b"temp", ttl=0.5)

    assert await backend.get(key="expiring") == b"temp"

    await sleep(1.0)

    assert await backend.get(key="expiring") is None


async def test_stale_on_error_serves_last_value(backend: CacheBackend) -> None:
    """A failing recompute serves the stale reserve within `stale_ttl`."""
    cache = TTLCache(ttl=0.5, backend=backend, serializer=JsonSerializer())
    fail = False
    calls = 0

    @cached(cache, stale_ttl=5)
    async def fetch() -> int:
        nonlocal calls
        calls += 1
        if fail:
            msg = "upstream down"
            raise RuntimeError(msg)
        return calls

    assert await fetch() == 1

    fail = True
    await sleep(1.0)  # primary entry expires, stale reserve still lives

    assert await fetch() == 1  # recompute raises, the stale value is served


async def test_delete(backend: CacheBackend) -> None:
    """Test that delete removes a key."""
    await backend.set(key="to_delete", value=b"bye", ttl=60)
    assert await backend.get(key="to_delete") == b"bye"

    await backend.delete(key="to_delete")

    assert await backend.get(key="to_delete") is None


async def test_delete_missing_key_is_no_op(backend: CacheBackend) -> None:
    """Test that deleting an absent key does not raise."""
    await backend.delete(key="never_existed_abc")


async def test_clear(backend: CacheBackend) -> None:
    """Test that clear removes all entries."""
    await backend.set(key="clear_a", value=b"a", ttl=60)
    await backend.set(key="clear_b", value=b"b", ttl=60)

    await backend.clear()

    assert await backend.get(key="clear_a") is None
    assert await backend.get(key="clear_b") is None


async def test_overwrite(backend: CacheBackend) -> None:
    """Test that setting the same key overwrites the previous value."""
    await backend.set(key="overwrite", value=b"old", ttl=60)
    await backend.set(key="overwrite", value=b"new", ttl=60)

    result = await backend.get(key="overwrite")

    assert result == b"new"


# --- Batch operations (run against all backends) ---


async def test_get_many_returns_found_only(backend: CacheBackend) -> None:
    """Test that get_many returns only the keys that exist."""
    await backend.set(key="gm_a", value=b"a", ttl=60)
    await backend.set(key="gm_b", value=b"b", ttl=60)

    result = await backend.get_many(keys=["gm_a", "gm_b", "gm_missing"])

    assert result == {"gm_a": b"a", "gm_b": b"b"}


async def test_get_many_empty_keys(backend: CacheBackend) -> None:
    """Test that get_many with no keys returns an empty dict."""
    assert await backend.get_many(keys=[]) == {}


async def test_set_many_stores_all(backend: CacheBackend) -> None:
    """Test that set_many writes every key."""
    await backend.set_many(items={"sm_a": b"a", "sm_b": b"b"}, ttl=60)

    assert await backend.get(key="sm_a") == b"a"
    assert await backend.get(key="sm_b") == b"b"


async def test_set_many_empty_is_no_op(backend: CacheBackend) -> None:
    """Test that set_many with no items does not raise."""
    await backend.set_many(items={}, ttl=60)


async def test_delete_many_removes_all(backend: CacheBackend) -> None:
    """Test that delete_many removes every listed key."""
    await backend.set(key="dm_a", value=b"a", ttl=60)
    await backend.set(key="dm_b", value=b"b", ttl=60)

    await backend.delete_many(keys=["dm_a", "dm_b", "dm_missing"])

    assert await backend.get(key="dm_a") is None
    assert await backend.get(key="dm_b") is None


async def test_delete_many_empty_is_no_op(backend: CacheBackend) -> None:
    """Test that delete_many with no keys does not raise."""
    await backend.delete_many(keys=[])


# --- Tags (run against all backends) ---


async def test_set_with_tags_then_delete_tags(backend: CacheBackend) -> None:
    """Test that delete_tags invalidates every key sharing a tag."""
    await backend.set(key="tg_a", value=b"a", ttl=60, tags=["group"])
    await backend.set(key="tg_b", value=b"b", ttl=60, tags=["group"])
    await backend.set(key="tg_c", value=b"c", ttl=60, tags=["other"])

    await backend.delete_tags(tags=["group"])

    assert await backend.get(key="tg_a") is None
    assert await backend.get(key="tg_b") is None
    assert await backend.get(key="tg_c") == b"c"


async def test_delete_cleans_tag_membership(backend: CacheBackend) -> None:
    """Test that deleting a key leaves its tag empty of that key."""
    await backend.set(key="dc_a", value=b"a", ttl=60, tags=["g"])
    await backend.set(key="dc_b", value=b"b", ttl=60, tags=["g"])

    await backend.delete(key="dc_a")
    await backend.delete_tags(tags=["g"])

    assert await backend.get(key="dc_b") is None


async def test_set_many_with_tags_then_delete_tags(
    backend: CacheBackend,
) -> None:
    """Test that set_many tags are invalidated by delete_tags."""
    await backend.set_many(
        items={"smt_a": b"a", "smt_b": b"b"}, ttl=60, tags=["bulk"]
    )

    await backend.delete_tags(tags=["bulk"])

    assert await backend.get(key="smt_a") is None
    assert await backend.get(key="smt_b") is None


async def test_overwrite_replaces_tags(backend: CacheBackend) -> None:
    """Test that re-setting a key with new tags drops the old tag link."""
    await backend.set(key="rt_k", value=b"v", ttl=60, tags=["old"])
    await backend.set(key="rt_k", value=b"v2", ttl=60, tags=["new"])

    await backend.delete_tags(tags=["old"])

    assert await backend.get(key="rt_k") == b"v2"

    await backend.delete_tags(tags=["new"])

    assert await backend.get(key="rt_k") is None


async def test_overwrite_with_empty_tags_clears_tags(
    backend: CacheBackend,
) -> None:
    """Re-setting a tagged key with no tags drops the old tag link."""
    await backend.set(key="et_k", value=b"v", ttl=60, tags=["old"])
    await backend.set(key="et_k", value=b"v2", ttl=60)  # no tags

    await backend.delete_tags(tags=["old"])

    # The key carries no tags now, so deleting "old" must not remove it.
    assert await backend.get(key="et_k") == b"v2"


async def test_clear_sweeps_tags(backend: CacheBackend) -> None:
    """Test that clear removes tagged entries and their tag bookkeeping."""
    await backend.set(key="ct_a", value=b"a", ttl=60, tags=["g"])

    await backend.clear()

    assert await backend.get(key="ct_a") is None
    # A fresh entry under the same tag is not haunted by stale members.
    await backend.set(key="ct_b", value=b"b", ttl=60, tags=["g"])
    await backend.delete_tags(tags=["g"])
    assert await backend.get(key="ct_b") is None


# --- Redis-specific tests ---


@pytest.mark.integration
async def test_redis_prefix_isolation() -> None:
    """Test that clearing one prefix does not affect another."""
    with RedisContainer() as container:
        port = container.get_exposed_port(6379)
        url = f"redis://localhost:{port}/0"
        async with (
            RedisCacheAdapter(
                provider=RedisProvider(url), prefix="alpha:"
            ) as alpha,
            RedisCacheAdapter(
                provider=RedisProvider(url), prefix="beta:"
            ) as beta,
        ):
            await alpha.set(key="k", value=b"alpha", ttl=60)
            await beta.set(key="k", value=b"beta", ttl=60)

            await alpha.clear()

            assert await alpha.get(key="k") is None
            assert await beta.get(key="k") == b"beta"


# --- End-to-end: TTLCache + @cached ---


@pytest.mark.integration
async def test_postgres_prefix_isolation() -> None:
    """Test that clearing one prefix does not affect another."""
    with PostgresContainer() as pg_container:
        port = pg_container.get_exposed_port(5432)
        url = f"postgresql://test:test@localhost:{port}/test"
        async with (
            PostgresProvider(url) as provider,
            PostgresCacheAdapter(provider=provider, prefix="alpha:") as alpha,
            PostgresCacheAdapter(provider=provider, prefix="beta:") as beta,
        ):
            await alpha.set(key="k", value=b"alpha", ttl=60)
            await beta.set(key="k", value=b"beta", ttl=60)

            await alpha.clear()

            assert await alpha.get(key="k") is None
            assert await beta.get(key="k") == b"beta"


@pytest.mark.integration
async def test_cached_end_to_end_with_postgres() -> None:
    """Test @cached with TTLCache backed by Postgres."""
    with PostgresContainer() as pg_container:
        port = pg_container.get_exposed_port(5432)
        url = f"postgresql://test:test@localhost:{port}/test"
        async with (
            PostgresProvider(url) as provider,
            PostgresCacheAdapter(
                provider=provider, prefix="e2e:"
            ) as pg_backend,
        ):
            cache = TTLCache(
                ttl=60,
                backend=pg_backend,
                serializer=JsonSerializer(),
            )
            call_count = 0

            @cached(cache, lock="local")
            async def fetch_user(user_id: int) -> dict:
                nonlocal call_count
                call_count += 1
                return {"id": user_id}

            first = await fetch_user(1)
            second = await fetch_user(1)

            assert first == {"id": 1}
            assert second == {"id": 1}
            assert call_count == 1


@pytest.mark.integration
async def test_cached_end_to_end_with_redis() -> None:
    """Test @cached with TTLCache backed by Redis."""
    with RedisContainer() as container:
        port = container.get_exposed_port(6379)
        url = f"redis://localhost:{port}/0"
        async with RedisCacheAdapter(
            provider=RedisProvider(url), prefix="e2e:"
        ) as redis_backend:
            cache = TTLCache(
                ttl=60,
                backend=redis_backend,
                serializer=JsonSerializer(),
            )
            call_count = 0

            @cached(cache)
            async def fetch_user(user_id: int) -> dict:
                nonlocal call_count
                call_count += 1
                return {"id": user_id}

            first = await fetch_user(1)
            second = await fetch_user(1)

            assert first == {"id": 1}
            assert second == {"id": 1}
            assert call_count == 1


async def test_cached_end_to_end_with_memory() -> None:
    """Test @cached with TTLCache backed by MemoryCacheAdapter."""
    async with MemoryCacheAdapter() as memory_backend:
        cache = TTLCache(
            ttl=60,
            backend=memory_backend,
            serializer=JsonSerializer(),
        )
        call_count = 0

        @cached(cache)
        async def fetch_user(user_id: int) -> dict:
            nonlocal call_count
            call_count += 1
            return {"id": user_id}

        first = await fetch_user(1)
        second = await fetch_user(1)

        assert first == {"id": 1}
        assert second == {"id": 1}
        assert call_count == 1
