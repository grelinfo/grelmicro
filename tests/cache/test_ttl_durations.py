"""Cache TTLs take whole seconds or a `timedelta`, never a float."""

from collections.abc import Mapping, Sequence
from datetime import timedelta

import pytest
from pydantic import TypeAdapter, ValidationError

from grelmicro.cache import Cache, TTLCache, TTLCacheConfig
from grelmicro.cache.cached import cached
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.errors import SettingsValidationError

pytestmark = [pytest.mark.timeout(10)]

WHOLE = 12
UNDER_A_SECOND = timedelta(seconds=12, milliseconds=500)

READ_BACK = [
    pytest.param(WHOLE, timedelta(seconds=WHOLE), id="int"),
    pytest.param(UNDER_A_SECOND, UNDER_A_SECOND, id="timedelta"),
]

FLOATS = [
    pytest.param(12.5, id="float"),
    pytest.param(12.0, id="whole-float"),
    pytest.param(True, id="bool"),
]

TEXT = [
    pytest.param("60", id="seconds-text"),
    pytest.param("PT0.5S", id="iso-8601-text"),
]

NOT_A_DURATION = [*FLOATS, *TEXT]

REFUSED_ARGUMENTS = [*NOT_A_DURATION, pytest.param(0, id="zero")]


class _RecordingBackend(MemoryCacheAdapter):
    """Memory backend that records the TTL of each write."""

    def __init__(self) -> None:
        super().__init__()
        self.ttls: dict[str, timedelta] = {}

    async def set(
        self,
        *,
        key: str,
        value: bytes,
        ttl: timedelta,
        tags: Sequence[str] = (),
    ) -> None:
        self.ttls[key] = ttl
        await super().set(key=key, value=value, ttl=ttl, tags=tags)

    async def set_many(
        self,
        *,
        items: Mapping[str, bytes],
        ttl: timedelta,
        tags: Sequence[str] = (),
    ) -> None:
        for key in items:
            self.ttls[key] = ttl
        await super().set_many(items=items, ttl=ttl, tags=tags)


async def _produce() -> bytes:
    return b"v"


def _refuse_pydantic(*_args: object, **_kwargs: object) -> None:
    msg = "Pydantic validation ran"
    raise AssertionError(msg)


@pytest.fixture
def cache() -> TTLCache:
    """Provide a cache on its own memory backend."""
    return TTLCache(backend=MemoryCacheAdapter())


@pytest.mark.parametrize("value", FLOATS)
def test_ttl_cache_float_ttl_refused(value: float) -> None:
    """A float or a bool default TTL is refused, naming `ttl`."""
    # Act / Assert
    with pytest.raises(
        SettingsValidationError,
        match="ttl must be whole seconds or a timedelta",
    ):
        TTLCache(ttl=value)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(("value", "expected"), READ_BACK)
def test_ttl_cache_ttl_reads_back_as_timedelta(
    value: int | timedelta, expected: timedelta
) -> None:
    """Whole seconds and a `timedelta` read back as a `timedelta`."""
    # Act
    cache = TTLCache(ttl=value)

    # Assert
    assert cache.config.ttl == expected


def test_ttl_cache_default_ttl_is_a_minute() -> None:
    """The default TTL is sixty seconds, as a `timedelta`."""
    # Act
    cache = TTLCache()

    # Assert
    assert cache.config.ttl == timedelta(seconds=60)


@pytest.mark.parametrize("value", FLOATS)
def test_ttl_cache_config_float_ttl_refused(value: float) -> None:
    """A float or a bool TTL is refused by the config."""
    # Act / Assert
    with pytest.raises(
        ValidationError, match="ttl must be whole seconds or a timedelta"
    ):
        TTLCacheConfig(ttl=value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("12", timedelta(seconds=WHOLE), id="seconds"),
        pytest.param("PT12.5S", UNDER_A_SECOND, id="iso-8601"),
    ],
)
def test_ttl_cache_config_ttl_from_text_reads_as_timedelta(
    raw: str, expected: timedelta
) -> None:
    """Text is whole seconds or an ISO 8601 duration."""
    # Act
    config = TTLCacheConfig.model_validate({"ttl": raw})

    # Assert
    assert config.ttl == expected


