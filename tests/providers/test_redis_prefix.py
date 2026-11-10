"""Test the key prefix of every store a Redis or Valkey provider builds.

A provider's `prefix` comes before each store's own. On a Redis Cluster, a
store that touches several keys at once puts them under a hash tag of its
own when no prefix carries one. These tests build the providers without
connecting, and read the keys each store would send.
"""

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from redis.asyncio.client import Redis

from grelmicro import Grelmicro
from grelmicro.cache.redis import RedisCacheAdapter
from grelmicro.coordination.redis import (
    RedisLeaderElectionAdapter,
    RedisLockAdapter,
    RedisReadWriteLockAdapter,
    RedisScheduleAdapter,
)
from grelmicro.errors import SettingsValidationError
from grelmicro.providers.redis import RedisConfig, RedisProvider
from grelmicro.providers.valkey import ValkeyConfig, ValkeyProvider
from grelmicro.resilience import ConsecutiveCountConfig, TokenBucketConfig
from grelmicro.resilience.circuitbreaker.redis import (
    RedisCircuitBreakerAdapter,
)
from grelmicro.resilience.ratelimiter.redis import RedisRateLimiterAdapter
from grelmicro.task.redis import RedisJobAdapter

pytestmark = [pytest.mark.timeout(5)]

STANDALONE = "redis://localhost:6379/0"
SENTINEL = "redis+sentinel://h1:26379,h2:26379/mymaster/0"
CLUSTER = "redis+cluster://h1:6379,h2:6379"

_CLASSES: dict[str, type] = {
    "lock": RedisLockAdapter,
    "rwlock": RedisReadWriteLockAdapter,
    "cache": RedisCacheAdapter,
    "job": RedisJobAdapter,
    "leaderelection": RedisLeaderElectionAdapter,
    "schedule": RedisScheduleAdapter,
    "ratelimiter": RedisRateLimiterAdapter,
    "circuitbreaker": RedisCircuitBreakerAdapter,
}
"""The class of each store a provider builds."""

MULTI_KEY: dict[str, list[str]] = {
    "lock": ["cart", "fence:cart"],
    "rwlock": ["cart", "cart:r", "cart:i"],
    "cache": ["k", "cache:tag:t", "cache:rtag:k"],
    "job": ["job:deadlines", "job:id:j1", "job:queue:send", "job:wait:send"],
}
"""Each store that touches several keys at once, and its keys with no prefix."""

SINGLE_KEY: dict[str, list[str]] = {
    "leaderelection": ["le_us:x"],
    "schedule": ["n"],
    "ratelimiter": ["tb:k"],
    "circuitbreaker": ["cb:pay"],
}
"""Each store that touches one key per call, and its key with no prefix."""

STORES = {**MULTI_KEY, **SINGLE_KEY}


def _keys(store: str, provider: RedisProvider, prefix: str = "") -> list[str]:
    """Build `store` on `provider` with its own `prefix`, return its keys."""
    return _keys_of(_CLASSES[store](provider=provider, prefix=prefix))


def _rate_key(adapter: RedisRateLimiterAdapter) -> list[str]:
    strategy = adapter.bind(TokenBucketConfig(capacity=1, refill_rate=1.0))
    return [f"{strategy._key_prefix}k"]  # ty: ignore[unresolved-attribute]


def _breaker_key(adapter: RedisCircuitBreakerAdapter) -> list[str]:
    strategy = adapter.bind(name="pay", config=ConsecutiveCountConfig())
    return [strategy._key]  # ty: ignore[unresolved-attribute]


_SAMPLE_KEYS: dict[type, Callable[[Any], list[str]]] = {
    RedisLockAdapter: lambda a: [a._key("cart"), a._fence_key("cart")],
    RedisReadWriteLockAdapter: lambda a: a._keys("cart"),
    RedisCacheAdapter: lambda a: [
        a._full("k"),
        a._tag_key("t"),
        a._rtag_key("k"),
    ],
    RedisJobAdapter: lambda a: a._keys("send", "j1"),
    RedisLeaderElectionAdapter: lambda a: [a._key("x")],
    RedisScheduleAdapter: lambda a: [a._key("n")],
    RedisRateLimiterAdapter: _rate_key,
    RedisCircuitBreakerAdapter: _breaker_key,
}
"""The keys each store sends for one sample call."""


