"""Tests for the Bulkhead concurrency-isolation pattern."""

import asyncio
import threading
from typing import Any, Self, cast

import pytest

from grelmicro import (
    ComponentNotRegisteredError,
    Grelmicro,
    NoActiveAppError,
    OutOfContextError,
)
from grelmicro.coordination import Coordination, Lock
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.resilience import Bulkhead, BulkheadConfig, BulkheadFullError
from grelmicro.resilience import bulkhead as bulkhead_module

pytestmark = [pytest.mark.timeout(5)]

LIMIT = 2
WORKERS = 6
UNBOUNDED_WORKERS = 5
ENV_LIMIT = 7
FROM_CONFIG_LIMIT = 4
CONFIG_CONCURRENT = 3
CONFIG_WAIT = 0.5
CONFIG_WORKERS = 2
ADD_RESULT = 42
KWARGS_SUM = 5
APPS = 3


# --- Construction & configuration ---


def test_config_property() -> None:
    """`config` exposes the resolved configuration."""
    bulkhead = Bulkhead(
        "api",
        max_concurrent=CONFIG_CONCURRENT,
        max_wait=CONFIG_WAIT,
        max_workers=CONFIG_WORKERS,
    )
    assert bulkhead.name == "api"
    assert isinstance(bulkhead.config, BulkheadConfig)
    assert bulkhead.config.max_concurrent == CONFIG_CONCURRENT
    assert bulkhead.config.max_wait == CONFIG_WAIT
    assert bulkhead.config.max_workers == CONFIG_WORKERS


def test_from_config() -> None:
    """`from_config` builds a bulkhead from a pre-built config."""
    bulkhead = Bulkhead.from_config(
        "api", BulkheadConfig(max_concurrent=FROM_CONFIG_LIMIT)
    )
    assert bulkhead.config.max_concurrent == FROM_CONFIG_LIMIT


