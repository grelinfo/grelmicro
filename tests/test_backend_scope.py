"""Tests for the deployment environment and the backend scope check."""

import gc
import logging
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from grelmicro import (
    BackendScopeError,
    Grelmicro,
    GrelmicroConfigWarning,
)
from grelmicro._config import flush_ignored_env_reports
from grelmicro._environment import recorded_bindings, unmet_requirements
from grelmicro.cache import Cache, TTLCache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.coordination import (
    Coordination,
    LeaderElection,
    Lock,
    ReadWriteLock,
    TaskLock,
)
from grelmicro.coordination.memory import (
    MemoryLeaderElectionAdapter,
    MemoryLockAdapter,
    MemoryReadWriteLockAdapter,
    MemoryScheduleAdapter,
)
from grelmicro.coordination.redis import RedisLockAdapter
from grelmicro.coordination.sqlite import SQLiteLockAdapter
from grelmicro.http import IdempotentRequests
from grelmicro.idempotency import Idempotency, IdempotencyConfig
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
from grelmicro.task._cron import CronTask
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


def test_a_host_requirement_names_sqlite_as_a_fix() -> None:
    """SQLite reaches far enough for `host`, so the message offers it."""
    micro = Grelmicro(
        uses=[Coordination(lock=MemoryLockAdapter(), requires="host")]
    )

    with pytest.raises(BackendScopeError, match="Use a SQLite,") as error:
        micro.check_backends()

    assert "provides scope 'process'" in str(error.value)


def _claim() -> None:
    """Fire nothing, a cron body the tests never run."""


def _patterns_on_memory() -> list[tuple[str, Callable[[], object]]]:
    """Every coordination pattern, each holding a memory backend of its own."""
    return [
        ("Lock('cart')", lambda: Lock("cart", backend=MemoryLockAdapter())),
        (
            "TaskLock('sweep')",
            lambda: TaskLock(
                "sweep", backend=MemoryLockAdapter(), lease_duration=60
            ),
        ),
        (
            "ReadWriteLock('stock')",
            lambda: ReadWriteLock(
                "stock", backend=MemoryReadWriteLockAdapter()
            ),
        ),
        (
            "LeaderElection('worker')",
            lambda: LeaderElection(
                "worker", backend=MemoryLeaderElectionAdapter()
            ),
        ),
        (
            "CronTask('claim')",
            lambda: CronTask(
                function=_claim,
                expr="* * * * *",
                name="claim",
                gate="claim",
                backend=MemoryScheduleAdapter(),
            ),
        ),
    ]


@pytest.mark.parametrize(
    ("label", "build"),
    _patterns_on_memory(),
    ids=[label for label, _ in _patterns_on_memory()],
)
async def test_a_pattern_on_its_own_memory_backend_is_refused_at_open(
    label: str, build: Callable[[], object]
) -> None:
    """A pattern never registered with the app is still checked."""
    pattern = build()
    micro = Grelmicro(environment="production")

    with pytest.raises(BackendScopeError) as error:
        await micro.__aenter__()

    assert f"{label} is bound to Memory" in str(error.value)
    assert "requires scope 'cluster'" in str(error.value)
    del pattern


async def test_a_pattern_built_inside_an_open_app_is_checked_at_once() -> None:
    """A lock built in a handler fails where it is built, not later."""
    async with Grelmicro(environment="production"):
        with pytest.raises(BackendScopeError, match=r"Lock\('cart'\)"):
            Lock("cart", backend=MemoryLockAdapter())


async def test_the_coordination_holding_the_same_backend_decides() -> None:
    """`Coordination(requires=...)` is where a lock accepts local reach."""
    shared = MemoryLockAdapter()
    lock = Lock("cart", backend=shared)
    micro = Grelmicro(
        uses=[Coordination(lock=shared, requires="process")],
        environment="production",
    )

    async with micro, lock:
        pass


def test_check_backends_reads_the_recorded_patterns() -> None:
    """A unit test catches the lock the pod would refuse to boot with."""
    lock = Lock("cart", backend=MemoryLockAdapter())

    with pytest.raises(BackendScopeError, match=r"Lock\('cart'\)"):
        Grelmicro().check_backends()

    del lock