def _keys_of(adapter: object) -> list[str]:
    """Return the keys a built store sends for one sample call."""
    return _SAMPLE_KEYS[type(adapter)](adapter)


def _under(prefix: str, keys: list[str]) -> list[str]:
    return [f"{prefix}{key}" for key in keys]


def _segment(store: str) -> str:
    """Return the kind segment a store adds after a provider prefix."""
    return f"{store}:" if store in MULTI_KEY else ""


def _inner(store: str) -> list[str]:
    """Return a store's keys after its kind segment.

    The job store marks its keys with `job:` when nothing else does. Once a
    kind segment marks them, it is the only marker.
    """
    keys = STORES[store]
    if store == "job":
        return [key.removeprefix("job:") for key in keys]
    return keys


@pytest.mark.parametrize("url", [STANDALONE, SENTINEL])
@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("own", ["", "ns:", "{app}", "{open"])
def test_standalone_and_sentinel_keys_are_unchanged(
    url: str, store: str, own: str
) -> None:
    """With no provider prefix, every key stays byte for byte as before."""
    # Arrange
    bare = STORES[store]

    # Act
    found = _keys(store, RedisProvider(url), own)

    # Assert
    assert found == _under(own, bare)


@pytest.mark.parametrize("url", [STANDALONE, SENTINEL])
@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize("own", ["", "orders:"])
def test_provider_prefix_comes_before_the_store_prefix(
    url: str, store: str, own: str
) -> None:
    """Every store puts the provider prefix before its own.

    A store that touches several keys at once adds its kind after them, so
    no store reaches into another one's keys.
    """
    # Act
    found = _keys(store, RedisProvider(url, prefix="myapp:"), own)

    # Assert
    assert found == _under(f"myapp:{own}{_segment(store)}", _inner(store))


def test_lock_fence_sits_inside_the_lock_segment() -> None:
    """The fence counter of a lock lives under the lock's own segment."""
    # Arrange
    adapter = RedisLockAdapter(provider=RedisProvider(STANDALONE, prefix="a:"))

    # Act
    fence = adapter._fence_key("cart")

    # Assert
    assert fence == "a:lock:fence:cart"


def test_cache_clear_scans_only_its_own_segment() -> None:
    """With a provider prefix, `clear` never matches another store's keys."""
    # Arrange
    adapter = RedisCacheAdapter(provider=RedisProvider(STANDALONE, prefix="a:"))

    # Act
    pattern = f"{adapter._full('')}*"

    # Assert
    assert pattern == "a:cache:*"


@pytest.mark.parametrize(
    ("provider_prefix", "own", "tag"),
    [
        ("", "", "{KIND}:"),
        ("myapp:", "", "{myapp:KIND}:"),
        ("", "orders:", "{orders:KIND}:"),
        ("myapp:", "orders:", "{myapp:orders:KIND}:"),
    ],
)
@pytest.mark.parametrize("store", MULTI_KEY)
def test_cluster_store_adds_its_own_hash_tag(
    store: str, provider_prefix: str, own: str, tag: str
) -> None:
    """On a Cluster, an untagged multi-key store keeps its keys in one slot."""
    # Arrange
    provider = RedisProvider(CLUSTER, prefix=provider_prefix)

    # Act
    found = _keys(store, provider, own)

    # Assert
    assert found == _under(tag.replace("KIND", store), _inner(store))


@pytest.mark.parametrize(
    ("provider_prefix", "own", "expected"),
    [
        ("", "{app}", "{app}"),
        ("", "{app}:", "{app}:"),
        ("{myapp}:", "", "{myapp}:KIND:"),
        ("{myapp}:", "orders:", "{myapp}:orders:KIND:"),
        ("myapp:", "{app}", "myapp:{app}KIND:"),
        ("{myapp}:", "{open", "{myapp}:{openKIND:"),
    ],
)
@pytest.mark.parametrize("store", MULTI_KEY)
def test_cluster_store_keeps_a_tag_it_is_given(
    store: str, provider_prefix: str, own: str, expected: str
) -> None:
    """A tag in either prefix is kept, and a provider prefix adds the kind."""
    # Arrange
    bare = MULTI_KEY[store]
    provider = RedisProvider(CLUSTER, prefix=provider_prefix)

    # Act
    found = _keys(store, provider, own)

    # Assert
    keys = _inner(store) if "KIND" in expected else bare
    assert found == _under(expected.replace("KIND", store), keys)