def test_env_vars_fill_unset_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset fields resolve from `GREL_BULKHEAD_{NAME}_*`."""
    monkeypatch.setenv("GREL_ENV_LOAD", "true")
    monkeypatch.setenv("GREL_BULKHEAD_CHECKOUT_MAX_CONCURRENT", str(ENV_LIMIT))

    bulkhead = Bulkhead("checkout")

    assert bulkhead.config.max_concurrent == ENV_LIMIT


# --- Concurrency enforcement ---


async def test_max_concurrent_caps_in_flight_calls() -> None:
    """No more than `max_concurrent` calls run inside the scope at once."""
    bulkhead = Bulkhead("api", max_concurrent=LIMIT, max_wait=2.0)
    active = 0
    peak = 0

    async def worker() -> None:
        nonlocal active, peak
        async with bulkhead:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(*(worker() for _ in range(WORKERS)))

    assert peak == LIMIT


async def test_unbounded_admits_everyone() -> None:
    """With `max_concurrent=None` there is no permit and no limit."""
    bulkhead = Bulkhead("api")
    active = 0
    peak = 0

    async def worker() -> None:
        nonlocal active, peak
        async with bulkhead:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(worker() for _ in range(UNBOUNDED_WORKERS)))

    assert peak == UNBOUNDED_WORKERS


async def test_fail_fast_rejects_when_full() -> None:
    """The default (no `max_wait`) rejects immediately when full."""
    bulkhead = Bulkhead("api", max_concurrent=1)
    released = asyncio.Event()

    async def holder() -> None:
        async with bulkhead:
            await released.wait()

    task = asyncio.create_task(holder())
    await asyncio.sleep(0.01)  # let the holder take the only permit

    with pytest.raises(BulkheadFullError) as exc:
        async with bulkhead:
            pass

    assert exc.value.name == "api"
    assert exc.value.max_concurrent == 1
    released.set()
    await task


async def test_max_wait_acquires_when_permit_frees() -> None:
    """A waiter within `max_wait` gets the permit once it frees."""
    bulkhead = Bulkhead("api", max_concurrent=1, max_wait=1.0)
    admitted = False

    async def holder() -> None:
        async with bulkhead:
            await asyncio.sleep(0.05)

    async def waiter() -> None:
        nonlocal admitted
        async with bulkhead:
            admitted = True

    await asyncio.gather(holder(), waiter())

    assert admitted is True


async def test_max_wait_rejects_after_timeout() -> None:
    """A waiter past `max_wait` is rejected."""
    bulkhead = Bulkhead("api", max_concurrent=1, max_wait=0.05)
    released = asyncio.Event()

    async def holder() -> None:
        async with bulkhead:
            await released.wait()

    task = asyncio.create_task(holder())
    await asyncio.sleep(0.01)

    with pytest.raises(BulkheadFullError):
        async with bulkhead:
            pass

    released.set()
    await task


async def test_nested_scopes_consume_permits() -> None:
    """Nested entries in one task each take and release a permit."""
    bulkhead = Bulkhead("api", max_concurrent=LIMIT)
    async with bulkhead, bulkhead:
        # Both permits are held; a third concurrent entry fails fast.
        with pytest.raises(BulkheadFullError):
            async with bulkhead:
                pass
    # Both released: a fresh entry succeeds.
    async with bulkhead:
        pass


# --- Decorator ---


async def test_decorator_enforces_limit() -> None:
    """`@bulkhead` admits calls under the limit."""
    bulkhead = Bulkhead("api", max_concurrent=1)

    @bulkhead
    async def handler() -> str:
        return "ok"

    assert await handler() == "ok"


def test_decorator_rejects_sync_function() -> None:
    """`@bulkhead` on a sync function raises `TypeError`."""
    bulkhead = Bulkhead("api", max_concurrent=1)

    with pytest.raises(TypeError, match="only decorates async functions"):

        @bulkhead  # ty: ignore[invalid-argument-type]
        def handler() -> None: ...


# --- to_thread ---


async def test_to_thread_default_executor() -> None:
    """Without `max_workers`, `to_thread` runs on the shared executor."""
    bulkhead = Bulkhead("api")

    result = await bulkhead.to_thread(lambda x: x + 1, 41)

    assert result == ADD_RESULT


async def test_to_thread_private_executor() -> None:
    """With `max_workers`, `to_thread` runs on the bulkhead's own pool."""
    bulkhead = Bulkhead("checkout", max_workers=2)

    name = await bulkhead.to_thread(lambda: threading.current_thread().name)
    # A second call reuses the already-built private executor.
    again = await bulkhead.to_thread(lambda: threading.current_thread().name)

    assert name.startswith("bulkhead-checkout")
    assert again.startswith("bulkhead-checkout")


async def test_to_thread_passes_kwargs() -> None:
    """`to_thread` forwards positional and keyword arguments."""
    bulkhead = Bulkhead("api", max_workers=1)

    def add(a: int, *, b: int) -> int:
        return a + b

    assert await bulkhead.to_thread(add, 2, b=3) == KWARGS_SUM


# --- Reconfigure ---


async def test_reconfigure_changes_concurrency() -> None:
    """A reconfigured `max_concurrent` applies to new entries."""
    bulkhead = Bulkhead("api", max_concurrent=1, max_wait=2.0)
    await bulkhead.reconfigure(
        bulkhead.config.model_copy(update={"max_concurrent": LIMIT})
    )

    active = 0
    peak = 0

    async def worker() -> None:
        nonlocal active, peak
        async with bulkhead:
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(*(worker() for _ in range(FROM_CONFIG_LIMIT)))

    assert peak == LIMIT


async def test_reconfigure_rebuilds_executor() -> None:
    """Changing `max_workers` discards the private executor."""
    bulkhead = Bulkhead("api", max_workers=1)
    await bulkhead.to_thread(lambda: None)  # builds the executor
    first = bulkhead._executor

    await bulkhead.reconfigure(
        bulkhead.config.model_copy(update={"max_workers": 2})
    )

    assert bulkhead._executor is None
    await bulkhead.to_thread(lambda: None)  # builds a fresh one
    assert bulkhead._executor is not first


