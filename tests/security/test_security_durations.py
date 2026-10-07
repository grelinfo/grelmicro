"""Security durations take whole seconds or a `timedelta`, never a float.

Bans, key set TTLs, the verified token cache and outbound token refresh are
checked at their exact boundaries on a clock the test moves, in whole
nanoseconds.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from pydantic import ValidationError

from grelmicro._duration import (
    MAX_DURATION,
    MICROSECOND,
    NANOSECONDS_PER_SECOND,
)
from grelmicro.errors import SettingsValidationError
from grelmicro.security import (
    ClientAuth,
    ClientBans,
    ClientBansConfig,
    DiscoveryConfig,
    JWKSConfig,
    JWTKey,
    JWTKeysConfig,
    JWTVerifier,
    OAuthClient,
    OAuthClientConfig,
)
from grelmicro.security import bans as bans_module
from grelmicro.security import jwt as jwt_module
from tests._durations import DURATIONS, FLOATS, NANOSECOND, NanosecondClock
from tests.security.jwt_signing import Signer

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = [pytest.mark.timeout(10)]

URL = "https://idp.example.com/.well-known/jwks.json"
ISSUER = "https://auth.grel.info/"
AUDIENCE = "grelmicro-api"
TOKEN_ENDPOINT = "https://auth.example.com/oauth2/token"
CLIENT = "203.0.113.9"
FAILURES = 3
HOUR = 3600
WHOLE = 12
UNDER_A_SECOND = timedelta(seconds=12, milliseconds=500)

SIGNER = Signer()

READ_BACK = [
    pytest.param(WHOLE, timedelta(seconds=WHOLE), id="int"),
    pytest.param(UNDER_A_SECOND, UNDER_A_SECOND, id="timedelta"),
]

FROM_TEXT = [
    pytest.param("12", timedelta(seconds=WHOLE), id="seconds"),
    pytest.param("PT12.5S", UNDER_A_SECOND, id="iso-8601"),
]


def _key() -> JWTKey:
    return JWTKey.pem(SIGNER.public_pem("RS256"), algorithm="RS256")


def _keys_config(**settings: Any) -> JWTKeysConfig:  # noqa: ANN401
    return JWTKeysConfig(keys=[_key()], audience=AUDIENCE, **settings)


def _jwks_config(**settings: Any) -> JWKSConfig:  # noqa: ANN401
    return JWKSConfig(url=URL, audience=AUDIENCE, **settings)


def _discovery_config(**settings: Any) -> DiscoveryConfig:  # noqa: ANN401
    return DiscoveryConfig(issuer=ISSUER, audience=AUDIENCE, **settings)


def _oauth_config(**settings: Any) -> OAuthClientConfig:  # noqa: ANN401
    return OAuthClientConfig(
        client_id="orders-api", token_endpoint=TOKEN_ENDPOINT, **settings
    )


def _bans(**settings: Any) -> ClientBans:  # noqa: ANN401
    return ClientBans(env_load=False, **settings)


def _verifier_keys(**settings: Any) -> JWTVerifier:  # noqa: ANN401
    return JWTVerifier.keys(
        _key(), audience=AUDIENCE, env_load=False, **settings
    )


def _verifier_jwks(**settings: Any) -> JWTVerifier:  # noqa: ANN401
    return JWTVerifier.jwks(URL, audience=AUDIENCE, env_load=False, **settings)


def _verifier_discover(**settings: Any) -> JWTVerifier:  # noqa: ANN401
    return JWTVerifier.discover(
        ISSUER, audience=AUDIENCE, env_load=False, **settings
    )


def _oauth_client(**settings: Any) -> OAuthClient:  # noqa: ANN401
    return OAuthClient.endpoint(
        TOKEN_ENDPOINT,
        client_id="orders-api",
        client_auth=ClientAuth.secret("s3cr3t-value"),
        env_load=False,
        **settings,
    )


CONFIGS: list[Any] = [
    pytest.param(ClientBansConfig, "window", id="bans-window"),
    pytest.param(ClientBansConfig, "duration", id="bans-duration"),
    pytest.param(_keys_config, "cache_ttl", id="keys-cache-ttl"),
    pytest.param(_jwks_config, "ttl", id="jwks-ttl"),
    pytest.param(_jwks_config, "cache_ttl", id="jwks-cache-ttl"),
    pytest.param(_discovery_config, "ttl", id="discovery-ttl"),
    pytest.param(_discovery_config, "cache_ttl", id="discovery-cache-ttl"),
    pytest.param(_oauth_config, "refresh_before", id="oauth-refresh-before"),
    pytest.param(
        _oauth_config, "default_lifetime", id="oauth-default-lifetime"
    ),
]

CACHE_TTLS: list[Any] = [
    pytest.param(_keys_config, id="keys"),
    pytest.param(_jwks_config, id="jwks"),
    pytest.param(_discovery_config, id="discovery"),
]

NOT_ZERO: list[Any] = [
    param for param in CONFIGS if param.values[1] != "cache_ttl"
]

COMPONENTS: list[Any] = [
    pytest.param(_bans, "window", id="bans-window"),
    pytest.param(_bans, "duration", id="bans-duration"),
    pytest.param(_verifier_keys, "cache_ttl", id="keys-cache-ttl"),
    pytest.param(_verifier_jwks, "ttl", id="jwks-ttl"),
    pytest.param(_verifier_jwks, "cache_ttl", id="jwks-cache-ttl"),
    pytest.param(_verifier_discover, "ttl", id="discovery-ttl"),
    pytest.param(_verifier_discover, "cache_ttl", id="discovery-cache-ttl"),
    pytest.param(_oauth_client, "refresh_before", id="oauth-refresh-before"),
    pytest.param(
        _oauth_client, "default_lifetime", id="oauth-default-lifetime"
    ),
]

ENVIRONMENT: list[Any] = [
    pytest.param(
        lambda: ClientBans(env_load=True),
        "GREL_CLIENTBANS_WINDOW",
        "window",
        id="bans-window",
    ),
    pytest.param(
        lambda: ClientBans(env_load=True),
        "GREL_CLIENTBANS_DURATION",
        "duration",
        id="bans-duration",
    ),
    pytest.param(
        lambda: JWTVerifier.keys(_key(), audience=AUDIENCE, env_load=True),
        "GREL_JWTVERIFIER_CACHE_TTL",
        "cache_ttl",
        id="keys-cache-ttl",
    ),
    pytest.param(
        lambda: JWTVerifier.jwks(URL, audience=AUDIENCE, env_load=True),
        "GREL_JWTVERIFIER_TTL",
        "ttl",
        id="jwks-ttl",
    ),
    pytest.param(
        lambda: OAuthClient.endpoint(
            TOKEN_ENDPOINT,
            client_id="orders-api",
            client_auth=ClientAuth.secret("s3cr3t-value"),
            env_load=True,
        ),
        "GREL_OAUTHCLIENT_REFRESH_BEFORE",
        "refresh_before",
        id="oauth-refresh-before",
    ),
    pytest.param(
        lambda: OAuthClient.endpoint(
            TOKEN_ENDPOINT,
            client_id="orders-api",
            client_auth=ClientAuth.secret("s3cr3t-value"),
            env_load=True,
        ),
        "GREL_OAUTHCLIENT_DEFAULT_LIFETIME",
        "default_lifetime",
        id="oauth-default-lifetime",
    ),
]


@pytest.fixture
def ban_clock(monkeypatch: pytest.MonkeyPatch) -> NanosecondClock:
    """Pin the clock ban tables read."""
    clock = NanosecondClock()
    monkeypatch.setattr(bans_module, "monotonic_ns", clock)
    return clock


@pytest.fixture
def wall_clock(monkeypatch: pytest.MonkeyPatch) -> NanosecondClock:
    """Pin the wall clock the verified token cache reads."""
    clock = NanosecondClock()
    clock.now = time.time_ns()
    monkeypatch.setattr(jwt_module, "time_ns", clock)
    return clock


def _banned(table: ClientBans) -> None:
    for _ in range(FAILURES):
        table.record(CLIENT, "signature")


async def _fetch(
    url: str,  # noqa: ARG001
    *,
    timeout: float,  # noqa: ARG001, ASYNC109
    max_bytes: int,  # noqa: ARG001
) -> bytes:
    return json.dumps({"keys": [SIGNER.public_jwk("RS256", kid="k1")]}).encode()


# --- Public API ---


@pytest.mark.parametrize(("config", "field"), CONFIGS)
@pytest.mark.parametrize("value", FLOATS)
def test_security_config_float_duration_refused(
    config: Callable[..., Any], field: str, value: object
) -> None:
    """A float or a bool duration is refused when the config is built."""
    # Act / Assert
    with pytest.raises(
        ValidationError, match=f"{field} must be whole seconds or a timedelta"
    ):
        config(**{field: value})


@pytest.mark.parametrize(("config", "field"), NOT_ZERO)
@pytest.mark.parametrize(
    "value", [0, timedelta(0), -1, timedelta(microseconds=-1)]
)
def test_security_config_duration_of_zero_or_less_refused(
    config: Callable[..., Any], field: str, value: object
) -> None:
    """A duration of zero or less is refused when the config is built."""
    # Act / Assert
    with pytest.raises(
        ValidationError, match=f"{field} must be greater than zero"
    ):
        config(**{field: value})


@pytest.mark.parametrize("config", CACHE_TTLS)
@pytest.mark.parametrize("value", [0, timedelta(0)])
def test_jwt_verifier_config_cache_ttl_zero_accepted_as_cache_off(
    config: Callable[..., Any], value: int | timedelta
) -> None:
    """`cache_ttl=0` is taken, meaning the cache is off."""
    # Act
    built = config(cache_ttl=value)

    # Assert
    assert built.cache_ttl == timedelta(0)


@pytest.mark.parametrize("config", CACHE_TTLS)
@pytest.mark.parametrize("value", [-1, timedelta(microseconds=-1)])
def test_jwt_verifier_config_negative_cache_ttl_refused(
    config: Callable[..., Any], value: int | timedelta
) -> None:
    """A negative `cache_ttl` is refused when the config is built."""
    # Act / Assert
    with pytest.raises(ValidationError, match="cache_ttl must not be negative"):
        config(cache_ttl=value)


WAITS: list[Any] = [
    pytest.param(_jwks_config, "retry_interval", id="jwks-retry-interval"),
    pytest.param(_jwks_config, "timeout", id="jwks-timeout"),
    pytest.param(
        _discovery_config, "retry_interval", id="discovery-retry-interval"
    ),
    pytest.param(_discovery_config, "timeout", id="discovery-timeout"),
    pytest.param(_oauth_config, "retry_interval", id="oauth-retry-interval"),
    pytest.param(_oauth_config, "timeout", id="oauth-timeout"),
]


@pytest.mark.parametrize(("config", "field"), WAITS)
@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "inf", "minus-inf"],
)
def test_security_config_non_finite_wait_refused(
    config: Callable[..., Any], field: str, value: float
) -> None:
    """A wait that is not a finite number is refused, naming the field."""
    # Act / Assert
    with pytest.raises(
        ValidationError, match=f"{field} must be a finite number"
    ):
        config(**{field: value})


@pytest.mark.parametrize("raw", ["0", "PT0S"])
def test_jwt_verifier_zero_cache_ttl_from_environment_turns_cache_off(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env text `"0"` or `"PT0S"` turns the verified token cache off."""
    # Arrange
    monkeypatch.setenv("GREL_JWTVERIFIER_CACHE_TTL", raw)
    verifier = JWTVerifier.keys(_key(), audience=AUDIENCE, env_load=True)
    token = _signed()

    # Act
    first = verifier.verify(token)
    again = verifier.verify(token)

    # Assert
    assert verifier.config.cache_ttl == timedelta(0)
    assert again is not first


