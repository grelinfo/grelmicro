"""Tests for provider readiness checks (`add_provider`, `provider_checks=`)."""

import asyncio
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Self, get_overloads

import httpx
import pytest
from starlette.status import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE

from grelmicro import Grelmicro, _deprecation
from grelmicro._deprecation import PENDING
from grelmicro.cache import Cache
from grelmicro.errors import GrelmicroFutureWarning, SettingsValidationError
from grelmicro.health import ProviderChecks, health_asgi
from grelmicro.health._checks import HealthChecks, HealthChecksConfig
from grelmicro.health._models import HealthStatus
from grelmicro.providers._base import Provider
from grelmicro.providers.sqlite import SQLiteProvider

pytestmark = [pytest.mark.timeout(1)]


class FakeProvider(Provider):
    """Minimal provider with a stub readiness check, no real backend."""

    def __init__(
        self, short_name: str = "fake", *, healthy: bool = True
    ) -> None:
        """Record the vendor name and the desired check outcome."""
        self.short_name = short_name  # ty: ignore[invalid-attribute-access]
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


def _client(health: HealthChecks) -> httpx.AsyncClient:
    """Return a client for the health endpoints serving `health`."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=health_asgi(health)),
        base_url="http://probe",
    )


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


class TestProviders:
    """Tests for `HealthChecks(provider_checks=...)`."""

    @pytest.mark.parametrize(
        ("providers", "critical"),
        [
            pytest.param("report", False, id="report"),
            pytest.param("require", True, id="require"),
        ],
    )
    async def test_registers_every_active_provider(
        self, providers: ProviderChecks, *, critical: bool
    ) -> None:
        """Every active provider gets a `provider:{short_name}` check."""
        redis = FakeProvider("redis")
        postgres = FakeProvider("postgres")
        health = HealthChecks(cache_ttl=0, provider_checks=providers)
        micro = Grelmicro(uses=[redis, postgres, health])

        async with micro:
            report = await health.run()

        assert set(report["checks"]) == {"provider:redis", "provider:postgres"}
        assert all(c["critical"] is critical for c in report["checks"].values())
        assert redis.calls == 1
        assert postgres.calls == 1

    async def test_off_registers_nothing(self) -> None:
        """`provider_checks="off"` registers no provider checks."""
        health = HealthChecks(cache_ttl=0, provider_checks="off")
        micro = Grelmicro(uses=[FakeProvider("redis"), health])

        async with micro:
            report = await health.run()

        assert report["checks"] == {}

    @pytest.mark.parametrize(
        ("spelling", "mode"),
        [
            pytest.param("OFF", "off", id="off"),
            pytest.param("Report", "report", id="report"),
            pytest.param("REQUIRE", "require", id="require"),
        ],
    )
    def test_matches_any_case(self, spelling: str, mode: str) -> None:
        """A mode in another case reads as that mode."""
        health = HealthChecks(provider_checks=spelling)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        configured = HealthChecks.from_config(
            HealthChecksConfig(),
            provider_checks=spelling,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        )

        assert health._provider_checks == mode
        assert configured._provider_checks == mode

    def test_unknown_mode_raises(self) -> None:
        """A string that names no mode is refused, naming the three."""
        with pytest.raises(
            SettingsValidationError, match="off, report, require"
        ):
            HealthChecks(provider_checks="on")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    async def test_report_keeps_readyz_up_while_a_backend_is_down(
        self,
    ) -> None:
        """A down backend shows in `/healthz` and leaves `/readyz` at 200."""
        health = HealthChecks(cache_ttl=0, provider_checks="report")
        micro = Grelmicro(uses=[FakeProvider("redis", healthy=False), health])

        async with micro, _client(health) as client:
            readyz = await client.get("/readyz")
            healthz = await client.get("/healthz")

        assert readyz.status_code == HTTP_200_OK
        assert healthz.status_code == HTTP_200_OK
        assert healthz.json()["checks"]["provider:redis"] == {
            "status": "error",
            "critical": False,
            "error": "ConnectionError: backend down",
        }

    async def test_require_fails_readyz_while_a_backend_is_down(self) -> None:
        """A down backend turns `/readyz` to 503."""
        health = HealthChecks(cache_ttl=0, provider_checks="require")
        micro = Grelmicro(uses=[FakeProvider("redis", healthy=False), health])

        async with micro, _client(health) as client:
            readyz = await client.get("/readyz")

        assert readyz.status_code == HTTP_503_SERVICE_UNAVAILABLE

    async def test_skips_provider_without_check(self) -> None:
        """A provider with no readiness check is skipped, not an error."""
        health = HealthChecks(cache_ttl=0, provider_checks="require")
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
        health = HealthChecks(cache_ttl=0, provider_checks="require")
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
        health = HealthChecks(cache_ttl=0, provider_checks="require")
        health.add_provider(redis, critical=False)
        micro = Grelmicro(uses=[redis, health])

        with caplog.at_level("WARNING", logger="grelmicro.health"):
            async with micro:
                report = await health.run()

        assert report["checks"]["provider:redis"]["critical"] is False
        assert "already registered" not in caplog.text

    async def test_explicit_custom_name_suppresses_auto_check(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An explicit custom name skips the `provider:{short_name}` check."""
        redis = FakeProvider("redis")
        health = HealthChecks(cache_ttl=0, provider_checks="require")
        health.add_provider(redis, name="cache", critical=False)
        micro = Grelmicro(uses=[redis, health])

        with caplog.at_level("WARNING", logger="grelmicro.health"):
            async with micro:
                report = await health.run()

        assert set(report["checks"]) == {"provider:cache"}
        assert "already registered" not in caplog.text