def test_a_pattern_on_a_backend_that_reaches_far_enough_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Redis lock costs one comparison at construction and nothing more."""
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    Lock("cart", backend=RedisLockAdapter())

    assert recorded_bindings() == []


def test_a_pattern_that_is_gone_is_not_reported() -> None:
    """The record lives only as long as the pattern does."""
    Lock("cart", backend=MemoryLockAdapter())
    gc.collect()

    Grelmicro().check_backends()


@pytest.mark.usefixtures("_undeclared")
async def test_the_warning_for_a_pattern_names_the_component_to_register() -> (
    None
):
    """A lock has no `requires=` of its own, so the way out names one."""
    lock = Lock("cart", backend=MemoryLockAdapter())
    micro = Grelmicro()

    with pytest.warns(GrelmicroConfigWarning) as warned:
        await micro.__aenter__()
    await micro.__aexit__(None, None, None)

    assert "Coordination(lock=..., requires='process')" in str(
        warned[0].message
    )
    del lock


def test_the_error_for_a_pattern_names_the_component_to_register() -> None:
    """The strict message closes with the same way out."""
    lock = LeaderElection("worker", backend=MemoryLeaderElectionAdapter())

    with pytest.raises(BackendScopeError) as error:
        Grelmicro().check_backends()

    assert "Coordination(election=..., requires=...)" in str(error.value)
    del lock


def test_idempotency_requires_the_fleet() -> None:
    """A replay has to find the stored response wherever the retry lands."""
    assert IdempotentRequests.default_requires == "cluster"
    assert Idempotency.default_requires == "cluster"
    assert IdempotentRequests().requires == "cluster"
    assert Idempotency("orders", requires="host").requires == "host"


async def test_idempotent_requests_on_a_memory_cache_is_refused() -> None:
    """The check follows the `Cache` the component rides."""
    micro = Grelmicro(
        uses=[Cache(MemoryCacheAdapter()), IdempotentRequests()],
        environment="production",
    )

    with pytest.raises(BackendScopeError) as error:
        await micro.__aenter__()

    message = str(error.value)
    assert (
        "IdempotentRequests('default') rides Cache('default'), which is "
        "bound to MemoryCacheAdapter and provides scope 'process', but "
        "requires scope 'cluster'"
    ) in message
    assert message.count("IdempotentRequests") == 1
    assert "Idempotency(" not in message


async def test_idempotent_requests_accepts_a_declared_local_reach() -> None:
    """`requires=` lowers the bar, as on every other component."""
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            IdempotentRequests(requires="process"),
        ],
        environment="production",
    )

    async with micro:
        pass


def test_idempotent_requests_follows_an_explicit_cache() -> None:
    """A `TTLCache` holding its own backend is checked on that backend."""
    micro = Grelmicro(
        uses=[IdempotentRequests(cache=TTLCache(backend=MemoryCacheAdapter()))]
    )

    with pytest.raises(
        BackendScopeError,
        match=r"IdempotentRequests\('default'\) is bound to MemoryCacheAdapter",
    ):
        micro.check_backends()


def test_a_rider_with_no_cache_to_ride_is_not_reported() -> None:
    """Nothing is bound, so there is nothing to check yet."""
    Grelmicro(uses=[IdempotentRequests()]).check_backends()


def test_an_idempotency_riding_a_memory_cache_is_refused() -> None:
    """The pattern is recorded and checked against the app's `Cache`."""
    idem = Idempotency("orders")
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])

    with pytest.raises(
        BackendScopeError, match=r"Idempotency\('orders'\) rides Cache"
    ):
        micro.check_backends()

    del idem


def test_an_idempotency_on_a_cache_that_holds_its_backend_is_refused() -> None:
    """A registered `Cache` holding the same backend does not decide."""
    backend = MemoryCacheAdapter()
    idem = Idempotency("orders", cache=TTLCache(backend=backend))
    micro = Grelmicro(uses=[Cache(backend)])

    with pytest.raises(
        BackendScopeError, match=r"Idempotency\('orders'\) is bound to"
    ):
        micro.check_backends()

    del idem


def test_an_idempotency_accepts_a_declared_local_reach() -> None:
    """`requires=` on the pattern is its own way out."""
    idem = Idempotency("orders", requires="process")
    from_config = Idempotency.from_config(
        "refunds", IdempotencyConfig(), requires="process"
    )

    Grelmicro(uses=[Cache(MemoryCacheAdapter())]).check_backends()

    del idem, from_config


class _UnhashableLockAdapter(MemoryLockAdapter):
    """A backend that cannot be held in a set, as a third party may write."""

    __hash__ = None  # type: ignore[assignment]


def test_a_backend_that_cannot_be_hashed_is_still_left_to_its_component() -> (
    None
):
    """The component decides at open, by identity, instead of a set."""
    backend = _UnhashableLockAdapter()
    coordination = Coordination(lock=backend, requires="process")
    lock = Lock("cart", backend=backend)

    Grelmicro(uses=[coordination]).check_backends()

    assert [binding.label for binding in recorded_bindings()] == [
        "Lock('cart')"
    ]
    del lock


class _SharedCacheAdapter(MemoryCacheAdapter):
    """A cache backend that says it reaches every replica."""

    scope = "cluster"


@pytest.mark.usefixtures("_undeclared")
async def test_patterns_built_inside_an_open_app_warn_once_per_shape() -> None:
    """A lock named per request warns once, and another mistake still warns."""
    async with Grelmicro():
        with pytest.warns(GrelmicroConfigWarning, match=r"Lock\('first'\)"):
            first = Lock("first", backend=MemoryLockAdapter())
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            second = Lock("second", backend=MemoryLockAdapter())
        with pytest.warns(GrelmicroConfigWarning, match="LeaderElection"):
            leader = LeaderElection(
                "worker", backend=MemoryLeaderElectionAdapter()
            )

    del first, second, leader