# --- uses= overrides ---


async def test_uses_overrides_default_backend_in_scope() -> None:
    """Inside the scope, a default lookup resolves to the bulkhead's component."""
    default = MemoryLockAdapter()
    dedicated = MemoryLockAdapter()
    micro = Grelmicro(uses=[Coordination(lock=default)])
    bulkhead = Bulkhead("checkout", uses=[Coordination(lock=dedicated)])

    async with micro:
        assert micro.get("coordination", "default").lock_backend is default
        async with bulkhead:
            assert (
                micro.get("coordination", "default").lock_backend is dedicated
            )
        assert micro.get("coordination", "default").lock_backend is default


async def test_uses_skips_none_entries() -> None:
    """A `None` entry is skipped, matching `Grelmicro(uses=[...])`."""
    default = MemoryLockAdapter()
    dedicated = MemoryLockAdapter()
    micro = Grelmicro(uses=[Coordination(lock=default)])
    bulkhead = Bulkhead("checkout", uses=[None, Coordination(lock=dedicated)])

    async with micro, bulkhead:
        assert micro.get("coordination", "default").lock_backend is dedicated


async def test_uses_override_only_covers_registered_keys() -> None:
    """A key the bulkhead does not override falls through to the app."""
    default = MemoryLockAdapter()
    dedicated = MemoryLockAdapter()
    micro = Grelmicro(uses=[Coordination(lock=default)])
    bulkhead = Bulkhead("checkout", uses=[Coordination(lock=dedicated)])

    async with micro, bulkhead:
        assert micro.get("coordination", "default").lock_backend is dedicated
        with pytest.raises(ComponentNotRegisteredError):
            micro.get("coordination", "analytics")


async def test_uses_opens_once_and_closes_at_shutdown() -> None:
    """`uses=` items open on first entry and close at app shutdown."""

    class Track:
        def __init__(self) -> None:
            self.entered = 0
            self.exited = 0

        async def __aenter__(self) -> Self:
            self.entered += 1
            return self

        async def __aexit__(self, *exc: object) -> None:
            self.exited += 1

    track = Track()
    bulkhead = Bulkhead("reports", uses=[track])
    micro = Grelmicro()

    async with micro:
        assert track.entered == 0
        async with bulkhead:
            pass
        async with bulkhead:
            pass
        assert track.entered == 1  # opened once, not per entry
        assert track.exited == 0
    assert track.exited == 1  # closed at app shutdown


async def test_uses_requires_active_app() -> None:
    """Entering a `uses=` bulkhead without an app raises."""
    bulkhead = Bulkhead(
        "checkout", uses=[Coordination(lock=MemoryLockAdapter())]
    )
    with pytest.raises(NoActiveAppError):
        async with bulkhead:
            pass


async def test_nested_bulkheads_merge_overrides() -> None:
    """A nested bulkhead's overrides layer over the outer one's."""
    default = MemoryLockAdapter()
    outer_adapter = MemoryLockAdapter()
    inner_adapter = MemoryLockAdapter()
    micro = Grelmicro(uses=[Coordination(lock=default)])
    outer = Bulkhead("outer", uses=[Coordination(lock=outer_adapter)])
    inner = Bulkhead(
        "inner", uses=[Coordination(lock=inner_adapter, name="analytics")]
    )

    async with micro, outer:
        assert (
            micro.get("coordination", "default").lock_backend is outer_adapter
        )
        async with inner:
            assert (
                micro.get("coordination", "default").lock_backend
                is outer_adapter
            )
            assert (
                micro.get("coordination", "analytics").lock_backend
                is inner_adapter
            )
        assert (
            micro.get("coordination", "default").lock_backend is outer_adapter
        )
    assert micro.get("coordination", "default").lock_backend is default