def test_ttl_cache_config_decimal_ttl_from_text_refused() -> None:
    """A decimal number of seconds from text is refused."""
    # Act / Assert
    with pytest.raises(ValidationError, match="ttl must be whole seconds"):
        TTLCacheConfig.model_validate({"ttl": "0.5"})


@pytest.mark.parametrize("value", FLOATS)
def test_cache_ttl_factory_float_ttl_refused(value: float) -> None:
    """`Cache.ttl` refuses a float or a bool TTL, naming `ttl`."""
    # Arrange
    component = Cache(MemoryCacheAdapter())

    # Act / Assert
    with pytest.raises(
        SettingsValidationError,
        match="ttl must be whole seconds or a timedelta",
    ):
        component.ttl(ttl=value)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(("value", "expected"), READ_BACK)
def test_cache_ttl_factory_ttl_reads_back_as_timedelta(
    value: int | timedelta, expected: timedelta
) -> None:
    """`Cache.ttl` takes whole seconds or a `timedelta`."""
    # Arrange
    component = Cache(MemoryCacheAdapter())

    # Act
    cache = component.ttl(ttl=value)

    # Assert
    assert cache.config.ttl == expected


@pytest.mark.parametrize("value", NOT_A_DURATION)
def test_cached_ttl_not_a_duration_refused(value: object) -> None:
    """`cached(ttl=...)` refuses a float, a bool or text, naming `ttl`."""
    # Act / Assert
    with pytest.raises(
        SettingsValidationError,
        match="ttl must be whole seconds or a timedelta",
    ):
        cached(ttl=value)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("value", NOT_A_DURATION)
def test_cached_stale_ttl_not_a_duration_refused(value: object) -> None:
    """`cached(stale_ttl=...)` refuses a float, a bool or text."""
    # Act / Assert
    with pytest.raises(
        SettingsValidationError,
        match="stale_ttl must be whole seconds or a timedelta",
    ):
        cached(ttl=WHOLE, stale_ttl=value)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("value", [0, -1, timedelta(0)])
def test_cached_stale_ttl_not_positive_refused(value: int | timedelta) -> None:
    """`cached(stale_ttl=...)` refuses zero or less."""
    # Act / Assert
    with pytest.raises(
        SettingsValidationError, match="stale_ttl must be greater than zero"
    ):
        cached(ttl=WHOLE, stale_ttl=value)


@pytest.mark.parametrize(("value", "_expected"), READ_BACK)
async def test_cached_ttl_and_stale_ttl_accepted(
    value: int | timedelta, _expected: timedelta
) -> None:
    """`cached` takes whole seconds or a `timedelta` for both durations."""

    # Arrange
    @cached(ttl=value, stale_ttl=value)
    async def load() -> int:
        return 1

    # Act
    result = await load()

    # Assert
    assert result == 1


@pytest.mark.parametrize("value", REFUSED_ARGUMENTS)
async def test_ttl_cache_set_bad_ttl_refused(
    cache: TTLCache, value: object
) -> None:
    """`set(ttl=...)` refuses a float, a bool, text or zero, naming `ttl`."""
    # Act / Assert
    with pytest.raises(ValueError, match=r"^ttl must be"):
        await cache.set("key", b"v", ttl=value)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("value", REFUSED_ARGUMENTS)
async def test_ttl_cache_set_bad_stale_ttl_refused(
    cache: TTLCache, value: object
) -> None:
    """`set(stale_ttl=...)` refuses a float, a bool, text or zero."""
    # Act / Assert
    with pytest.raises(ValueError, match=r"^stale_ttl must be"):
        await cache.set("key", b"v", stale_ttl=value)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("value", REFUSED_ARGUMENTS)
