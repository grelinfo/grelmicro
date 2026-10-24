"""Tests for the tasks `Grelmicro.describe()` reports and the checks on their gates."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from grelmicro import Grelmicro
from grelmicro.coordination import Coordination, LeaderElection, TaskLock
from grelmicro.coordination.memory import (
    MemoryLockAdapter,
    MemoryScheduleAdapter,
)
from grelmicro.describe import TaskGate, TaskReport
from grelmicro.providers.redis import RedisProvider
from grelmicro.task import CronTask, IntervalTask, TaskRouter, Tasks

if TYPE_CHECKING:
    from grelmicro import Usable
    from grelmicro.types import Environment

MODULE = "tests.test_describe_tasks"
"""The module every task below is named after."""


async def purge() -> None:
    """Do nothing, every minute."""


async def nightly() -> None:
    """Do nothing, every night."""


async def heartbeat() -> None:
    """Do nothing, every ten seconds."""


async def unsafe() -> None:
    """Do nothing, under a name that is not a valid lock name."""


unsafe.__module__ = "__main__"


def _coordination() -> Coordination:
    """Return a memory Coordination holding a lock and a schedule backend."""
    return Coordination(
        lock=MemoryLockAdapter(),
        schedule=MemoryScheduleAdapter(),
        requires="process",
    )


def _app(
    *uses: Usable, environment: Environment | None = "development"
) -> Grelmicro:
    """Build an app in `environment`, `development` unless told otherwise."""
    return Grelmicro(uses=list(uses), environment=environment)


def _reported(micro: Grelmicro) -> tuple[TaskReport, ...]:
    """Return the tasks `micro` reports, which a registered `Tasks` sets."""
    tasks = micro.describe().tasks
    assert tasks is not None
    return tasks


def _task(tasks: TaskRouter, name: str) -> CronTask | IntervalTask:
    """Return the task named `name` in `tasks`, narrowed to a known kind."""
    [task] = [t for t in tasks.tasks if t.name == name]
    assert isinstance(task, CronTask | IntervalTask)
    return task


def test_report_lists_an_interval_task_behind_a_claim() -> None:
    """A claimed interval task reports its interval, key and lock backend."""
    tasks = Tasks()
    tasks.every(interval=60, gate="claim")(purge)

    [report] = _reported(_app(_coordination(), tasks))

    assert report == TaskReport(
        name=f"{MODULE}:purge",
        interval=timedelta(seconds=60),
        gate="claim",
        claim_key=f"{MODULE}:purge",
        backend="MemoryLockAdapter",
    )


def test_report_lists_a_cron_task_behind_a_leader() -> None:
    """A leader-gated cron task names the election and claims under its name."""
    leader = LeaderElection("primary")
    tasks = Tasks()
    tasks.add_task(leader)
    tasks.cron("0 3 * * *", gate=leader)(nightly)

    report = _reported(_app(_coordination(), tasks))

    assert report[-1] == TaskReport(
        name=f"{MODULE}:nightly",
        cron="0 3 * * *",
        timezone="UTC",
        gate="leader",
        leader="primary",
        claim_key=f"{MODULE}:nightly",
        backend="MemoryScheduleAdapter",
    )


def test_report_lists_an_interval_task_behind_a_leader() -> None:
    """A leader-gated interval task claims each interval under its lock."""
    leader = LeaderElection("primary")
    tasks = Tasks()
    tasks.add_task(leader)
    tasks.every(interval=60, gate=leader, name="purge")(purge)

    report = _reported(_app(_coordination(), tasks))

    assert report[-1] == TaskReport(
        name="purge",
        interval=timedelta(seconds=60),
        gate="leader",
        leader="primary",
        claim_key="purge",
        backend="MemoryLockAdapter",
    )


def test_report_lists_an_ungated_task_with_its_schedule_only() -> None:
    """A task every worker runs reports no gate, key or backend."""
    tasks = Tasks()
    tasks.every(interval=10)(heartbeat)

    [report] = _reported(_app(_coordination(), tasks))

    assert report == TaskReport(
        name=f"{MODULE}:heartbeat", interval=timedelta(seconds=10)
    )


def test_report_lists_a_task_lock_gate_under_its_own_name() -> None:
    """A `TaskLock` gate claims under its own name, through its own backend."""
    tasks = Tasks()
    lock = TaskLock("purge-lock", backend=MemoryLockAdapter())
    tasks.every(interval=1, gate=lock)(purge)

    [report] = _reported(_app(tasks))

    assert report.gate == "lock"
    assert report.claim_key == "purge-lock"
    assert report.backend == "MemoryLockAdapter"


def test_report_resolves_a_task_lock_from_a_named_coordination() -> None:
    """A `TaskLock` naming a `Coordination` resolves its backend from that one."""
    tasks = Tasks()
    lock = TaskLock(backend="jobs")
    tasks.every(interval=1, gate=lock, name="purge")(purge)
    jobs = Coordination(lock=MemoryLockAdapter(), name="jobs")

    [report] = _reported(_app(jobs, tasks))

    assert report.claim_key == "purge"
    assert report.backend == "MemoryLockAdapter"


def test_report_names_a_cron_backend_passed_to_the_task() -> None:
    """A cron task given `backend=` reports it without any `Coordination`."""
    tasks = Tasks()
    tasks.cron("0 3 * * *", gate="claim", backend=MemoryScheduleAdapter())(
        nightly
    )

    [report] = _reported(_app(tasks))

    assert report.backend == "MemoryScheduleAdapter"


def test_claim_key_of_an_explicit_name_is_the_name() -> None:
    """A task given `name=` claims under that name, for both kinds."""
    tasks = Tasks()
    tasks.every(interval=60, gate="claim", name="purge-sessions")(purge)
    tasks.cron("0 3 * * *", gate="claim", name="nightly-report")(nightly)

    report = _reported(_app(_coordination(), tasks))

    assert [task.claim_key for task in report] == [
        "purge-sessions",
        "nightly-report",
    ]


def test_claim_key_of_a_derived_name_is_the_lock_name_it_maps_to() -> None:
    """An interval claim maps a derived name to a lock name, cron keeps it."""
    tasks = Tasks()
    tasks.every(interval=60, gate="claim")(unsafe)
    tasks.cron("0 3 * * *", gate="claim")(unsafe)

    interval, cron = _reported(_app(_coordination(), tasks))

    assert interval.claim_key == "task-__main__:unsafe"
    assert cron.claim_key == "__main__:unsafe"


def test_report_includes_router_tasks_in_registration_order() -> None:
    """Tasks of an included router follow the ones the Tasks holds itself."""
    router = TaskRouter()
    router.every(interval=10)(heartbeat)
    tasks = Tasks()
    tasks.include_router(router)
    tasks.cron("0 3 * * *", gate="claim")(nightly)
    tasks.every(interval=60, gate="claim")(purge)

    report = _reported(_app(_coordination(), tasks))

    assert [task.name for task in report] == [
        f"{MODULE}:nightly",
        f"{MODULE}:purge",
        f"{MODULE}:heartbeat",
    ]


def test_report_gives_each_cron_task_its_effective_timezone() -> None:
    """The task's own zone, else its router's, else the `Tasks` zone."""
    router = TaskRouter(timezone="Asia/Tokyo")
    router.cron("0 3 * * *", gate="claim", name="router")(nightly)
    tasks = Tasks(timezone="Europe/Zurich")
    tasks.cron("0 3 * * *", gate="claim", name="inherits")(nightly)
    tasks.cron(
        "0 3 * * *", gate="claim", name="declares", timezone="America/New_York"
    )(nightly)
    tasks.every(interval=60, name="interval")(purge)
    tasks.include_router(router)

    report = _reported(_app(_coordination(), tasks))

    assert {task.name: task.timezone for task in report} == {
        "inherits": "Europe/Zurich",
        "declares": "America/New_York",
        "interval": None,
        "router": "Asia/Tokyo",
    }


def test_report_names_the_provider_the_backend_borrows() -> None:
    """A backend borrowed from a Provider carries its short name."""
    tasks = Tasks()
    tasks.every(interval=60, gate="claim")(purge)
    redis = RedisProvider("redis://localhost:6379/0")

    [report] = _reported(_app(redis, tasks))

    assert report.backend == "RedisLockAdapter"
    assert report.provider == "redis"


def test_report_without_tasks_registered_holds_none() -> None:
    """An app with no `Tasks` reports no task list and renders no block."""
    report = _app(_coordination()).describe()

    assert report.tasks is None
    assert "Tasks" not in report.render()


def test_report_with_empty_tasks_renders_none_registered() -> None:
    """A registered `Tasks` with no task says so, like Components."""
    report = _app(Tasks()).describe()

    assert report.tasks == ()
    assert "Tasks\n  none registered\n" in report.render()


def test_report_renders_one_row_per_task() -> None:
    """Each row reads name, schedule, gate, backend and provider.

    A task that is neither a cron nor an interval task, such as the
    election itself, shows `-` for its schedule.
    """
    leader = LeaderElection("primary")
    tasks = Tasks()
    tasks.every(interval=60, gate="claim", name="purge")(purge)
    tasks.cron("0 3 * * *", gate=leader, name="nightly")(nightly)
    tasks.every(interval=timedelta(milliseconds=500), name="beat")(heartbeat)
    tasks.every(interval=1, gate=TaskLock(), name="locked")(purge)
    tasks.add_task(leader)
    redis = RedisProvider("redis://localhost:6379/0")

    text = _app(redis, tasks).describe().render()

    block = text.split("Tasks\n", 1)[1].split("\n\n", 1)[0]
    assert block.splitlines() == [
        "  purge    every 60s             claim           RedisLockAdapter <- redis",
        "  nightly  cron 0 3 * * * (UTC)  leader primary  RedisScheduleAdapter <- redis",
        "  beat     every 0.5s            none",
        "  locked   every 1s              lock            RedisLockAdapter <- redis",
        "  primary  -                     none",
    ]
    assert text.index("Components") < text.index("Tasks") < text.index("Checks")


def test_report_renders_a_gated_task_with_no_backend() -> None:
    """A gated task whose backend does not resolve says so on its row."""
    tasks = Tasks()
    tasks.every(interval=60, gate="claim", name="purge")(purge)

    text = _app(tasks).describe().render()

    assert "  purge  every 60s  claim  no backend\n" in text


def test_reports_compare_by_value() -> None:
    """Two reports of the same wiring are equal, so a test can pin one."""

    def build() -> Grelmicro:
        tasks = Tasks()
        tasks.every(interval=60, gate="claim")(purge)
        tasks.cron("0 3 * * *", gate="claim")(nightly)
        return _app(_coordination(), tasks)

    assert build().describe().tasks == build().describe().tasks


def test_cron_gate_reads_back_what_was_passed() -> None:
    """The election is the same object, a claim is the string."""
    leader = LeaderElection("primary")
    tasks = Tasks()
    tasks.cron("0 3 * * *", gate=leader, name="leader")(nightly)
    tasks.cron("0 3 * * *", gate="claim", name="claim")(nightly)
    tasks.cron("0 3 * * *", name="omitted")(nightly)

    led = _task(tasks, "leader")
    assert isinstance(led, CronTask)
    assert led.gate is leader
    assert _task(tasks, "claim").gate == "claim"
    assert _task(tasks, "omitted").gate is None


def test_interval_gate_reads_back_what_was_passed() -> None:
    """A lock and an election are the same objects the caller passed."""
    leader = LeaderElection("primary")
    lock = TaskLock()
    tasks = Tasks()
    tasks.every(interval=60, gate=leader, name="leader")(purge)
    tasks.every(interval=1, gate=lock, name="lock")(purge)
    tasks.every(interval=60, gate="claim", name="claim")(purge)
    tasks.every(interval=60, name="none")(purge)

    assert _task(tasks, "leader").gate is leader
    assert _task(tasks, "lock").gate is lock
    assert _task(tasks, "claim").gate == "claim"
    assert _task(tasks, "none").gate is None


def test_task_report_and_gate_are_public() -> None:
    """The report model and its gate type import from `grelmicro.describe`."""
    gate: TaskGate = "claim"

    assert TaskReport(name="purge", gate=gate).gate == "claim"


def _gate_backend(micro: Grelmicro) -> list[tuple[str, str]]:
    """Return the status and detail of each `gate-backend` check entry."""
    return [
        (check.status, check.detail)
        for check in micro.describe().checks
        if check.name == "gate-backend"
    ]


@pytest.mark.parametrize("environment", ["production", "staging", None])
def test_gate_backend_warns_on_a_gated_task_with_no_backend(
    environment: Environment | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gated task with no claim backend warns, and never fails the report."""
    monkeypatch.delenv("GREL_ENVIRONMENT", raising=False)
    tasks = Tasks()
    tasks.every(interval=60, gate="claim", name="purge")(purge)
    tasks.cron("0 3 * * *", gate="claim", name="nightly")(nightly)
    micro = _app(tasks, environment=environment)

    found = _gate_backend(micro)

    assert [status for status, _ in found] == ["warn", "warn"]
    assert "'purge'" in found[0][1]
    assert "lock backend" in found[0][1]
    assert "'nightly'" in found[1][1]
    assert "schedule backend" in found[1][1]
    assert micro.describe().ok


