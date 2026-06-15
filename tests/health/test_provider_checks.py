"""Tests for provider readiness checks (`add_provider`, `auto_health`)."""

from typing import Self

import pytest

from grelmicro import Grelmicro
from grelmicro.health._checks import HealthChecks
from grelmicro.health._models import HealthStatus
from grelmicro.providers._base import Provider

pytestmark = [pytest.mark.timeout(1)]


class FakeProvider(Provider):
    """Minimal provider with a stub readiness check, no real backend."""

    def __init__(
        self, short_name: str = "fake", *, healthy: bool = True
    ) -> None:
        """Record the vendor name and the desired check outcome."""
        self.short_name = short_name  # type: ignore[misc]  # ty: ignore[invalid-attribute-access]
        self._healthy = healthy
        self.calls = 0

    async def check(self) -> None:
        """Count the call and raise when configured unhealthy."""
        self.calls += 1
        if not self._healthy:
            msg = "backend down"
            raise ConnectionError(msg)

    async def __aenter__(self) -> Self:
        """Open the provider (no-op)."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the provider (no-op)."""


class NoCheckProvider(Provider):
    """Provider without a readiness check, like a backend-less provider."""

    short_name = "nocheck"

    async def __aenter__(self) -> Self:
        """Open the provider (no-op)."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the provider (no-op)."""


class TestAddProvider:
    """Tests for `HealthChecks.add_provider`."""

    async def test_registers_provider_short_name(self) -> None:
        """The check is registered under `provider:{short_name}`."""
        health = HealthChecks(cache_ttl=0)
        provider = FakeProvider("redis")

        health.add_provider(provider)

        report = await health.run()
        assert "provider:redis" in report["checks"]
        assert report["checks"]["provider:redis"]["status"] == HealthStatus.OK
        assert provider.calls == 1

    async def test_critical_by_default(self) -> None:
        """An unreachable provider flips the aggregate to error."""
        health = HealthChecks(cache_ttl=0)
        health.add_provider(FakeProvider("redis", healthy=False))

        report = await health.run()
        assert report["checks"]["provider:redis"]["critical"] is True
        assert report["status"] == HealthStatus.ERROR

    async def test_critical_false_does_not_flip_aggregate(self) -> None:
        """A non-critical provider failure stays out of the aggregate."""
        health = HealthChecks(cache_ttl=0)
        health.add_provider(
            FakeProvider("cache", healthy=False), critical=False
        )

        report = await health.run()
        assert report["checks"]["provider:cache"]["critical"] is False
        assert report["status"] == HealthStatus.OK

    async def test_name_override(self) -> None:
        """An explicit name registers `provider:{name}` instead."""
        health = HealthChecks(cache_ttl=0)
        health.add_provider(FakeProvider("redis"), name="sessions")

        report = await health.run()
        assert "provider:sessions" in report["checks"]
        assert "provider:redis" not in report["checks"]

    async def test_timeout_override(self) -> None:
        """An explicit timeout is applied to the registered check."""
        timeout = 2.0
        health = HealthChecks(cache_ttl=0)
        health.add_provider(FakeProvider("redis"), timeout=timeout)

        assert health._entries["provider:redis"].timeout == timeout

    def test_provider_without_check_raises(self) -> None:
        """A provider that ships no readiness check raises ValueError."""
        health = HealthChecks()
        with pytest.raises(ValueError, match="no readiness check"):
            health.add_provider(NoCheckProvider())

    async def test_base_check_raises_not_implemented(self) -> None:
        """The base `Provider.check` raises `NotImplementedError`."""
        with pytest.raises(NotImplementedError, match="no readiness check"):
            await NoCheckProvider().check()

    def test_duplicate_name_raises(self) -> None:
        """Registering the same provider name twice raises."""
        health = HealthChecks()
        health.add_provider(FakeProvider("redis"))
        with pytest.raises(ValueError, match="already registered"):
            health.add_provider(FakeProvider("redis"))


class TestAutoHealth:
    """Tests for `HealthChecks(auto_health=True)`."""

    async def test_registers_every_active_provider(self) -> None:
        """Every active provider gets a critical `provider:{short_name}` check."""
        redis = FakeProvider("redis")
        postgres = FakeProvider("postgres")
        health = HealthChecks(cache_ttl=0, auto_health=True)
        micro = Grelmicro(uses=[redis, postgres, health])

        async with micro:
            report = await health.run()

        assert set(report["checks"]) == {"provider:redis", "provider:postgres"}
        assert all(c["critical"] for c in report["checks"].values())
        assert redis.calls == 1
        assert postgres.calls == 1

    async def test_off_by_default(self) -> None:
        """Without the flag, no provider checks are registered."""
        health = HealthChecks(cache_ttl=0)
        micro = Grelmicro(uses=[FakeProvider("redis"), health])

        async with micro:
            report = await health.run()

        assert report["checks"] == {}

    async def test_skips_provider_without_check(self) -> None:
        """A provider with no readiness check is skipped, not an error."""
        health = HealthChecks(cache_ttl=0, auto_health=True)
        micro = Grelmicro(
            uses=[NoCheckProvider(), FakeProvider("redis"), health]
        )

        async with micro:
            report = await health.run()

        assert set(report["checks"]) == {"provider:redis"}

    async def test_duplicate_vendor_skipped_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A second provider of the same vendor is skipped with a warning."""
        health = HealthChecks(cache_ttl=0, auto_health=True)
        micro = Grelmicro(
            uses=[FakeProvider("redis"), FakeProvider("redis"), health]
        )

        with caplog.at_level("WARNING", logger="grelmicro.health"):
            async with micro:
                report = await health.run()

        assert set(report["checks"]) == {"provider:redis"}
        assert "already registered" in caplog.text

    async def test_explicit_registration_wins_without_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An explicit add_provider keeps its settings and warns nothing."""
        redis = FakeProvider("redis")
        health = HealthChecks(cache_ttl=0, auto_health=True)
        health.add_provider(redis, critical=False)
        micro = Grelmicro(uses=[redis, health])

        with caplog.at_level("WARNING", logger="grelmicro.health"):
            async with micro:
                report = await health.run()

        assert report["checks"]["provider:redis"]["critical"] is False
        assert "already registered" not in caplog.text


class TestProvidersProperty:
    """Tests for `Grelmicro.providers`."""

    def test_lists_providers_deduped_by_identity(self) -> None:
        """Listed providers appear once, in registration order.

        A `HealthChecks` component is present so the bare providers are
        lifecycle-only (no default-component registration).
        """
        redis = FakeProvider("redis")
        postgres = FakeProvider("postgres")
        micro = Grelmicro(uses=[redis, postgres, redis, HealthChecks()])

        assert micro.providers == (redis, postgres)

    def test_empty_when_no_providers(self) -> None:
        """An app with no providers reports an empty tuple."""
        micro = Grelmicro(uses=[HealthChecks()])
        assert micro.providers == ()