@pytest.mark.parametrize(("config", "field"), CONFIGS)
def test_security_config_duration_over_a_hundred_years_refused(
    config: Callable[..., Any], field: str
) -> None:
    """A duration over a hundred years is refused when the config is built."""
    # Act / Assert
    with pytest.raises(
        ValidationError, match=f"{field} must be at most 100 years"
    ):
        config(**{field: MAX_DURATION + MICROSECOND})


@pytest.mark.parametrize(("config", "field"), CONFIGS)
@pytest.mark.parametrize(("value", "expected"), READ_BACK)
def test_security_config_duration_reads_back_as_timedelta(
    config: Callable[..., Any],
    field: str,
    value: int | timedelta,
    expected: timedelta,
) -> None:
    """Whole seconds and a `timedelta` read back as a `timedelta`."""
    # Act
    built = config(**{field: value})

    # Assert
    assert getattr(built, field) == expected


@pytest.mark.parametrize(("component", "field"), COMPONENTS)
@pytest.mark.parametrize("value", FLOATS)
def test_security_component_float_duration_refused(
    component: Callable[..., Any], field: str, value: object
) -> None:
    """A float or a bool keyword is refused, naming the setting."""
    # Act / Assert
    with pytest.raises(
        SettingsValidationError,
        match=f"{field} must be whole seconds or a timedelta",
    ):
        component(**{field: value})