@pytest.mark.parametrize("provider_prefix", ["", "myapp:"])
@pytest.mark.parametrize("store", SINGLE_KEY)
def test_cluster_single_key_store_takes_no_tag(
    store: str, provider_prefix: str
) -> None:
    """A store that touches one key per call spreads across the shards."""
    # Arrange
    bare = SINGLE_KEY[store]
    provider = RedisProvider(CLUSTER, prefix=provider_prefix)

    # Act
    found = _keys(store, provider)

    # Assert
    assert found == _under(provider_prefix, bare)


@pytest.mark.parametrize("own", ["{open", "{}", "}", "a}b", "x{y"])
@pytest.mark.parametrize("store", MULTI_KEY)
def test_cluster_store_refuses_a_brace_that_opens_no_tag(
    store: str, own: str
) -> None:
    """A store prefix with braces but no `{name}` is refused, as before."""
    # Act & Assert
    with pytest.raises(SettingsValidationError, match="hash tag"):
        _keys(store, RedisProvider(CLUSTER), own)


@pytest.mark.parametrize("cls", [RedisProvider, ValkeyProvider])
@pytest.mark.parametrize("url", [STANDALONE, CLUSTER])
@pytest.mark.parametrize(
    "prefix",
    ["{myapp", "{}", "my{app:", "{}{app}", "}", "a}b", "}{a}", "{a}b}"],
)
def test_provider_refuses_a_brace_that_opens_no_tag(
    cls: type[RedisProvider], url: str, prefix: str
) -> None:
    """The provider refuses a broken tag on every topology."""
    # Act & Assert
    with pytest.raises(SettingsValidationError, match="hash tag"):
        cls(url, prefix=prefix)


@pytest.mark.parametrize("store", MULTI_KEY)
@pytest.mark.parametrize(
    "own",
    ["{app}", "{a}b}", "}{a}", "{a{b}", "{}{app}", "x{}{a}", "{a}{b", "a}{b}"],
)
def test_cluster_store_keeps_every_prefix_it_accepted_before(
    store: str, own: str
) -> None:
    """A store prefix with a `{name}` anywhere is still used as given."""
    # Act
    found = _keys(store, RedisProvider(CLUSTER), own)

    # Assert
    assert found == _under(own, MULTI_KEY[store])


@pytest.mark.parametrize(
    ("url", "provider_prefix", "deadlines"),
    [
        (STANDALONE, "", "job:deadlines"),
        (SENTINEL, "", "job:deadlines"),
        (STANDALONE, "myapp:", "myapp:job:deadlines"),
        (CLUSTER, "", "{job}:deadlines"),
        (CLUSTER, "myapp:", "{myapp:job}:deadlines"),
        (CLUSTER, "{myapp}:", "{myapp}:job:deadlines"),
    ],
)
def test_job_store_marks_its_keys_once(
    url: str, provider_prefix: str, deadlines: str
) -> None:
    """The job store has one `job` marker on every topology."""
    # Act
    keys = _keys("job", RedisProvider(url, prefix=provider_prefix))

    # Assert
    assert keys[0] == deadlines
    assert not any("job:job:" in key for key in keys)


@pytest.mark.parametrize("store", MULTI_KEY)
@pytest.mark.parametrize("own", ["}", "a}b", "{a}b}"])
def test_standalone_store_keeps_a_stray_closing_brace(
    store: str, own: str
) -> None:
    """Off a Cluster, a store prefix is used as given, `}` included."""
    # Act
    found = _keys(store, RedisProvider(STANDALONE), own)

    # Assert
    assert found == _under(own, MULTI_KEY[store])