async def test_uses_opens_once_under_concurrent_first_entry() -> None:
    """Concurrent first entries open `uses=` exactly once."""

    class Track:
        def __init__(self) -> None:
            self.entered = 0

        async def __aenter__(self) -> Self:
            await asyncio.sleep(0)  # yield so the second task races in
            self.entered += 1
            return self

        async def __aexit__(self, *exc: object) -> None:
            pass

    track = Track()
    bulkhead = Bulkhead("reports", uses=[track])
    micro = Grelmicro()

    async def worker() -> None:
        async with bulkhead:
            await asyncio.sleep(0.01)

    async with micro:
        await asyncio.gather(worker(), worker())
        assert track.entered == 1


async def test_scope_resolves_per_task_not_per_app() -> None:
    """Two tasks running at the same instant resolve the same key differently.

    The in-scope task sees the bulkhead's component while its sibling sees
    the app's. This is why an ambient resolution can never be cached on the
    pattern object.
    """
    default = MemoryLockAdapter()
    dedicated = MemoryLockAdapter()
    micro = Grelmicro(uses=[Coordination(lock=default)])
    bulkhead = Bulkhead("checkout", uses=[Coordination(lock=dedicated)])
    shared = Lock("cart")
    barrier = asyncio.Barrier(2)

    async def in_scope() -> object:
        async with bulkhead:
            await barrier.wait()
            return shared.backend

    async def sibling() -> object:
        await barrier.wait()
        return shared.backend

    async with micro:
        inside, outside = await asyncio.gather(in_scope(), sibling())

    assert inside is dedicated
    assert outside is default


def test_scope_resolves_per_event_loop() -> None:
    """Each event loop resolves its own app and its own bulkhead components.

    One module-level pattern object is shared by both loops, which is the
    documented idiom, so the resolution has to stay per context.
    """
    shared = Lock("cart")
    seen: dict[str, tuple[bool, bool]] = {}

    def run(tag: str) -> None:
        async def main() -> None:
            default = MemoryLockAdapter()
            dedicated = MemoryLockAdapter()
            micro = Grelmicro(uses=[Coordination(lock=default)])
            bulkhead = Bulkhead(tag, uses=[Coordination(lock=dedicated)])
            async with micro:
                app_ok = shared.backend is default
                async with bulkhead:
                    scope_ok = shared.backend is dedicated
            seen[tag] = (app_ok, scope_ok)

        asyncio.run(main())

    threads = [
        threading.Thread(target=run, args=(tag,)) for tag in ("left", "right")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert seen == {"left": (True, True), "right": (True, True)}


async def test_a_permit_is_returned_when_the_scope_cannot_open() -> None:
    """A provider that is down must not cost a permit for good."""
    bulkhead = Bulkhead("uses-fail", max_concurrent=1)

    async def refuse() -> None:
        msg = "provider down"
        raise ConnectionError(msg)

    bulkhead._uses = (object(),)
    bulkhead._open_uses = refuse  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]

    for _ in range(2):
        with pytest.raises(ConnectionError):
            await bulkhead.__aenter__()

    semaphore = bulkhead._state.semaphore
    assert semaphore is not None
    assert semaphore._value == 1


async def test_a_permit_is_returned_when_the_scope_push_fails() -> None:
    """Everything after the permit is taken must give it back on failure."""
    bulkhead = Bulkhead("push-fail", max_concurrent=1)

    def no_task() -> object:
        msg = "no running task"
        raise RuntimeError(msg)

    bulkhead._overrides = {("cache", "default"): cast("Any", object())}

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(bulkhead_module, "_current_task", no_task)
        for _ in range(2):
            with pytest.raises(RuntimeError, match="no running task"):
                await bulkhead.__aenter__()

    semaphore = bulkhead._state.semaphore
    assert semaphore is not None
    assert semaphore._value == 1
    assert bulkhead_module._active_bulkhead.get(None) is None


