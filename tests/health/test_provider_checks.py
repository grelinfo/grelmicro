"""Tests for provider readiness checks (`add_provider`, `providers=`)."""

import asyncio
import warnings
from pathlib import Path
from typing import Self

import httpx
import pytest
from starlette.status import HTTP_200_OK, HTTP_503_SERVICE_UNAVAILABLE

from grelmicro import Grelmicro
from grelmicro._deprecation import AUTO_HEALTH, PENDING, PROVIDERS_DEFAULT
from grelmicro.cache import Cache
from grelmicro.errors import GrelmicroFutureWarning, SettingsValidationError
from grelmicro.health import ProviderChecks, health_asgi
from grelmicro.health._checks import HealthChecks, HealthChecksConfig
from grelmicro.health._models import HealthStatus
from grelmicro.providers._base import Provider
from grelmicro.providers.sqlite import SQLiteProvider
from tests._future import assert_future_warning

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
    """Tests for `HealthChecks(providers=...)`."""

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
        health = HealthChecks(cache_ttl=0, providers=providers)
        micro = Grelmicro(uses=[redis, postgres, health])

        async with micro:
            report = await health.run()

        assert set(report["checks"]) == {"provider:redis", "provider:postgres"}
        assert all(c["critical"] is critical for c in report["checks"].values())
        assert redis.calls == 1
        assert postgres.calls == 1

    async def test_off_registers_nothing(self) -> None:
        """`providers="off"` registers no provider checks."""
        health = HealthChecks(cache_ttl=0, providers="off")
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
        health = HealthChecks(providers=spelling)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        configured = HealthChecks.from_config(
            HealthChecksConfig(),
            providers=spelling,  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        )

        assert health._providers == mode
        assert configured._providers == mode

    def test_unknown_mode_raises(self) -> None:
        """A string that names no mode is refused, naming the three."""
        with pytest.raises(
            SettingsValidationError, match="off, report, require"
        ):
            HealthChecks(providers="on")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    async def test_report_keeps_readyz_up_while_a_backend_is_down(
        self,
    ) -> None:
        """A down backend shows in `/healthz` and leaves `/readyz` at 200."""
        health = HealthChecks(cache_ttl=0, providers="report")
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
        health = HealthChecks(cache_ttl=0, providers="require")
        micro = Grelmicro(uses=[FakeProvider("redis", healthy=False), health])

        async with micro, _client(health) as client:
            readyz = await client.get("/readyz")

        assert readyz.status_code == HTTP_503_SERVICE_UNAVAILABLE

    async def test_skips_provider_without_check(self) -> None:
        """A provider with no readiness check is skipped, not an error."""
        health = HealthChecks(cache_ttl=0, providers="require")
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
        health = HealthChecks(cache_ttl=0, providers="require")
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
        health = HealthChecks(cache_ttl=0, providers="require")
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
        health = HealthChecks(cache_ttl=0, providers="require")
        health.add_provider(redis, name="cache", critical=False)
        micro = Grelmicro(uses=[redis, health])

        with caplog.at_level("WARNING", logger="grelmicro.health"):
            async with micro:
                report = await health.run()

        assert set(report["checks"]) == {"provider:cache"}
        assert "already registered" not in caplog.text