class TestAutoHealthRemoved:
    """`auto_health=` is removed in 0.44.0, so it is an unknown keyword."""

    @pytest.mark.parametrize("value", [True, False])
    def test_raises_type_error(self, *, value: bool) -> None:
        """`auto_health=` raises `TypeError` like any unknown keyword."""
        with pytest.raises(TypeError, match="auto_health"):
            HealthChecks(auto_health=value)  # type: ignore[call-arg]  # ty: ignore[unknown-argument]
        with pytest.raises(TypeError, match="auto_health"):
            HealthChecks.from_config(  # type: ignore[call-arg]
                HealthChecksConfig(),
                auto_health=value,  # ty: ignore[unknown-argument]
            )

    @pytest.mark.parametrize(
        "method", [HealthChecks.__init__, HealthChecks.from_config]
    )
    def test_has_no_overloads(self, method: object) -> None:
        """`HealthChecks` and `from_config` declare one signature each."""
        assert get_overloads(method) == []  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    def test_no_pending_entry(self) -> None:
        """No pending entry warns about `auto_health=` or the default."""
        assert not hasattr(_deprecation, "AUTO_HEALTH")
        assert not hasattr(_deprecation, "PROVIDERS_DEFAULT")
        assert not [
            entry for entry in PENDING if "provider_checks" in entry.message
        ]


async def _open(micro: Grelmicro) -> None:
    """Open and close `micro` once."""
    async with micro:
        pass


def _start(*uses: object) -> Grelmicro:
    """Build an app from `uses`, then open and close it once."""
    micro = Grelmicro(uses=list(uses))  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    asyncio.run(_open(micro))
    return micro


def _future_warnings(caught: list[warnings.WarningMessage]) -> list[str]:
    """Return the `GrelmicroFutureWarning` messages in `caught`."""
    return [
        str(warning.message)
        for warning in caught
        if issubclass(warning.category, GrelmicroFutureWarning)
    ]


async def _checks(micro: Grelmicro, health: HealthChecks) -> dict[str, object]:
    """Open `micro` and return the checks one run of `health` reports."""
    async with micro:
        return dict((await health.run())["checks"])


