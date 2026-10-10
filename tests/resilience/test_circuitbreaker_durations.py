"""A circuit breaker reset timeout is whole seconds or a `timedelta`.

Each backend's timing is covered by its own suite. These tests pin the
public API and the exact values the Redis and Postgres strategies send.
"""

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from grelmicro._duration import microseconds
from grelmicro.errors import SettingsValidationError
from grelmicro.resilience import (
    CircuitBreaker,
    CircuitBreakerState,
    ConsecutiveCountConfig,
)
from grelmicro.resilience.circuitbreaker.postgres import (
    _PostgresConsecutiveCountStrategy,
)
from grelmicro.resilience.circuitbreaker.redis import (
    _RedisConsecutiveCountStrategy,
)

pytestmark = [pytest.mark.timeout(5)]

TWELVE_SECONDS = 12
"""A reset timeout in whole seconds."""

TWELVE_AND_A_HALF_SECONDS = timedelta(seconds=12, milliseconds=500)
"""A reset timeout that is not a whole number of seconds."""

NOT_WHOLE_MILLISECONDS = timedelta(seconds=1, microseconds=500_001)
"""A reset timeout that is not a whole number of milliseconds."""

DAY_SECONDS = 86_400
"""The idle lifetime of a stored circuit, in seconds."""

LIFETIME_PAST_A_DAY = timedelta(seconds=8_640, microseconds=1)
"""A reset timeout whose lifetime, ten times longer, is a day and 10 µs."""

TWO_DAYS = timedelta(days=2)
"""A manual cool-down longer than the configured one's lifetime."""

TWENTY_DAYS_SECONDS = 20 * DAY_SECONDS
"""The lifetime of a `TWO_DAYS` cool-down, ten times longer, in seconds."""

ERROR_THRESHOLD = 5
"""The default error threshold, sent with each recorded error."""


# --- The public API ---


@pytest.mark.parametrize("value", [12.5, 12.0, True])
def test_circuit_breaker_float_or_bool_reset_timeout_refused(
    value: float,
) -> None:
    """A float or a bool reset timeout is refused by the factory."""
    # Act / Assert
    with pytest.raises(
        SettingsValidationError, match="whole seconds or a timedelta"
    ):
        CircuitBreaker.consecutive_count(
            "cart",
            reset_timeout=value,  # ty: ignore[invalid-argument-type]
            env_load=False,
        )


@pytest.mark.parametrize("value", [12.5, 12.0, True])
def test_consecutive_count_config_float_or_bool_reset_timeout_refused(
    value: float,
) -> None:
    """A float or a bool reset timeout is refused by the config."""
    # Act / Assert
    with pytest.raises(ValidationError, match="whole seconds or a timedelta"):
        ConsecutiveCountConfig(reset_timeout=value)


@pytest.mark.parametrize("value", [0, timedelta(0), -1])
def test_circuit_breaker_reset_timeout_of_zero_or_less_refused(
    value: int | timedelta,
) -> None:
    """A reset timeout of zero or less is refused."""
    # Act / Assert
    with pytest.raises(SettingsValidationError, match="greater than zero"):
        CircuitBreaker.consecutive_count(
            "cart", reset_timeout=value, env_load=False
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (TWELVE_SECONDS, timedelta(seconds=TWELVE_SECONDS)),
        (TWELVE_AND_A_HALF_SECONDS, TWELVE_AND_A_HALF_SECONDS),
    ],
)
def test_circuit_breaker_reset_timeout_reads_back_as_timedelta(
    value: int | timedelta, expected: timedelta
) -> None:
    """Whole seconds and a `timedelta` read back as a `timedelta`."""
    # Act
    breaker = CircuitBreaker.consecutive_count(
        "cart", reset_timeout=value, env_load=False
    )

    # Assert
    assert breaker.config.reset_timeout == expected


def test_consecutive_count_config_reset_timeout_defaults_to_thirty_seconds() -> (
    None
):
    """The default reset timeout is thirty seconds, as a `timedelta`."""
    # Act
    config = ConsecutiveCountConfig()

    # Assert
    assert config.reset_timeout == timedelta(seconds=30)