@pytest.mark.parametrize(("component", "field"), COMPONENTS)
@pytest.mark.parametrize(("value", "expected"), READ_BACK)
def test_security_component_duration_reads_back_as_timedelta(
    component: Callable[..., Any],
    field: str,
    value: int | timedelta,
    expected: timedelta,
) -> None:
    """A keyword in whole seconds or a `timedelta` reads back as a `timedelta`."""
    # Act
    built = component(**{field: value})

    # Assert
    assert getattr(built.config, field) == expected


@pytest.mark.parametrize(("build", "variable", "field"), ENVIRONMENT)
@pytest.mark.parametrize(("raw", "expected"), FROM_TEXT)
def test_security_component_duration_from_environment_reads_as_timedelta(
    build: Callable[[], Any],
    variable: str,
    field: str,
    raw: str,
    expected: timedelta,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env text is whole seconds or an ISO 8601 duration."""
    # Arrange
    monkeypatch.setenv(variable, raw)

    # Act
    built = build()

    # Assert
    assert getattr(built.config, field) == expected


@pytest.mark.parametrize(("build", "variable", "field"), ENVIRONMENT)
def test_security_component_decimal_duration_from_environment_refused_without_echo(
    build: Callable[[], Any],
    variable: str,
    field: str,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decimal number of seconds is refused, and the error never repeats it."""
    # Arrange
    monkeypatch.setenv(variable, "4321.5")

    # Act
    with pytest.raises(SettingsValidationError) as refused:
        build()

    # Assert
    assert "4321.5" not in str(refused.value)


def test_client_bans_config_defaults_are_a_minute_and_five_minutes() -> None:
    """The window is a minute and a ban lasts five minutes, as `timedelta`s."""
    # Act
    config = ClientBansConfig()

    # Assert
    assert config.window == timedelta(minutes=1)
    assert config.duration == timedelta(minutes=5)


def test_key_publishing_defaults_are_an_hour_and_five_minutes() -> None:
    """A key set is current for an hour and a verified token cached five minutes."""
    # Act
    config = _jwks_config()

    # Assert
    assert config.ttl == timedelta(hours=1)
    assert config.cache_ttl == timedelta(minutes=5)


def test_oauth_client_config_defaults_are_a_minute_and_five_minutes() -> None:
    """A token refreshes a minute ahead and lives five minutes when unsaid."""
    # Act
    config = _oauth_config()

    # Assert
    assert config.refresh_before == timedelta(minutes=1)
    assert config.default_lifetime == timedelta(minutes=5)


def test_jwks_config_dumps_ttl_as_iso_8601() -> None:
    """A key set TTL dumps to JSON as ISO 8601 and reads back exactly."""
    # Arrange
    config = _jwks_config(ttl=timedelta(milliseconds=1500))

    # Act
    dumped = config.model_dump(mode="json")

    # Assert
    assert dumped["ttl"] == "PT1.5S"
    assert JWKSConfig.model_validate(dumped).ttl == config.ttl


# --- Bans ---


@given(duration=DURATIONS)
def test_client_bans_ban_lasts_its_whole_duration(duration: timedelta) -> None:
    """A ban holds until its duration has passed, to the nanosecond."""
    # Arrange
    clock = NanosecondClock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(bans_module, "monotonic_ns", clock)
        table = _bans(failures=FAILURES, duration=duration)
        _banned(table)

        # Act
        clock.advance(duration, -NANOSECOND)
        held = table.banned(CLIENT)
        clock.advance(timedelta(0), NANOSECOND)
        lifted = not table.banned(CLIENT)

    # Assert
    assert held
    assert lifted


@given(window=DURATIONS)
def test_client_bans_failures_count_for_their_whole_window(
    window: timedelta,
) -> None:
    """A failure counts until its window has passed, to the nanosecond."""
    # Arrange
    clock = NanosecondClock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(bans_module, "monotonic_ns", clock)
        table = _bans(failures=2, window=window)
        table.record(CLIENT, "signature")

        # Act
        clock.advance(window, -NANOSECOND)
        banned = table.record(CLIENT, "signature")

    # Assert
    assert banned


def test_client_bans_failure_past_its_window_starts_over(
    ban_clock: NanosecondClock,
) -> None:
    """A failure a whole window old no longer counts."""
    # Arrange
    window = timedelta(milliseconds=1)
    table = _bans(failures=2, window=window)
    table.record(CLIENT, "signature")

    # Act
    ban_clock.advance(window)
    banned = table.record(CLIENT, "signature")

    # Assert
    assert not banned


@pytest.mark.usefixtures("ban_clock")
def test_client_bans_ban_of_one_microsecond_bans() -> None:
    """A ban of one microsecond is a ban, not zero."""
    # Arrange
    table = _bans(failures=FAILURES, duration=MICROSECOND)

    # Act
    _banned(table)

    # Assert
    assert table.banned(CLIENT)


@pytest.mark.usefixtures("ban_clock")
def test_client_bans_ban_of_one_microsecond_reports_one_microsecond_left() -> (
    None
):
    """A ban of one microsecond reports exactly that long left."""
    # Arrange
    table = _bans(failures=FAILURES, duration=MICROSECOND)

    # Act
    _banned(table)

    # Assert
    assert table.banned_for(CLIENT) == pytest.approx(1e-6)


def test_client_bans_banned_for_reports_the_exact_time_left(
    ban_clock: NanosecondClock,
) -> None:
    """The time left is the ban duration less what has passed."""
    # Arrange
    table = _bans(failures=FAILURES, duration=timedelta(seconds=300))
    _banned(table)

    # Act
    ban_clock.advance(timedelta(seconds=100))

    # Assert
    assert table.banned_for(CLIENT) == pytest.approx(200.0)


# --- Key set TTL ---


@settings(max_examples=50, deadline=None)
@given(ttl=DURATIONS)
def test_jwt_verifier_key_set_stale_exactly_at_its_ttl(
    ttl: timedelta,
) -> None:
    """A fetched key set is stale once its TTL has passed, never later."""
    # Arrange
    clock = NanosecondClock()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(jwt_module, "monotonic_ns", clock)
        verifier = JWTVerifier.from_config(_jwks_config(ttl=ttl), fetch=_fetch)
        asyncio.run(verifier.refresh())

        # Act
        clock.advance(ttl, -NANOSECOND)
        fresh = not verifier.stale
        clock.advance(timedelta(0), NANOSECOND)
        stale = verifier.stale

    # Assert
    assert fresh
    assert stale


# --- Verified token cache ---


def _signed(**claims: Any) -> str:  # noqa: ANN401
    now = int(time.time())
    payload = {"sub": "user-1", "aud": AUDIENCE, "exp": now + HOUR, "iat": now}
    payload.update(claims)
    return SIGNER.token(payload, algorithm="RS256")


def test_jwt_verifier_cache_entry_ends_exactly_at_its_ttl(
    wall_clock: NanosecondClock,
) -> None:
    """A cached token is answered from memory until its TTL, never past it."""
    # Arrange
    cache_ttl = timedelta(milliseconds=1500)
    verifier = _verifier_keys(cache_ttl=cache_ttl)
    token = _signed()
    first = verifier.verify(token)

    # Act
    wall_clock.advance(cache_ttl, -NANOSECOND)
    held = verifier.verify(token)
    wall_clock.advance(timedelta(0), NANOSECOND)
    again = verifier.verify(token)

    # Assert
    assert held is first
    assert again is not first


def test_jwt_verifier_cache_entry_never_outlives_the_token(
    wall_clock: NanosecondClock,
) -> None:
    """A token expiring before the TTL is served from memory until its `exp`, not after."""
    # Arrange
    exp = wall_clock.now // NANOSECONDS_PER_SECOND + 10
    verifier = _verifier_keys(cache_ttl=timedelta(days=1), leeway=0)
    token = _signed(exp=exp)
    first = verifier.verify(token)

    # Act
    wall_clock.now = exp * NANOSECONDS_PER_SECOND - NANOSECOND
    held = verifier.verify(token)
    wall_clock.now += NANOSECOND
    again = verifier.verify(token)

    # Assert
    assert held is first
    assert again is not first
