"""Idempotency and response cache TTLs take whole seconds or a `timedelta`."""

from collections.abc import Sequence
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from grelmicro._describe import _Endpoint, _reads_idempotent, _seconds_text
from grelmicro._duration import MAX_DURATION
from grelmicro.cache import JsonSerializer, TTLCache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.errors import SettingsValidationError
from grelmicro.http import (
    CachedResponses,
    CachedResponsesConfig,
    IdempotentRequests,
    IdempotentRequestsConfig,
)
from grelmicro.http._response_cache import (
    _kept_for,
    declare_cached,
    declared_cache,
)
from grelmicro.idempotency import Idempotency, IdempotencyConfig
from grelmicro.integrations.fastapi import CachedResponse

pytestmark = [pytest.mark.timeout(10)]

WHOLE = 12
UNDER_A_SECOND = timedelta(seconds=12, milliseconds=500)

FLOATS = [
    pytest.param(12.5, id="float"),
    pytest.param(12.0, id="whole-float"),
    pytest.param(True, id="bool"),
]

READ_BACK = [
    pytest.param(WHOLE, timedelta(seconds=WHOLE), id="int"),
    pytest.param(UNDER_A_SECOND, UNDER_A_SECOND, id="timedelta"),
]

FROM_TEXT = [
    pytest.param("12", timedelta(seconds=WHOLE), id="seconds"),
    pytest.param("PT12.5S", UNDER_A_SECOND, id="iso-8601"),
]


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


# --- Idempotency ---


def test_idempotency_config_default_ttl_is_a_day() -> None:
    """The default TTL is one day, as a `timedelta`."""
    # Act
    config = IdempotencyConfig()

    # Assert
    assert config.ttl == timedelta(days=1)


@pytest.mark.parametrize("value", FLOATS)
def test_idempotency_config_float_ttl_refused(value: float) -> None:
    """A float or a bool TTL is refused when the config is built."""
    # Act / Assert
    with pytest.raises(
        ValidationError, match="ttl must be whole seconds or a timedelta"
    ):
        IdempotencyConfig(ttl=value)


@pytest.mark.parametrize("value", FLOATS)
def test_idempotency_float_ttl_refused(value: float) -> None:
    """`Idempotency(ttl=...)` refuses a float or a bool, naming `ttl`."""
    # Act / Assert
    with pytest.raises(
        SettingsValidationError,
        match="ttl must be whole seconds or a timedelta",
    ):
        Idempotency("charge", ttl=value, env_load=False)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(("value", "expected"), READ_BACK)
def test_idempotency_ttl_reads_back_as_timedelta(
    value: int | timedelta, expected: timedelta
) -> None:
    """Whole seconds and a `timedelta` read back as a `timedelta`."""
    # Act
    idem = Idempotency("charge", ttl=value, env_load=False)

    # Assert
    assert idem.config.ttl == expected


