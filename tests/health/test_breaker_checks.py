"""Tests for circuit breaker health checks (`add_breaker`)."""

from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from starlette.status import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE

from grelmicro.health import HealthChecks, health_asgi
from grelmicro.health._models import HealthStatus
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.resilience import CircuitBreaker, CircuitBreakerState
from grelmicro.resilience.circuitbreaker import _TransitionCause
from grelmicro.resilience.circuitbreaker.memory import (
    MemoryCircuitBreakerAdapter,
)
from grelmicro.resilience.circuitbreaker.sqlite import (
    SQLiteCircuitBreakerAdapter,
)

pytestmark = [pytest.mark.timeout(1)]

RESET_TIMEOUT = timedelta(seconds=12)
"""Cool-down of the breakers under test."""


class BoomError(Exception):
    """Error the tripping calls raise."""


def make_breaker(
    name: str = "inverter",
    backend: MemoryCircuitBreakerAdapter
    | SQLiteCircuitBreakerAdapter
    | None = None,
) -> CircuitBreaker:
    """Return a breaker that opens on its first error."""
    return CircuitBreaker.consecutive_count(
        name,
        error_threshold=1,
        success_threshold=1,
        reset_timeout=RESET_TIMEOUT,
        backend=backend or MemoryCircuitBreakerAdapter(),
    )


async def trip(breaker: CircuitBreaker) -> None:
    """Open `breaker` with one failing call."""
    with pytest.raises(BoomError):
        async with breaker:
            raise BoomError


def client_for(health: HealthChecks) -> httpx.AsyncClient:
    """Return a client for the app serving `health`."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=health_asgi(health)),
        base_url="http://probe",
    )


class TestRegistration:
    """Tests for the name, critical flag and timeout of `add_breaker`."""

    def test_name_defaults_to_breaker_name(self) -> None:
        """The check is registered as `circuitbreaker:{breaker.name}`."""
        # Arrange
        health = HealthChecks(cache_ttl=0)

        # Act
        health.add_breaker(make_breaker("inverter"))

        # Assert
        assert list(health._entries) == ["circuitbreaker:inverter"]

    def test_name_overrides_the_suffix(self) -> None:
        """`name=` replaces the breaker name in the check name."""
        # Arrange
        health = HealthChecks(cache_ttl=0)

        # Act
        health.add_breaker(make_breaker("inverter"), name="solar")

        # Assert
        assert list(health._entries) == ["circuitbreaker:solar"]

    def test_invalid_name_points_at_name_argument(self) -> None:
        """A breaker name that makes an invalid check name asks for `name=`."""
        # Arrange
        health = HealthChecks(cache_ttl=0)

        # Act / Assert
        with pytest.raises(ValueError, match="name="):
            health.add_breaker(make_breaker("Inverter API"))
        assert health._entries == {}

    def test_too_long_name_points_at_name_argument(self) -> None:
        """A check name over 64 chars asks for `name=`."""
        # Arrange
        health = HealthChecks(cache_ttl=0)

        # Act / Assert
        with pytest.raises(ValueError, match="name="):
            health.add_breaker(make_breaker("a" * 60))

    def test_duplicate_is_refused(self) -> None:
        """A second breaker under the same check name is refused."""
        # Arrange
        health = HealthChecks(cache_ttl=0)
        health.add_breaker(make_breaker("inverter"))

        # Act / Assert
        with pytest.raises(ValueError, match="already registered"):
            health.add_breaker(make_breaker("inverter"))

    def test_non_critical_by_default(self) -> None:
        """The check is non-critical unless `critical=True` is passed."""
        # Arrange
        health = HealthChecks(cache_ttl=0)

        # Act
        health.add_breaker(make_breaker("inverter"))
        health.add_breaker(make_breaker("heater"), critical=True)

        # Assert
        assert health._entries["circuitbreaker:inverter"].critical is False
        assert health._entries["circuitbreaker:heater"].critical is True

    def test_timeout_is_passed_through(self) -> None:
        """An explicit timeout is applied to the registered check."""
        # Arrange
        health = HealthChecks(cache_ttl=0)
        timeout = 2.0

        # Act
        health.add_breaker(make_breaker(), timeout=timeout)

        # Assert
        assert health._entries["circuitbreaker:inverter"].timeout == timeout

    def test_keyed_circuit_includes_the_key(self) -> None:
        """A per-key circuit is registered under `breaker:key`."""
        # Arrange
        health = HealthChecks(cache_ttl=0)

        # Act
        health.add_breaker(make_breaker("inverter").keyed("roof"))

        # Assert
        assert list(health._entries) == ["circuitbreaker:inverter:roof"]


class TestStatus:
    """Tests for what each breaker state reports."""

    async def test_closed_is_ok(self) -> None:
        """A closed breaker reports ok with no details."""
        # Arrange
        health = HealthChecks(cache_ttl=0)
        health.add_breaker(make_breaker())

        # Act
        report = await health.run()

        # Assert
        result = report["checks"]["circuitbreaker:inverter"]
        assert result["status"] == HealthStatus.OK
        assert result["error"] is None
        assert result["details"] is None

    async def test_open_fails_with_message_and_details(self) -> None:
        """An open breaker fails with the time to its next probe."""
        # Arrange
        breaker = make_breaker()
        await trip(breaker)
        health = HealthChecks(cache_ttl=0)
        health.add_breaker(breaker)

        # Act
        report = await health.run()

        # Assert
        result = report["checks"]["circuitbreaker:inverter"]
        assert result["status"] == HealthStatus.ERROR
        assert result["error"] == "circuit breaker open, next probe in 12s"
        assert result["details"] == {"state": "OPEN", "retry_after": 12}

    async def test_forced_open_fails_until_reset(self) -> None:
        """An isolated breaker fails and says only a reset releases it."""
        # Arrange
        breaker = make_breaker()
        await breaker.isolate()
        health = HealthChecks(cache_ttl=0)
        health.add_breaker(breaker)

        # Act
        report = await health.run()

        # Assert
        result = report["checks"]["circuitbreaker:inverter"]
        assert result["status"] == HealthStatus.ERROR
        assert result["error"] == "circuit breaker forced open until reset"
        assert result["details"] == {"state": "FORCED_OPEN"}

    async def test_half_open_is_ok_with_details(self) -> None:
        """A half-open breaker reports ok with its state in the details."""
        # Arrange
        breaker = make_breaker()
        await breaker._transition(
            CircuitBreakerState.HALF_OPEN, _TransitionCause.MANUAL
        )
        health = HealthChecks(cache_ttl=0)
        health.add_breaker(breaker)

        # Act
        report = await health.run()

        # Assert
        result = report["checks"]["circuitbreaker:inverter"]
        assert result["status"] == HealthStatus.OK
        assert result["details"] == {"state": "HALF_OPEN"}

    async def test_forced_closed_is_ok_with_details(self) -> None:
        """A forced-closed breaker reports ok with its state in the details."""
        # Arrange
        breaker = make_breaker()
        await breaker._transition(
            CircuitBreakerState.FORCED_CLOSED, _TransitionCause.MANUAL
        )
        health = HealthChecks(cache_ttl=0)
        health.add_breaker(breaker)

        # Act
        report = await health.run()

        # Assert
        result = report["checks"]["circuitbreaker:inverter"]
        assert result["status"] == HealthStatus.OK
        assert result["details"] == {"state": "FORCED_CLOSED"}

    async def test_reads_backend_state_not_the_cache(self) -> None:
        """An idle breaker reports open when another instance opened it."""
        # Arrange
        backend = MemoryCircuitBreakerAdapter()
        idle = make_breaker(backend=backend)
        await trip(make_breaker(backend=backend))
        health = HealthChecks(cache_ttl=0)
        health.add_breaker(idle)

        # Act
        report = await health.run()

        # Assert
        assert idle.state is CircuitBreakerState.CLOSED
        result = report["checks"]["circuitbreaker:inverter"]
        assert result["status"] == HealthStatus.ERROR

    async def test_check_leaves_the_cached_state_alone(self) -> None:
        """Running the check does not refresh the breaker's local view."""
        # Arrange
        backend = MemoryCircuitBreakerAdapter()
        idle = make_breaker(backend=backend)
        await trip(make_breaker(backend=backend))
        health = HealthChecks(cache_ttl=0)
        health.add_breaker(idle)

        # Act
        await health.run()

        # Assert
        assert idle.state is CircuitBreakerState.CLOSED