@pytest.mark.usefixtures("_undeclared")
async def test_the_warning_for_a_pattern_names_the_line_that_built_it() -> None:
    """The warning points at user code, not inside grelmicro."""
    async with Grelmicro():
        with pytest.warns(GrelmicroConfigWarning) as warned:
            lock = Lock("cart", backend=MemoryLockAdapter())

    assert warned[0].filename == __file__
    del lock


def test_a_coordination_only_built_does_not_answer_for_a_lock() -> None:
    """Only a registered component decides, whatever the build order."""
    backend = MemoryLockAdapter()
    Coordination(lock=backend, requires="process")
    lock = Lock("cart", backend=backend)

    with pytest.raises(BackendScopeError, match=r"Lock\('cart'\)"):
        Grelmicro().check_backends()

    del lock


def test_a_requirement_naming_no_scope_is_passed_over() -> None:
    """A typo in `requires=` does not crash startup, as on a component."""
    idem = Idempotency("orders", requires=cast("Any", "clustr"))

    Grelmicro(uses=[Cache(MemoryCacheAdapter())]).check_backends()

    del idem


async def test_idempotent_requests_built_in_an_open_app_is_not_recorded() -> (
    None
):
    """The component answers for the store it builds, under its own name."""
    async with Grelmicro(
        uses=[Cache(MemoryCacheAdapter())], environment="production"
    ):
        component = IdempotentRequests(requires="cluster")

    assert recorded_bindings() == []
    del component


def test_the_error_for_a_cache_offers_no_kubernetes_backend() -> None:
    """Kubernetes stores coordination state, not cached responses."""
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), IdempotentRequests()])

    with pytest.raises(
        BackendScopeError, match="Use a Redis, Valkey, or Postgres backend"
    ):
        micro.check_backends()


async def test_a_pattern_built_inside_an_open_app_that_holds_is_quiet() -> None:
    """Built where it is checked, a binding that holds raises nothing."""
    async with Grelmicro(
        uses=[Cache(_SharedCacheAdapter())], environment="production"
    ):
        idem = Idempotency("orders")

    del idem


async def test_a_lock_built_by_its_coordination_is_left_to_it() -> None:
    """`micro.coordination.lock(...)` holds the component's own backend."""
    micro = Grelmicro(
        uses=[Coordination(MemoryProvider(), requires="process")],
        environment="production",
    )

    async with micro:
        lock = micro.coordination.lock("cart")

    assert recorded_bindings() == []
    del lock


def test_a_pattern_on_a_backend_with_no_scope_is_not_recorded() -> None:
    """A third-party backend that declares no reach is never reported."""
    adapter = MemoryLockAdapter()
    del type(adapter).scope
    try:
        Lock("cart", backend=adapter)
    finally:
        MemoryLockAdapter.scope = "process"

    assert recorded_bindings() == []


def test_every_recorded_pattern_is_reported() -> None:
    """Two locks on memory are two findings."""
    first = Lock("first", backend=MemoryLockAdapter())
    second = Lock("second", backend=MemoryLockAdapter())

    with pytest.raises(BackendScopeError) as error:
        Grelmicro().check_backends()

    assert "Lock('first')" in str(error.value)
    assert "Lock('second')" in str(error.value)
    del first, second


def test_an_idempotency_on_a_cache_that_reaches_far_enough_holds() -> None:
    """An explicit shared backend meets the requirement, and is not kept."""
    idem = Idempotency("orders", cache=TTLCache(backend=_SharedCacheAdapter()))

    assert recorded_bindings() == []

    del idem


def test_a_registered_pattern_does_not_answer_for_its_own_backend() -> None:
    """A pattern in `uses=[...]` states no reach, so it is still checked."""
    election = LeaderElection("worker", backend=MemoryLeaderElectionAdapter())

    with pytest.raises(BackendScopeError, match=r"LeaderElection\('worker'\)"):
        Grelmicro(uses=[election]).check_backends()


async def test_an_unregistered_coordination_builds_on_its_own_reach() -> None:
    """The `requires=` a component declares holds for the locks it builds."""
    coordination = Coordination(lock=MemoryLockAdapter(), requires="process")

    async with Grelmicro(environment="production"):
        lock = coordination.lock("cart")

    Grelmicro().check_backends()
    del lock


def test_an_unregistered_coordination_on_its_default_reach_is_checked() -> None:
    """With no `requires=`, the lock it builds still needs the fleet."""
    lock = Coordination(lock=MemoryLockAdapter()).lock("cart")

    with pytest.raises(BackendScopeError, match=r"Lock\('cart'\)"):
        Grelmicro().check_backends()

    del lock