def test_gate_backend_warns_when_coordination_lacks_the_kind() -> None:
    """A `Coordination` with no schedule backend leaves a cron claim unresolved."""
    tasks = Tasks()
    tasks.cron("0 3 * * *", gate="claim", name="nightly")(nightly)
    coordination = Coordination(lock=MemoryLockAdapter())

    [(status, detail)] = _gate_backend(
        _app(coordination, tasks, environment="production")
    )

    assert status == "warn"
    assert "'nightly'" in detail


@pytest.mark.parametrize("environment", ["development", "test"])
def test_gate_backend_passes_in_development_and_test(
    environment: Environment,
) -> None:
    """A missing claim backend is expected where memory wiring is the point."""
    tasks = Tasks()
    tasks.every(interval=60, gate="claim")(purge)

    [(status, detail)] = _gate_backend(_app(tasks, environment=environment))

    assert status == "ok"
    assert repr(environment) in detail


def test_gate_backend_passes_when_every_gate_resolves() -> None:
    """One `ok` entry when every gated task resolves its backend."""
    tasks = Tasks()
    tasks.every(interval=60, gate="claim")(purge)

    found = _gate_backend(
        _app(_coordination(), tasks, environment="production")
    )

    assert found == [("ok", "every gated task resolves its claim backend")]