class TestProvidersDefault:
    """`provider_checks=` left unset reads as `"report"`."""

    @pytest.mark.parametrize(
        "build",
        [
            pytest.param(lambda: HealthChecks(cache_ttl=0), id="init"),
            pytest.param(
                lambda: HealthChecks.from_config(
                    HealthChecksConfig(cache_ttl=0)
                ),
                id="from-config",
            ),
        ],
    )
    async def test_registers_a_report_check_per_provider(
        self, build: Callable[[], HealthChecks]
    ) -> None:
        """Each Provider gets a non-critical `provider:{short_name}` check."""
        health = build()
        micro = Grelmicro(
            uses=[FakeProvider("redis"), FakeProvider("postgres"), health]
        )

        checks = await _checks(micro, health)

        assert set(checks) == {"provider:redis", "provider:postgres"}
        assert all(
            check["critical"] is False  # type: ignore[index]  # ty: ignore[not-subscriptable]
            for check in checks.values()
        )

    async def test_a_down_provider_shows_in_healthz_and_keeps_readyz_up(
        self,
    ) -> None:
        """A down backend shows in `/healthz` and leaves `/readyz` at 200."""
        health = HealthChecks(cache_ttl=0)
        micro = Grelmicro(uses=[FakeProvider("redis", healthy=False), health])

        async with micro, _client(health) as client:
            readyz = await client.get("/readyz")
            healthz = await client.get("/healthz")

        assert readyz.status_code == HTTP_200_OK
        assert healthz.status_code == HTTP_200_OK
        assert healthz.json()["checks"]["provider:redis"] == {
            "status": "error",
            "critical": False,
            "error": "ConnectionError: backend down",
        }

    async def test_borrowed_provider_gets_a_check(self, tmp_path: Path) -> None:
        """A Provider a Component borrows without listing gets a check."""
        sqlite = SQLiteProvider(path=str(tmp_path / "app.db"))
        health = HealthChecks(cache_ttl=0)

        checks = await _checks(Grelmicro(uses=[Cache(sqlite), health]), health)

        assert set(checks) == {"provider:sqlite"}

    async def test_add_provider_keeps_its_registration(self) -> None:
        """A Provider added with `add_provider` keeps its settings."""
        redis = FakeProvider("redis")
        health = HealthChecks(cache_ttl=0)
        health.add_provider(redis)

        checks = await _checks(Grelmicro(uses=[redis, health]), health)

        assert checks["provider:redis"]["critical"] is True  # type: ignore[index]  # ty: ignore[not-subscriptable]

    @pytest.mark.parametrize(
        "uses",
        [
            pytest.param(
                lambda: (FakeProvider(), HealthChecks()), id="init-default"
            ),
            pytest.param(
                lambda: (
                    FakeProvider(),
                    HealthChecks.from_config(HealthChecksConfig()),
                ),
                id="from-config-default",
            ),
            pytest.param(
                lambda: (
                    FakeProvider(),
                    HealthChecks(),
                    HealthChecks(name="other"),
                ),
                id="two-health-checks",
            ),
            pytest.param(
                lambda: (FakeProvider(), HealthChecks(provider_checks="off")),
                id="off",
            ),
            pytest.param(
                lambda: (
                    FakeProvider(),
                    HealthChecks(provider_checks="require"),
                ),
                id="require",
            ),
        ],
    )
    def test_warns_nothing(
        self, uses: Callable[[], tuple[object, ...]]
    ) -> None:
        """Opening an app beside a Provider warns nothing, set or unset."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _start(*uses())

        assert not _future_warnings(caught)

    @pytest.mark.parametrize("mode", ["report", "require"])
    async def test_opened_outside_an_app_registers_nothing(
        self, mode: ProviderChecks
    ) -> None:
        """A `HealthChecks` opened on its own has no Provider to check."""
        health = HealthChecks(cache_ttl=0, provider_checks=mode)

        async with health:
            report = await health.run()

        assert report["checks"] == {}

    def test_faked_app_warns_nothing(self, tmp_path: Path) -> None:
        """Under `fake()`, an app that borrows a Provider warns nothing."""
        sqlite = SQLiteProvider(path=str(tmp_path / "app.db"))
        micro = Grelmicro(uses=[Cache(sqlite), HealthChecks()])

        async def open_faked() -> None:
            async with micro.fake(), micro:
                pass

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            asyncio.run(open_faked())

        assert not _future_warnings(caught)


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