async def test_a_partly_opened_scope_resumes_where_it_stopped() -> None:
    """An item already entered must not be entered a second time."""
    entered: list[str] = []

    class Item:
        """A scope item that records its entries."""

        def __init__(self, label: str, *, fails: bool) -> None:
            self.label = label
            self.fails = fails

        async def __aenter__(self) -> Self:
            entered.append(self.label)
            if self.fails:
                self.fails = False
                msg = "server briefly down"
                raise ConnectionError(msg)
            return self

        async def __aexit__(self, *_: object) -> None:
            """Leave the scope."""

    micro = Grelmicro()
    async with micro:
        bulkhead = Bulkhead(
            "resume",
            max_concurrent=1,
            uses=[Item("first", fails=False), Item("second", fails=True)],
        )
        with pytest.raises(ConnectionError):
            await bulkhead.__aenter__()

        async with bulkhead:
            pass

    assert entered == ["first", "second", "second"]


async def test_a_second_app_opens_the_scope_again() -> None:
    """The scope belongs to one app, so the next app opens it from scratch."""

    class Track:
        """A scope item that counts its entries and exits."""

        def __init__(self) -> None:
            self.entered = 0
            self.exited = 0

        async def __aenter__(self) -> Self:
            self.entered += 1
            return self

        async def __aexit__(self, *_: object) -> None:
            self.exited += 1

    track = Track()
    bulkhead = Bulkhead("reports", uses=[track])

    for _ in range(APPS):
        async with Grelmicro():
            async with bulkhead:
                pass
            async with bulkhead:
                pass

    assert track.entered == APPS  # once per app, not once per entry
    assert track.exited == APPS


async def test_a_partly_opened_scope_does_not_resume_in_the_next_app() -> None:
    """A failed open in one app leaves no resume index for the next."""
    entered: list[str] = []

    class Item:
        """A scope item that records its entries."""

        def __init__(self, label: str, *, fails: bool) -> None:
            self.label = label
            self.fails = fails

        async def __aenter__(self) -> Self:
            entered.append(self.label)
            if self.fails:
                self.fails = False
                msg = "server briefly down"
                raise ConnectionError(msg)
            return self

        async def __aexit__(self, *_: object) -> None:
            """Leave the scope."""

    bulkhead = Bulkhead(
        "resume",
        uses=[Item("first", fails=False), Item("second", fails=True)],
    )

    async with Grelmicro():
        with pytest.raises(ConnectionError):
            await bulkhead.__aenter__()

    async with Grelmicro(), bulkhead:
        pass

    assert entered == ["first", "second", "first", "second"]


async def test_a_failed_first_item_arms_one_reset_per_app() -> None:
    """A retried open pushes the reset once, not once per attempt."""

    class Item:
        """A scope item that fails a set number of times."""

        def __init__(self, failures: int) -> None:
            self.failures = failures

        async def __aenter__(self) -> Self:
            if self.failures:
                self.failures -= 1
                msg = "server briefly down"
                raise ConnectionError(msg)
            return self

        async def __aexit__(self, *_: object) -> None:
            """Leave the scope."""

    attempts = 3
    bulkhead = Bulkhead("retry", uses=[Item(attempts)])
    forget = bulkhead._forget_scope
    resets = 0

    def counting_forget(
        micro: Grelmicro, scope: bulkhead_module._Scope
    ) -> None:
        nonlocal resets
        resets += 1
        forget(micro, scope)

    bulkhead._forget_scope = counting_forget  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]

    async with Grelmicro() as micro:
        for _ in range(attempts):
            with pytest.raises(ConnectionError):
                await bulkhead.__aenter__()
        async with bulkhead:
            pass
        assert len(micro._scoped_uses) == 1

    assert resets == 1  # pushed once per app, not once per attempt
    assert bulkhead._opened_for is bulkhead_module._UNSCOPED
    assert micro._scoped_uses == {}


