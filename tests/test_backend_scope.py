"""Tests for the deployment environment and the backend scope check."""

import logging
import warnings
from pathlib import Path

import pytest

from grelmicro import BackendScopeError, Grelmicro, GrelmicroConfigWarning
from grelmicro._config import flush_ignored_env_reports
from grelmicro._environment import unmet_requirements
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.coordination import Coordination
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.coordination.redis import RedisLockAdapter
from grelmicro.coordination.sqlite import SQLiteLockAdapter
from grelmicro.outbox import Outbox
from grelmicro.outbox.memory import MemoryOutboxAdapter
from grelmicro.providers.memory import MemoryProvider
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.resilience import (
    Bulkhead,
    CircuitBreakerComponent,
    RateLimiterComponent,
)
from grelmicro.resilience.circuitbreaker.memory import (
    MemoryCircuitBreakerAdapter,
)
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter
from grelmicro.types import Environment

STRICT_ENVIRONMENTS: list[Environment] = ["staging", "production"]
QUIET_ENVIRONMENTS: list[Environment] = ["development", "test"]


@pytest.fixture
def _undeclared(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override the autouse fixture and declare no tier at all."""
    monkeypatch.delenv("GREL_ENVIRONMENT", raising=False)


def test_adapters_declare_their_scope() -> None:
    """Every first-party adapter says how far it shares what it holds."""
    assert MemoryLockAdapter.scope == "process"
    assert SQLiteLockAdapter.scope == "host"
    assert RedisLockAdapter.scope == "cluster"


def test_components_default_to_the_scope_their_promise_needs() -> None:
    """Coordination and Outbox need the fleet, the rest do not."""
    assert Coordination.default_requires == "cluster"
    assert Outbox.default_requires == "cluster"
    assert Cache.default_requires == "process"
    assert RateLimiterComponent.default_requires == "process"
    assert CircuitBreakerComponent.default_requires == "process"


@pytest.mark.parametrize("environment", STRICT_ENVIRONMENTS)
async def test_strict_environment_refuses_a_backend_that_falls_short(
    environment: Environment,
) -> None:
    """A memory lock in a deployed environment is an error, not a warning."""
    micro = Grelmicro(
        uses=[Coordination(lock=MemoryLockAdapter())],
        environment=environment,
    )

    with pytest.raises(BackendScopeError) as error:
        await micro.__aenter__()

    assert "MemoryLockAdapter" in str(error.value)
    assert "provides scope 'process'" in str(error.value)
    assert "requires scope 'cluster'" in str(error.value)
    assert environment in str(error.value)


async def test_strict_environment_reports_every_component() -> None:
    """One message names each component that does not hold."""
    micro = Grelmicro(
        uses=[
            Coordination(lock=MemoryLockAdapter()),
            Cache(MemoryCacheAdapter(), requires="cluster"),
        ],
        environment="production",
    )

    with pytest.raises(BackendScopeError) as error:
        await micro.__aenter__()

    assert "Coordination('default')" in str(error.value)
    assert "Cache('default')" in str(error.value)


async def test_one_component_reports_its_backends_together() -> None:
    """A provider behind four coordination backends is one mistake."""
    micro = Grelmicro(uses=[MemoryProvider()], environment="production")

    with pytest.raises(BackendScopeError) as error:
        await micro.__aenter__()

    message = str(error.value)
    assert message.count("Coordination('default')") == 1
    assert "MemoryLockAdapter, " in message
    assert "MemoryScheduleAdapter" in message
    assert "provide scope 'process'" in message


@pytest.mark.parametrize("environment", QUIET_ENVIRONMENTS)
async def test_quiet_environment_reports_nothing(
    environment: Environment,
) -> None:
    """Development and test wire memory backends on purpose."""
    micro = Grelmicro(
        uses=[Coordination(lock=MemoryLockAdapter())],
        environment=environment,
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        async with micro:
            pass


@pytest.mark.usefixtures("_undeclared")
async def test_undeclared_environment_warns_once_on_both_channels(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The report names the backend, the tier variable, and the way out."""
    micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])

    with caplog.at_level(logging.WARNING, logger="grelmicro"):
        with pytest.warns(GrelmicroConfigWarning, match="MemoryLockAdapter"):
            await micro.__aenter__()
        flush_ignored_env_reports()
        await micro.__aexit__(None, None, None)

    record = caplog.records[0].__dict__
    assert record["component"] == "Coordination('default')"
    assert record["backend_scope"] == "process"
    message = caplog.records[0].getMessage()
    assert "GREL_ENVIRONMENT" in message
    assert "requires='process'" in message