class TestAutoHealthRename:
    """`auto_health=` is renamed to `providers=` and removed in 0.44.0."""

    @pytest.mark.parametrize(
        ("auto_health", "mode"),
        [
            pytest.param(True, "require", id="true"),
            pytest.param(False, "off", id="false"),
        ],
    )
    def test_maps_to_providers_and_warns(
        self, *, auto_health: bool, mode: ProviderChecks
    ) -> None:
        """`auto_health=` warns the rename and reads as its `providers=` mode."""
        entry = AUTO_HEALTH[auto_health]
        assert_future_warning(
            entry,
            default=lambda: HealthChecks(auto_health=auto_health),
            explicit=lambda: HealthChecks(providers=mode),
        )
        assert_future_warning(
            entry,
            default=lambda: HealthChecks.from_config(
                HealthChecksConfig(), auto_health=auto_health
            ),
            explicit=lambda: HealthChecks.from_config(
                HealthChecksConfig(), providers=mode
            ),
        )
        with pytest.warns(GrelmicroFutureWarning):
            health = HealthChecks(auto_health=auto_health)
        assert health._providers == mode

    async def test_true_registers_critical_checks(self) -> None:
        """`auto_health=True` registers what `providers="require"` does."""
        with pytest.warns(GrelmicroFutureWarning):
            health = HealthChecks(cache_ttl=0, auto_health=True)
        micro = Grelmicro(uses=[FakeProvider("redis"), health])

        async with micro:
            report = await health.run()

        assert report["checks"]["provider:redis"]["critical"] is True

    def test_messages(self) -> None:
        """Each message names the new spelling and the removal release."""
        assert AUTO_HEALTH[True].message == (
            '`auto_health=True` is renamed to `providers="require"`. '
            "`auto_health=True` is removed in 0.44.0."
        )
        assert AUTO_HEALTH[False].message == (
            '`auto_health=False` is renamed to `providers="off"`. '
            "`auto_health=False` is removed in 0.44.0."
        )

    def test_points_at_the_caller(self) -> None:
        """The warning reports the line that built the `HealthChecks`."""
        with pytest.warns(GrelmicroFutureWarning) as caught:
            HealthChecks(auto_health=True)
        with pytest.warns(GrelmicroFutureWarning) as configured:
            HealthChecks.from_config(HealthChecksConfig(), auto_health=True)

        assert caught[0].filename == __file__
        assert configured[0].filename == __file__

    def test_both_raise(self) -> None:
        """Passing both `auto_health=` and `providers=` names both."""
        message = "providers=.*auto_health="
        with pytest.raises(TypeError, match=message):
            HealthChecks(providers="require", auto_health=True)
        with pytest.raises(TypeError, match=message):
            HealthChecks.from_config(
                HealthChecksConfig(), providers="off", auto_health=False
            )

    def test_is_pending_until_0_44(self) -> None:
        """The stale-entry guard covers the removal."""
        for entry in AUTO_HEALTH.values():
            assert entry in PENDING
            assert entry.removed_in == "0.44.0"


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