@pytest.mark.parametrize(("raw", "expected"), FROM_TEXT)
def test_idempotency_ttl_from_environment_reads_as_timedelta(
    raw: str, expected: timedelta, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env text is whole seconds or an ISO 8601 duration."""
    # Arrange
    monkeypatch.setenv("GREL_IDEMPOTENCY_CHARGE_TTL", raw)

    # Act
    idem = Idempotency("charge", env_load=True)

    # Assert
    assert idem.config.ttl == expected


@pytest.mark.parametrize("value", FLOATS)
def test_idempotent_requests_float_ttl_refused(value: float) -> None:
    """`IdempotentRequests(ttl=...)` refuses a float or a bool."""
    # Act / Assert
    with pytest.raises(
        SettingsValidationError,
        match="ttl must be whole seconds or a timedelta",
    ):
        IdempotentRequests(ttl=value, env_load=False)  # ty: ignore[invalid-argument-type]


def test_idempotent_requests_env_load_reads_the_ttl_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_load=True` reads the TTL whatever the process-wide flag says."""
    # Arrange
    monkeypatch.delenv("GREL_ENV_LOAD", raising=False)
    monkeypatch.setenv("GREL_IDEMPOTENCY_HTTP_TTL", "PT0.5S")

    # Act
    component = IdempotentRequests(env_load=True)

    # Assert
    assert component.idempotency.config.ttl == timedelta(milliseconds=500)


def test_idempotent_requests_env_load_false_ignores_the_ttl_in_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`env_load=False` leaves the TTL variable unread."""
    # Arrange
    monkeypatch.setenv("GREL_ENV_LOAD", "true")
    monkeypatch.setenv("GREL_IDEMPOTENCY_HTTP_TTL", "PT0.5S")

    # Act
    component = IdempotentRequests(env_load=False)

    # Assert
    assert component.idempotency.config.ttl == timedelta(days=1)


def test_idempotent_requests_from_config_ignores_the_ttl_in_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`from_config` reads no environment variable, the TTL included."""
    # Arrange
    monkeypatch.setenv("GREL_ENV_LOAD", "true")
    monkeypatch.setenv("GREL_IDEMPOTENCY_HTTP_TTL", "PT0.5S")

    # Act
    component = IdempotentRequests.from_config(IdempotentRequestsConfig())

    # Assert
    assert component.idempotency.config.ttl == timedelta(days=1)


@pytest.mark.parametrize(
    "ttl",
    [
        pytest.param(timedelta(seconds=WHOLE), id="whole"),
        pytest.param(MAX_DURATION, id="max"),
    ],
)
async def test_idempotency_fingerprint_outlives_the_response_by_a_second(
    ttl: timedelta,
) -> None:
    """The fingerprint is kept one second past the response, even at 100 years."""
    # Arrange
    backend = _RecordingBackend()
    cache = TTLCache(backend=backend, serializer=JsonSerializer())
    idem = Idempotency("charge", ttl=ttl, cache=cache, env_load=False)

    # Act
    async with idem("key-1", fingerprint="abc") as op:
        op.store({"status": "ok"})

    # Assert
    assert sorted(backend.ttls.values()) == [ttl, ttl + timedelta(seconds=1)]


# --- Response cache ---


def test_cached_responses_config_default_ttl_is_a_minute() -> None:
    """The default TTL is sixty seconds, as a `timedelta`."""
    # Act
    config = CachedResponsesConfig()

    # Assert
    assert config.ttl == timedelta(seconds=60)


@pytest.mark.parametrize("value", FLOATS)
def test_cached_responses_config_float_ttl_refused(value: float) -> None:
    """A float or a bool TTL is refused when the config is built."""
    # Act / Assert
    with pytest.raises(
        ValidationError, match="ttl must be whole seconds or a timedelta"
    ):
        CachedResponsesConfig(ttl=value)


@pytest.mark.parametrize("value", FLOATS)
def test_cached_responses_config_float_per_path_ttl_refused(
    value: float,
) -> None:
    """A float per-path TTL is refused by position, never by pattern."""
    # Act / Assert
    with pytest.raises(
        ValidationError,
        match="pattern 2 in include must be whole seconds or a timedelta",
    ):
        CachedResponsesConfig(include={"/a": 1, "/secret/*": value})


@pytest.mark.parametrize("value", [0, -1, "0", timedelta(0)])
def test_cached_responses_config_per_path_ttl_not_positive_refused(
    value: object,
) -> None:
    """A per-path TTL of zero or less is refused by position."""
    # Act / Assert
    with pytest.raises(
        ValidationError, match="pattern 1 in include must be greater than zero"
    ):
        CachedResponsesConfig(include={"/a": value})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [*READ_BACK, *FROM_TEXT],
)
def test_cached_responses_config_per_path_ttl_reads_as_timedelta(
    raw: object, expected: timedelta
) -> None:
    """A per-path TTL is whole seconds, a `timedelta`, or text."""
    # Act
    config = CachedResponsesConfig(include={"/a": raw})

    # Assert
    assert config.include == {"/a": expected}


@pytest.mark.parametrize("value", FLOATS)
def test_cached_responses_float_ttl_refused(value: float) -> None:
    """`CachedResponses(ttl=...)` refuses a float or a bool."""
    # Act / Assert
    with pytest.raises(
        SettingsValidationError,
        match="ttl must be whole seconds or a timedelta",
    ):
        CachedResponses(ttl=value, env_load=False)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(("raw", "expected"), FROM_TEXT)