class TestEndpoints:
    """Tests for how a breaker check shows on `/readyz` and `/healthz`."""

    async def test_non_critical_open_keeps_readyz_up(self) -> None:
        """An open non-critical breaker passes /readyz and shows in /healthz."""
        # Arrange
        breaker = make_breaker()
        await trip(breaker)
        health = HealthChecks(cache_ttl=0)
        health.add_breaker(breaker)

        # Act
        async with client_for(health) as client:
            readyz = await client.get("/readyz")
            healthz = await client.get("/healthz")

        # Assert
        assert readyz.status_code == HTTP_200_OK
        assert healthz.status_code == HTTP_200_OK
        assert healthz.json()["checks"]["circuitbreaker:inverter"] == {
            "status": "error",
            "critical": False,
            "error": "circuit breaker open, next probe in 12s",
        }

    async def test_critical_open_fails_readyz(self) -> None:
        """An open critical breaker fails /readyz."""
        # Arrange
        breaker = make_breaker("heater")
        await trip(breaker)
        health = HealthChecks(cache_ttl=0)
        health.add_breaker(breaker, critical=True)

        # Act
        async with client_for(health) as client:
            readyz = await client.get("/readyz")

        # Assert
        assert readyz.status_code == HTTP_503_SERVICE_UNAVAILABLE


class TestSharedBackend:
    """Tests for a breaker whose state lives in a shared backend."""

    async def test_idle_replica_reports_open(self, tmp_path: Path) -> None:
        """A breaker that made no call reports the open state it shares."""
        # Arrange
        provider = SQLiteProvider(str(tmp_path / "cb.db"))
        async with (
            provider,
            SQLiteCircuitBreakerAdapter(provider=provider) as backend,
        ):
            await trip(make_breaker(backend=backend))
            idle = make_breaker(backend=backend)
            health = HealthChecks(cache_ttl=0)
            health.add_breaker(idle)

            # Act
            report = await health.run()

        # Assert
        assert idle.state is CircuitBreakerState.CLOSED
        result = report["checks"]["circuitbreaker:inverter"]
        assert result["status"] == HealthStatus.ERROR
        assert result["error"] == "circuit breaker open, next probe in 12s"