@pytest.mark.parametrize("cls", [RedisProvider, ValkeyProvider])
def test_provider_prefix_defaults_to_empty(cls: type[RedisProvider]) -> None:
    """A provider with no `prefix` changes no key."""
    # Act
    provider = cls(STANDALONE)

    # Assert
    assert provider.prefix == ""


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: RedisProvider(STANDALONE, prefix="myapp:"), id="redis"
        ),
        pytest.param(
            lambda: RedisProvider.from_config(
                RedisConfig(url=STANDALONE, prefix="myapp:")
            ),
            id="redis-config",
        ),
        pytest.param(
            lambda: RedisProvider.from_client(
                MagicMock(spec=Redis), prefix="myapp:"
            ),
            id="redis-client",
        ),
        pytest.param(
            lambda: RedisProvider.sentinel(
                sentinels=[("h1", 26379)],
                service_name="mymaster",
                prefix="myapp:",
            ),
            id="redis-sentinel",
        ),
        pytest.param(
            lambda: RedisProvider.cluster(
                nodes=[("h1", 6379)], prefix="myapp:"
            ),
            id="redis-cluster",
        ),
        pytest.param(
            lambda: ValkeyProvider(STANDALONE, prefix="myapp:"), id="valkey"
        ),
        pytest.param(
            lambda: ValkeyProvider.from_config(
                ValkeyConfig(url=STANDALONE, prefix="myapp:")
            ),
            id="valkey-config",
        ),
        pytest.param(
            lambda: ValkeyProvider.from_client(MagicMock(), prefix="myapp:"),
            id="valkey-client",
        ),
        pytest.param(
            lambda: ValkeyProvider.sentinel(
                sentinels=[("h1", 26379)],
                service_name="mymaster",
                prefix="myapp:",
            ),
            id="valkey-sentinel",
        ),
        pytest.param(
            lambda: ValkeyProvider.cluster(
                nodes=[("h1", 6379)], prefix="myapp:"
            ),
            id="valkey-cluster",
        ),
    ],
)
def test_every_construction_form_takes_a_prefix(
    build: Callable[[], RedisProvider],
) -> None:
    """Each way to build a provider carries the prefix to its stores."""
    # Act
    provider = build()

    # Assert
    assert provider.prefix == "myapp:"
    assert provider.schedule_backend()._key("n") == "myapp:n"


def test_from_config_refuses_a_broken_tag() -> None:
    """A broken tag in `RedisConfig.prefix` is refused like the keyword."""
    # Arrange
    config = RedisConfig(url=STANDALONE, prefix="{myapp")

    # Act & Assert
    with pytest.raises(SettingsValidationError, match="hash tag"):
        RedisProvider.from_config(config)


@pytest.mark.parametrize("store", STORES)
def test_rebind_recomputes_the_prefix(store: str) -> None:
    """A store moved to another provider takes that provider's prefix."""
    # Arrange
    adapter = _CLASSES[store](provider=RedisProvider(STANDALONE, prefix="a:"))

    # Act
    adapter._rebind_provider(RedisProvider(STANDALONE, prefix="b:"))

    # Assert
    assert _keys_of(adapter) == _under(f"b:{_segment(store)}", _inner(store))


@pytest.mark.parametrize("store", MULTI_KEY)
def test_rebind_onto_a_cluster_adds_the_tag(store: str) -> None:
    """A store moved onto a Cluster provider takes the default tag."""
    # Arrange
    adapter = _CLASSES[store](provider=RedisProvider(STANDALONE))

    # Act
    adapter._rebind_provider(RedisProvider(CLUSTER, prefix="myapp:"))

    # Assert
    assert _keys_of(adapter) == _under(f"{{myapp:{store}}}:", _inner(store))


@pytest.mark.parametrize("cls", [RedisProvider, ValkeyProvider])
def test_a_bare_cluster_provider_builds_an_app(
    cls: type[RedisProvider],
) -> None:
    """A cluster URL alone builds an app, its lock under the default tag."""
    # Act
    micro = Grelmicro(uses=[cls(CLUSTER)])

    # Assert
    lock = micro.coordination.lock_backend
    assert isinstance(lock, RedisLockAdapter)
    assert lock._key("cart") == "{lock}:cart"


def test_circuit_breaker_keeps_its_prefix_until_the_provider_changes() -> None:
    """Every breaker shares one prefix, computed again after a rebind."""
    # Arrange
    adapter = RedisCircuitBreakerAdapter(
        provider=RedisProvider(STANDALONE, prefix="a:")
    )
    first = _breaker_key(adapter)

    # Act
    second = _breaker_key(adapter)
    adapter._rebind_provider(RedisProvider(STANDALONE, prefix="b:"))
    rebound = _breaker_key(adapter)

    # Assert
    assert (first, second, rebound) == (
        ["a:cb:pay"],
        ["a:cb:pay"],
        ["b:cb:pay"],
    )