@pytest.mark.usefixtures("_undeclared")
async def test_undeclared_environment_counts_the_findings_it_omits() -> None:
    """A second finding is counted rather than repeated in full."""
    micro = Grelmicro(
        uses=[
            Coordination(lock=MemoryLockAdapter()),
            Cache(MemoryCacheAdapter(), requires="cluster"),
        ]
    )

    with pytest.warns(
        GrelmicroConfigWarning, match="One other binding does not hold"
    ):
        await micro.__aenter__()

    await micro.__aexit__(None, None, None)


async def test_requires_lowers_the_bar_for_a_single_process_deployment() -> (
    None
):
    """A declared single-process deployment boots on memory in production."""
    micro = Grelmicro(
        uses=[
            Coordination(lock=MemoryLockAdapter(), requires="process"),
            Outbox(MemoryOutboxAdapter(), requires="process"),
        ],
        environment="production",
    )

    async with micro:
        assert micro.environment == "production"


async def test_requires_raises_the_bar_for_a_shared_budget() -> None:
    """A rate limiter told to be fleet-wide refuses a per-replica backend."""
    micro = Grelmicro(
        uses=[
            RateLimiterComponent(MemoryRateLimiterAdapter(), requires="cluster")
        ],
        environment="production",
    )

    with pytest.raises(BackendScopeError, match="MemoryRateLimiterAdapter"):
        await micro.__aenter__()


def test_sqlite_holds_across_processes_but_not_across_hosts(
    tmp_path: Path,
) -> None:
    """`host` satisfies a host requirement and fails a cluster one."""
    provider = SQLiteProvider(path=str(tmp_path / "cache.db"))

    Grelmicro(
        uses=[Cache(provider, requires="host")],
        environment="production",
    ).check_backends()

    stricter = Grelmicro(uses=[Cache(provider, requires="cluster")])
    with pytest.raises(BackendScopeError, match="provides scope 'host'"):
        stricter.check_backends()


async def test_a_local_pattern_on_memory_is_left_alone() -> None:
    """A per-replica cache and circuit breaker are the standard shape."""
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CircuitBreakerComponent(MemoryCircuitBreakerAdapter()),
        ],
        environment="production",
    )

    async with micro:
        pass


async def test_an_adapter_that_declares_no_scope_is_not_reported() -> None:
    """A third-party author knows their reach, and grelmicro does not."""
    adapter = MemoryLockAdapter()
    del type(adapter).scope
    try:
        micro = Grelmicro(
            uses=[Coordination(lock=adapter)], environment="production"
        )
        micro.check_backends()
    finally:
        MemoryLockAdapter.scope = "process"


def test_check_backends_answers_for_production_from_a_test_process() -> None:
    """The declared tier is `test`, and the answer is still production's."""
    micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])
    assert micro.environment == "test"

    with pytest.raises(BackendScopeError, match="'production'"):
        micro.check_backends()


def test_check_backends_takes_the_tier_to_answer_for() -> None:
    """The question is visible at the call site."""
    micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])

    with pytest.raises(BackendScopeError, match="'staging'"):
        micro.check_backends(environment="staging")


def test_check_backends_passes_on_a_wiring_that_holds() -> None:
    """Nothing is raised when every bound backend reaches far enough."""
    micro = Grelmicro(
        uses=[Coordination(lock=MemoryLockAdapter(), requires="process")]
    )

    micro.check_backends()