class TestProvidersDefault:
    """`providers=` left unset warns it defaults to "report" in 0.44.0."""

    def test_left_unset_beside_a_provider_warns(self) -> None:
        """Leaving `providers` unset warns, and passing any mode does not."""
        assert_future_warning(
            PROVIDERS_DEFAULT,
            default=lambda: _start(FakeProvider(), HealthChecks()),
            explicit=lambda: [
                _start(FakeProvider(), HealthChecks(providers="off")),
                _start(FakeProvider(), HealthChecks(providers="report")),
                _start(FakeProvider(), HealthChecks(providers="require")),
            ],
        )

    def test_message(self) -> None:
        """The message names the switch release and both choices."""
        assert PROVIDERS_DEFAULT.message == (
            "`HealthChecks()` reports each Provider in `/healthz` by "
            "default from 0.44.0. "
            'Pass `providers="off"` to keep the current behavior, '
            'or `providers="report"` to adopt the new one now.'
        )

    def test_points_at_the_line_that_opens_the_app(self) -> None:
        """The warning reports the `async with micro:` line, not grelmicro's."""
        with pytest.warns(GrelmicroFutureWarning) as caught:
            _start(FakeProvider(), HealthChecks())

        assert caught[0].filename == __file__

    def test_from_config_left_unset_warns(self) -> None:
        """`from_config` without `providers` warns too."""
        with pytest.warns(GrelmicroFutureWarning, match="providers="):
            _start(
                FakeProvider(), HealthChecks.from_config(HealthChecksConfig())
            )

    def test_from_config_explicit_is_silent(self) -> None:
        """`from_config(providers="off")` warns nothing."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _start(
                FakeProvider(),
                HealthChecks.from_config(HealthChecksConfig(), providers="off"),
            )

        assert not _future_warnings(caught)

    def test_auto_health_does_not_warn_the_default(self) -> None:
        """`auto_health=` warns its rename only, not the default change."""
        with pytest.warns(GrelmicroFutureWarning):
            health = HealthChecks(auto_health=False)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _start(FakeProvider(), health)

        assert not _future_warnings(caught)

    def test_borrowed_provider_warns(self, tmp_path: Path) -> None:
        """A Provider a Component borrows without listing counts."""
        sqlite = SQLiteProvider(path=str(tmp_path / "app.db"))

        with pytest.warns(GrelmicroFutureWarning, match="providers="):
            _start(Cache(sqlite), HealthChecks())

    @pytest.mark.parametrize(
        "uses",
        [
            pytest.param((), id="no-provider"),
            pytest.param((NoCheckProvider(),), id="provider-without-check"),
        ],
    )
    def test_silent_without_a_checked_provider(
        self, uses: tuple[Provider, ...]
    ) -> None:
        """Nothing warns when no Provider would get a check."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _start(*uses, HealthChecks())

        assert not _future_warnings(caught)

    def test_silent_when_every_provider_is_added(self) -> None:
        """A Provider already added with `add_provider` warns nothing."""
        redis = FakeProvider("redis")
        health = HealthChecks()
        health.add_provider(redis, critical=False)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _start(redis, health)

        assert not _future_warnings(caught)

    def test_silent_for_health_checks_opened_outside_an_app(self) -> None:
        """A `HealthChecks` opened on its own, with no app, warns nothing."""

        async def open_alone() -> None:
            async with HealthChecks():
                pass

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            asyncio.run(open_alone())

        assert not _future_warnings(caught)

    def test_silent_for_the_store_a_fake_swaps_in(self) -> None:
        """Under `fake()`, the memory store that stands in warns nothing."""
        micro = Grelmicro(uses=[HealthChecks()])

        async def open_faked() -> None:
            async with micro.fake(), micro:
                pass

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            asyncio.run(open_faked())

        assert not _future_warnings(caught)

    def test_faked_app_counts_its_own_providers(self, tmp_path: Path) -> None:
        """Under `fake()`, a Provider the app borrows still warns."""
        sqlite = SQLiteProvider(path=str(tmp_path / "app.db"))
        micro = Grelmicro(uses=[Cache(sqlite), HealthChecks()])

        async def open_faked() -> None:
            async with micro.fake(), micro:
                pass

        with pytest.warns(GrelmicroFutureWarning, match="providers="):
            asyncio.run(open_faked())

    def test_warns_once_per_app(self) -> None:
        """Two `HealthChecks` and two opens of one app warn once."""
        micro = Grelmicro(
            uses=[FakeProvider(), HealthChecks(), HealthChecks(name="other")]
        )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            asyncio.run(_open(micro))
            asyncio.run(_open(micro))

        assert _future_warnings(caught) == [PROVIDERS_DEFAULT.message]

    def test_left_unset_registers_no_provider_check(self) -> None:
        """The warning changes nothing: no `provider:` check is registered."""
        health = HealthChecks(cache_ttl=0)
        micro = Grelmicro(uses=[FakeProvider("redis"), health])

        async def open_and_run() -> dict[str, object]:
            async with micro:
                return dict((await health.run())["checks"])

        with pytest.warns(GrelmicroFutureWarning):
            checks = asyncio.run(open_and_run())

        assert checks == {}

    def test_is_pending_until_0_44(self) -> None:
        """The stale-entry guard covers the switch."""
        assert PROVIDERS_DEFAULT in PENDING
        assert PROVIDERS_DEFAULT.switch_in == "0.44.0"


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