async def test_overlapping_apps_keep_separate_scope_records() -> None:
    """Two apps active at once each keep their own record of the scope."""
    opens = 0

    class Track:
        """A scope item that counts opens."""

        async def __aenter__(self) -> Self:
            nonlocal opens
            opens += 1
            return self

        async def __aexit__(self, *_: object) -> None:
            """Leave the scope."""

    bulkhead = Bulkhead("shared", uses=[Track()])
    inner = Grelmicro(allow_multiple=True)
    outer = Grelmicro(allow_multiple=True)

    async with outer:
        async with bulkhead:
            pass
        assert opens == 1
        async with inner, bulkhead:
            pass
        # Each app opened against its own record, so neither read the
        # other's as its own.
        assert opens == LIMIT
        assert len(outer._scoped_uses) == 1
        assert inner._scoped_uses == {}
        # The other app's shutdown left this one's record alone.
        async with bulkhead:
            pass
        assert opens == LIMIT


async def test_a_scope_closing_mid_open_does_not_report_itself_open() -> None:
    """Shutdown during an in-flight open leaves no stale open scope behind."""
    started = asyncio.Event()
    release = asyncio.Event()
    opened: list[str] = []
    closed: list[str] = []

    class Slow:
        """A scope item that blocks until it is released."""

        def __init__(self, label: str) -> None:
            self.label = label

        async def __aenter__(self) -> Self:
            started.set()
            await release.wait()
            opened.append(self.label)
            return self

        async def __aexit__(self, *_: object) -> None:
            closed.append(self.label)

    bulkhead = Bulkhead("slow", uses=[Slow("first"), Slow("second")])
    micro = Grelmicro()

    async def enter() -> None:
        async with bulkhead:
            pass

    await micro.__aenter__()
    task = asyncio.create_task(enter())
    await started.wait()
    # Shut the app down while the open is parked on the first item.
    await micro.__aexit__(None, None, None)
    release.set()
    with pytest.raises(OutOfContextError, match="closing with the app"):
        await task

    assert opened == ["first"]  # the second item was never started
    assert closed == ["first"]  # the first closed itself, the stack was gone
    # Nothing claims the scope is open, so the next app opens it again.
    assert bulkhead._opened_for is bulkhead_module._UNSCOPED
    assert micro._scoped_uses == {}


async def test_entering_a_scope_that_closed_with_its_app_raises() -> None:
    """An entry made after the owning app unwound names the shutdown."""
    opens = 0

    class Track:
        """A scope item that counts opens."""

        async def __aenter__(self) -> Self:
            nonlocal opens
            opens += 1
            return self

        async def __aexit__(self, *_: object) -> None:
            """Leave the scope."""

    bulkhead = Bulkhead("drain", uses=[Track()])
    drained: list[BaseException] = []

    class Flusher:
        """An app item that drains through the bulkhead on shutdown."""

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            try:
                async with bulkhead:
                    pass
            except OutOfContextError as exc:
                drained.append(exc)

    async with Grelmicro(uses=[Flusher()]):
        async with bulkhead:
            pass
        assert opens == 1

    assert opens == 1  # the drain did not rebuild the closed scope
    assert "closing with the app that opened it" in str(drained[0])


def test_the_open_lock_is_built_on_the_app_event_loop() -> None:
    """A bulkhead reused across event loops does not carry a bound lock."""
    bulkhead = Bulkhead("cli", uses=[_Gate()])

    async def run() -> None:
        async with Grelmicro():
            # Two tasks race the first entry, which binds the scope lock.
            await asyncio.gather(*(_enter(bulkhead) for _ in range(LIMIT)))

    asyncio.run(run())
    asyncio.run(run())


class _Gate:
    """A scope item that yields control so a racing entry queues behind it."""

    async def __aenter__(self) -> Self:
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *_: object) -> None:
        """Leave the scope."""


async def _enter(bulkhead: Bulkhead) -> None:
    """Enter and leave the bulkhead once."""
    async with bulkhead:
        pass