def test_environment_comes_from_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GREL_ENVIRONMENT` declares the tier without a constructor argument."""
    monkeypatch.setenv("GREL_ENVIRONMENT", "staging")
    assert Grelmicro().environment == "staging"


def test_the_argument_wins_over_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit tier outranks the environment, as everywhere else."""
    monkeypatch.setenv("GREL_ENVIRONMENT", "production")
    assert Grelmicro(environment="development").environment == "development"


def test_the_variable_is_read_without_the_env_load_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A safety check behind an opt-in flag would be off where it matters."""
    monkeypatch.delenv("GREL_ENV_LOAD", raising=False)
    monkeypatch.setenv("GREL_ENVIRONMENT", "production")
    assert Grelmicro().environment == "production"


@pytest.mark.parametrize("value", ["preprod", "qa", "prodution", "PRODUCTION"])
def test_a_value_naming_no_tier_warns_and_reads_as_undeclared(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fleet with its own tier names keeps booting, and a typo is loud."""
    monkeypatch.setenv("GREL_ENVIRONMENT", value)

    with caplog.at_level(logging.WARNING, logger="grelmicro"):
        with pytest.warns(GrelmicroConfigWarning, match="is not one of"):
            micro = Grelmicro()
        flush_ignored_env_reports()

    assert micro.environment is None
    assert caplog.records[0].__dict__["variable"] == "GREL_ENVIRONMENT"


def test_a_value_naming_no_tier_is_reported_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second app with the same value stays quiet."""
    monkeypatch.setenv("GREL_ENVIRONMENT", "preprod")

    with pytest.warns(GrelmicroConfigWarning, match="is not one of"):
        Grelmicro()

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert Grelmicro().environment is None


class _Bare:
    """A component-shaped object with no registration name."""

    kind = "bare"

    def __init__(self) -> None:
        self.requires = "cluster"
        self.backend = MemoryLockAdapter()
        self._lock_backend = self.backend


def test_a_component_without_a_name_is_labelled_by_its_class() -> None:
    """The label falls back to the class when there is no name to show."""
    unmet = unmet_requirements([_Bare()])

    assert unmet[0].component == "_Bare"


def test_the_same_backend_behind_two_slots_is_named_once() -> None:
    """One adapter serving two slots of a component is one entry."""
    unmet = unmet_requirements([_Bare()])

    assert unmet[0].backends == ("MemoryLockAdapter",)


@pytest.mark.usefixtures("_undeclared")
async def test_undeclared_environment_counts_several_omitted_findings() -> None:
    """Three findings report the first and count the other two."""
    micro = Grelmicro(
        uses=[
            Coordination(lock=MemoryLockAdapter()),
            Cache(MemoryCacheAdapter(), requires="cluster"),
            RateLimiterComponent(
                MemoryRateLimiterAdapter(), requires="cluster"
            ),
        ]
    )

    with pytest.warns(
        GrelmicroConfigWarning, match="2 other bindings do not hold"
    ):
        await micro.__aenter__()

    await micro.__aexit__(None, None, None)


@pytest.mark.usefixtures("_undeclared")
async def test_the_report_logs_straight_away_once_logging_is_configured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An app opened after `Log` does not queue its report."""
    flush_ignored_env_reports()
    micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])

    with caplog.at_level(logging.WARNING, logger="grelmicro"):
        with pytest.warns(GrelmicroConfigWarning):
            await micro.__aenter__()
        await micro.__aexit__(None, None, None)

    assert "MemoryLockAdapter" in caplog.records[0].getMessage()


async def test_a_bulkhead_checks_the_components_it_opens() -> None:
    """`Bulkhead(uses=[...])` is checked the first time the scope opens."""
    bulkhead = Bulkhead(
        "orders",
        max_concurrent=1,
        uses=[Coordination(lock=MemoryLockAdapter())],
    )
    micro = Grelmicro(environment="production")

    async with micro:
        with pytest.raises(BackendScopeError, match="MemoryLockAdapter"):
            async with bulkhead:
                pass  # pragma: no cover