def test_cached_responses_ttl_from_environment_reads_as_timedelta(
    raw: str, expected: timedelta, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env text is whole seconds or an ISO 8601 duration."""
    # Arrange
    monkeypatch.setenv("GREL_CACHED_RESPONSES_TTL", raw)

    # Act
    component = CachedResponses(env_load=True)

    # Assert
    assert component._live.state.config.ttl == expected


def test_cached_responses_per_path_ttl_from_environment_reads_as_timedelta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env JSON gives each pattern whole seconds or an ISO 8601 duration."""
    # Arrange
    monkeypatch.setenv(
        "GREL_CACHED_RESPONSES_INCLUDE", '{"/a": 12, "/b/*": "PT12.5S"}'
    )

    # Act
    component = CachedResponses(env_load=True)

    # Assert
    assert component._live.state.config.include == {
        "/a": timedelta(seconds=WHOLE),
        "/b/*": UNDER_A_SECOND,
    }


@pytest.mark.parametrize(
    "value",
    [*FLOATS, pytest.param("60", id="text"), pytest.param(0, id="zero")],
)
def test_cached_response_declaration_bad_ttl_refused(value: object) -> None:
    """`CachedResponse(ttl=...)` refuses a float, a bool, text or zero."""
    # Act / Assert
    with pytest.raises(ValueError, match=r"^ttl must be"):
        CachedResponse(ttl=value)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(("value", "expected"), READ_BACK)
def test_cached_response_declaration_ttl_reads_as_timedelta(
    value: int | timedelta, expected: timedelta
) -> None:
    """A route declaration carries its TTL as a `timedelta`."""
    # Act
    declared = declared_cache(declare_cached(value))

    # Assert
    assert declared == expected


@pytest.mark.parametrize(
    ("kept", "expected"),
    [
        pytest.param(float("inf"), timedelta(seconds=WHOLE), id="unnamed"),
        pytest.param(30.0, timedelta(seconds=WHOLE), id="longer"),
        pytest.param(5.0, timedelta(seconds=5), id="shorter"),
        pytest.param(4.2, timedelta(seconds=5), id="fraction-rounds-up"),
        pytest.param(11.5, timedelta(seconds=WHOLE), id="never-past-ttl"),
    ],
)
def test_response_cache_kept_for_shorter_of_ttl_and_freshness(
    kept: float, expected: timedelta
) -> None:
    """A response is kept for its TTL or the freshness it names, if shorter."""
    # Act
    duration = _kept_for(timedelta(seconds=WHOLE), kept)

    # Assert
    assert duration == expected


# --- Describe ---


@pytest.mark.parametrize(
    ("duration", "text"),
    [
        pytest.param(timedelta(days=30), "2592000s", id="thirty-days"),
        pytest.param(timedelta(milliseconds=500), "0.5s", id="half-second"),
        pytest.param(timedelta(seconds=60), "60s", id="minute"),
        pytest.param(timedelta(microseconds=1), "0.000001s", id="microsecond"),
        pytest.param(MAX_DURATION, "3153600000s", id="max"),
    ],
)
def test_describe_duration_written_in_plain_seconds(
    duration: timedelta, text: str
) -> None:
    """A TTL is whole seconds when it is whole, never in exponent form."""
    # Act
    written = _seconds_text(duration)

    # Assert
    assert written == text


def test_describe_idempotent_window_of_thirty_days_written_in_seconds() -> None:
    """A thirty-day replay window reads as plain seconds."""
    # Arrange
    component = SimpleNamespace(
        config=SimpleNamespace(methods=("POST",), include=(), exclude=()),
        _key_maker=None,
        route_is_gated=lambda method, path: False,  # noqa: ARG005
        idempotency=SimpleNamespace(
            config=SimpleNamespace(ttl=timedelta(days=30))
        ),
    )
    endpoint = _Endpoint(method="POST", path="/orders", route=None, contexts=())

    # Act
    applied = _reads_idempotent(component)(endpoint)

    # Assert
    assert applied == "idempotent 2592000s"