def test_consecutive_count_config_reset_timeout_dumps_as_iso_8601() -> None:
    """A reset timeout dumps to JSON as ISO 8601 and reads back exactly."""
    # Arrange
    config = ConsecutiveCountConfig(reset_timeout=TWELVE_AND_A_HALF_SECONDS)

    # Act
    dumped = config.model_dump(mode="json")

    # Assert
    assert dumped["reset_timeout"] == "PT12.5S"
    assert ConsecutiveCountConfig.model_validate(dumped) == config


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12", timedelta(seconds=TWELVE_SECONDS)),
        ("PT12.5S", TWELVE_AND_A_HALF_SECONDS),
    ],
)
def test_circuit_breaker_reset_timeout_from_environment_reads_as_timedelta(
    raw: str, expected: timedelta, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env text is whole seconds or an ISO 8601 duration."""
    # Arrange
    monkeypatch.setenv("GREL_CIRCUITBREAKER_CART_RESET_TIMEOUT", raw)

    # Act
    breaker = CircuitBreaker.consecutive_count("cart", env_load=True)

    # Assert
    assert breaker.config.reset_timeout == expected


def test_circuit_breaker_decimal_reset_timeout_from_environment_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decimal number of seconds from the environment is refused."""
    # Arrange
    monkeypatch.setenv("GREL_CIRCUITBREAKER_CART_RESET_TIMEOUT", "0.5")

    # Act / Assert
    with pytest.raises(
        SettingsValidationError, match="whole seconds or an ISO 8601 duration"
    ):
        CircuitBreaker.consecutive_count("cart", env_load=True)


# --- Redis: whole microseconds to the scripts, lifetime up to the second ---


def _redis_strategy(
    reset_timeout: timedelta,
) -> tuple[_RedisConsecutiveCountStrategy, dict[str, AsyncMock]]:
    """Return a Redis strategy on a fake client, with its scripts by source."""
    scripts: dict[str, AsyncMock] = {}

    def register(source: str) -> AsyncMock:
        script = AsyncMock(return_value=["CLOSED", 0, 0, "0", "0"])
        scripts[source] = script
        return script

    client = MagicMock()
    client.register_script.side_effect = register
    strategy = _RedisConsecutiveCountStrategy(
        client=client,
        key="cb:cart",
        config=ConsecutiveCountConfig(reset_timeout=reset_timeout),
    )
    return strategy, scripts


def _args(script: AsyncMock) -> list[Any]:
    call = script.await_args
    assert call is not None
    return call.kwargs["args"]


async def test_redis_circuit_breaker_passes_reset_timeout_in_whole_microseconds() -> (
    None
):
    """Each script gets the reset timeout in whole microseconds."""
    # Arrange
    strategy, scripts = _redis_strategy(NOT_WHOLE_MILLISECONDS)

    # Act
    await strategy.record_outcome(success=False)

    # Assert
    record_error = scripts[_RedisConsecutiveCountStrategy._LUA_RECORD_ERROR]
    assert _args(record_error) == [
        ERROR_THRESHOLD,
        microseconds(NOT_WHOLE_MILLISECONDS),
        DAY_SECONDS,
    ]


async def test_redis_circuit_breaker_lifetime_rounds_up_to_the_second() -> None:
    """A lifetime past a whole second is rounded up, never down."""
    # Arrange
    strategy, scripts = _redis_strategy(LIFETIME_PAST_A_DAY)

    # Act
    await strategy.try_acquire()

    # Assert
    try_acquire = scripts[_RedisConsecutiveCountStrategy._LUA_TRY_ACQUIRE]
    assert _args(try_acquire)[2] == DAY_SECONDS + 1


async def test_redis_circuit_breaker_cool_down_gets_its_own_lifetime() -> None:
    """A manual cool-down longer than the config's keeps the key for it."""
    # Arrange
    strategy, scripts = _redis_strategy(timedelta(seconds=30))

    # Act
    await strategy.transition(
        desired=CircuitBreakerState.OPEN, cool_down=TWO_DAYS
    )

    # Assert
    transition = scripts[_RedisConsecutiveCountStrategy._LUA_TRANSITION]
    assert _args(transition) == [
        "OPEN",
        microseconds(TWO_DAYS),
        TWENTY_DAYS_SECONDS,
    ]


# --- Postgres: whole microseconds to the functions ---


def _postgres_strategy(
    reset_timeout: timedelta,
) -> tuple[_PostgresConsecutiveCountStrategy, MagicMock]:
    """Return a Postgres strategy on a fake pool."""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(
        return_value={
            "r_state": "CLOSED",
            "r_cerr": 0,
            "r_csucc": 0,
            "r_opened_at": 0.0,
            "r_retry_after": 0.0,
        }
    )
    pool.execute = AsyncMock()
    strategy = _PostgresConsecutiveCountStrategy(
        pool=pool,
        name="cb:cart",
        table_name="grelmicro_circuit_breaker",
        config=ConsecutiveCountConfig(reset_timeout=reset_timeout),
    )
    return strategy, pool


async def test_postgres_circuit_breaker_passes_reset_timeout_in_whole_microseconds() -> (
    None
):
    """The error function gets the reset timeout in whole microseconds."""
    # Arrange
    strategy, pool = _postgres_strategy(NOT_WHOLE_MILLISECONDS)

    # Act
    await strategy.record_outcome(success=False)

    # Assert
    assert pool.fetchrow.await_args.args[1:] == (
        "cb:cart",
        ERROR_THRESHOLD,
        microseconds(NOT_WHOLE_MILLISECONDS),
    )


async def test_postgres_circuit_breaker_passes_cool_down_in_whole_microseconds() -> (
    None
):
    """The transition function gets a manual cool-down in whole microseconds."""
    # Arrange
    strategy, pool = _postgres_strategy(timedelta(seconds=30))

    # Act
    await strategy.transition(
        desired=CircuitBreakerState.OPEN, cool_down=NOT_WHOLE_MILLISECONDS
    )

    # Assert
    assert pool.execute.await_args.args[1:] == (
        "cb:cart",
        "OPEN",
        microseconds(NOT_WHOLE_MILLISECONDS),
    )