def test_gate_backend_skips_an_app_with_no_gated_task() -> None:
    """Nothing to check without a gate."""
    tasks = Tasks()
    tasks.every(interval=60)(purge)

    assert _gate_backend(_app(tasks, environment="production")) == []
    assert _gate_backend(_app(environment="production")) == []


def _leader_checks(micro: Grelmicro) -> list[tuple[str, str]]:
    """Return the status and detail of each `leader-not-running` check entry."""
    return [
        (check.status, check.detail)
        for check in micro.describe().checks
        if check.name == "leader-not-running"
    ]


def test_leader_check_warns_on_an_election_no_tasks_runs() -> None:
    """A task gated on an election that no `Tasks` holds is reported."""
    leader = LeaderElection("primary")
    tasks = Tasks()
    tasks.every(interval=60, gate=leader, name="purge")(purge)
    micro = _app(_coordination(), tasks, environment="production")

    [(status, detail)] = _leader_checks(micro)

    assert status == "warn"
    assert "'purge'" in detail
    assert "'primary'" in detail
    assert "tasks.add_task" in detail
    assert micro.describe().ok


def test_leader_check_passes_an_election_another_tasks_runs() -> None:
    """The election may run in any registered `Tasks`, or a router of one."""
    leader = LeaderElection("primary")
    elections = TaskRouter()
    elections.add_task(leader)
    runner = Tasks(env_prefix="GREL_RUNNER_")
    runner.include_router(elections)
    tasks = Tasks()
    tasks.cron("0 3 * * *", gate=leader)(nightly)

    found = _leader_checks(_app(_coordination(), runner, tasks))

    assert found == [
        ("ok", "every leader election a task is gated on is registered")
    ]


def test_leader_check_skips_an_app_with_no_leader_gate() -> None:
    """Nothing to check without a leader gate."""
    tasks = Tasks()
    tasks.every(interval=60, gate="claim")(purge)

    assert _leader_checks(_app(_coordination(), tasks)) == []