async def test_ttl_cache_set_many_bad_ttl_refused(
    cache: TTLCache, value: object
) -> None:
    """`set_many(ttl=...)` refuses a float, a bool, text or zero."""
    # Act / Assert
    with pytest.raises(ValueError, match=r"^ttl must be"):
        await cache.set_many({"key": b"v"}, ttl=value)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("value", REFUSED_ARGUMENTS)
async def test_ttl_cache_get_or_set_bad_ttl_refused(
    cache: TTLCache, value: object
) -> None:
    """`get_or_set(ttl=...)` refuses a float, a bool, text or zero."""
    # Act / Assert
    with pytest.raises(ValueError, match=r"^ttl must be"):
        await cache.get_or_set("key", _produce, ttl=value)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("value", REFUSED_ARGUMENTS)
async def test_ttl_cache_get_or_set_bad_stale_ttl_refused(
    cache: TTLCache, value: object
) -> None:
    """`get_or_set(stale_ttl=...)` refuses a float, a bool, text or zero."""
    # Act / Assert
    with pytest.raises(ValueError, match=r"^stale_ttl must be"):
        await cache.get_or_set("key", _produce, stale_ttl=value)  # ty: ignore[invalid-argument-type]


async def test_ttl_cache_get_or_set_hit_runs_no_pydantic_validation(
    cache: TTLCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hit checks `ttl` and `stale_ttl` without a Pydantic validation."""
    # Arrange
    await cache.set("key", b"v")
    monkeypatch.setattr(TypeAdapter, "validate_python", _refuse_pydantic)

    # Act
    result = await cache.get_or_set(
        "key", _produce, ttl=WHOLE, stale_ttl=UNDER_A_SECOND
    )

    # Assert
    assert result == b"v"


async def test_ttl_cache_get_or_set_miss_runs_no_pydantic_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A miss stores under the checked durations without Pydantic."""
    # Arrange
    backend = _RecordingBackend()
    cache = TTLCache(backend=backend)
    monkeypatch.setattr(TypeAdapter, "validate_python", _refuse_pydantic)

    # Act
    await cache.get_or_set("key", _produce, ttl=WHOLE, stale_ttl=UNDER_A_SECOND)

    # Assert
    assert backend.ttls["cache:key\x1fst"] == (
        timedelta(seconds=WHOLE) + UNDER_A_SECOND
    )


async def test_ttl_cache_set_passes_ttl_as_timedelta() -> None:
    """A per-entry TTL in whole seconds reaches the backend as a `timedelta`."""
    # Arrange
    backend = _RecordingBackend()
    cache = TTLCache(backend=backend)

    # Act
    await cache.set("key", b"v", ttl=WHOLE)

    # Assert
    assert backend.ttls["cache:key"] == timedelta(seconds=WHOLE)


async def test_ttl_cache_set_keeps_stale_reserve_for_ttl_plus_stale_ttl() -> (
    None
):
    """The stale reserve lives for the TTL plus `stale_ttl`, exactly."""
    # Arrange
    backend = _RecordingBackend()
    cache = TTLCache(ttl=UNDER_A_SECOND, backend=backend)

    # Act
    await cache.set("key", b"v", stale_ttl=timedelta(microseconds=1))

    # Assert
    assert backend.ttls["cache:key\x1fst"] == UNDER_A_SECOND + timedelta(
        microseconds=1
    )


async def test_ttl_cache_set_many_passes_ttl_as_timedelta() -> None:
    """`set_many` passes its TTL to the backend as a `timedelta`."""
    # Arrange
    backend = _RecordingBackend()
    cache = TTLCache(backend=backend)

    # Act
    await cache.set_many({"key": b"v"}, ttl=UNDER_A_SECOND)

    # Assert
    assert backend.ttls["cache:key"] == UNDER_A_SECOND


async def test_ttl_cache_get_or_set_passes_ttl_as_timedelta() -> None:
    """`get_or_set` stores the computed value under a `timedelta` TTL."""
    # Arrange
    backend = _RecordingBackend()
    cache = TTLCache(backend=backend)

    # Act
    await cache.get_or_set("key", _produce, ttl=WHOLE)

    # Assert
    assert backend.ttls["cache:key"] == timedelta(seconds=WHOLE)
