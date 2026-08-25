"""Tests for the Bulkhead concurrency-isolation pattern."""

import asyncio
import contextvars
import threading
from contextlib import AsyncExitStack
from typing import Any, Self, cast

import pytest

from grelmicro import (
    AmbiguousProviderError,
    BackendScopeError,
    ComponentAlreadyRegisteredError,
    ComponentNotRegisteredError,
    Grelmicro,
    NoActiveAppError,
    OutOfContextError,
)
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.coordination import Coordination, Lock
from grelmicro.coordination.memory import MemoryLockAdapter
from grelmicro.log import Log
from grelmicro.providers._base import Provider
from grelmicro.providers.memory import MemoryProvider
from grelmicro.providers.postgres import PostgresProvider
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
_LAZY_URL = "postgresql://user:pass@localhost:5499/db"
"""A URL whose pool is only built on enter, so ordering can be read cold."""


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


async def test_uses_wraps_a_bare_backend_like_the_app() -> None:
    """A bare backend in `uses=` becomes the Component a Pattern resolves."""
    default = MemoryLockAdapter()
    dedicated = MemoryLockAdapter()
    micro = Grelmicro(uses=[Coordination(lock=default)])
    bulkhead = Bulkhead("reports", uses=[dedicated])

    async with micro, bulkhead:
        assert micro.get("coordination", "default").lock_backend is dedicated


async def test_uses_scopes_the_kinds_a_bare_provider_serves() -> None:
    """A bare Provider in `uses=` overrides the kinds it can serve."""
    default = MemoryLockAdapter()
    micro = Grelmicro(uses=[Coordination(lock=default)])
    bulkhead = Bulkhead("reports", uses=[MemoryProvider()])

    async with micro, bulkhead:
        scoped = micro.get("coordination", "default")
        assert scoped.lock_backend is not default


async def test_uses_leaves_a_kind_an_explicit_component_claims() -> None:
    """An explicit Component wins over the default a Provider would give."""
    dedicated = MemoryLockAdapter()
    micro = Grelmicro(uses=[Coordination(lock=MemoryLockAdapter())])
    bulkhead = Bulkhead(
        "reports", uses=[MemoryProvider(), Coordination(lock=dedicated)]
    )

    async with micro, bulkhead:
        assert micro.get("coordination", "default").lock_backend is dedicated


def test_uses_refuses_two_bare_providers() -> None:
    """Two bare Providers cannot both be the default for a shared kind."""
    with pytest.raises(AmbiguousProviderError, match="multiple providers"):
        Bulkhead("reports", uses=[MemoryProvider(), MemoryProvider()])


async def test_uses_fills_no_defaults_for_two_providers_with_a_component() -> (
    None
):
    """With a Component listed, two Providers are lifecycle only."""
    default = MemoryLockAdapter()
    micro = Grelmicro(uses=[Coordination(lock=default)])
    bulkhead = Bulkhead(
        "reports",
        uses=[
            MemoryProvider(),
            MemoryProvider(),
            Cache(MemoryCacheAdapter()),
        ],
    )

    async with micro, bulkhead:
        # Neither provider filled `coordination`, so the app's lock stands.
        assert micro.get("coordination", "default").lock_backend is default


async def test_uses_checks_the_backends_a_provider_fills() -> None:
    """The filled Components face the same backend check as listed ones."""
    bulkhead = Bulkhead("reports", uses=[MemoryProvider()])

    async with Grelmicro(environment="production"):
        with pytest.raises(BackendScopeError):
            async with bulkhead:
                pass


def test_uses_leaves_out_a_provider_it_was_not_given() -> None:
    """A borrowed Provider the list omits is never adopted into the scope.

    The scope opens on the app's exit stack, above the app's own items, so
    adopting a Provider the app already holds would close it too early.
    """
    provider = PostgresProvider(url=_LAZY_URL)
    bulkhead = Bulkhead("reports", uses=[Cache(provider)])

    assert all(item is not provider for item in bulkhead._uses)


def test_uses_opens_a_provider_listed_after_its_component() -> None:
    """A Provider listed late moves ahead of the Component that holds it."""
    provider = PostgresProvider(url=_LAZY_URL)
    bulkhead = Bulkhead("reports", uses=[Cache(provider), provider])

    assert bulkhead._uses[0] is provider
    assert sum(item is provider for item in bulkhead._uses) == 1


def test_uses_refuses_two_components_with_one_key() -> None:
    """A shadowed Component would open a pool nothing could resolve to."""
    with pytest.raises(ComponentAlreadyRegisteredError, match="listed twice"):
        Bulkhead(
            "reports",
            uses=[
                Coordination(lock=MemoryLockAdapter()),
                Coordination(lock=MemoryLockAdapter()),
            ],
        )


def test_uses_takes_the_same_component_listed_twice() -> None:
    """One instance named twice is one Component, not a clash."""
    coordination = Coordination(lock=MemoryLockAdapter())
    bulkhead = Bulkhead("reports", uses=[coordination, coordination])

    assert bulkhead._overrides[("coordination", "default")] is coordination


def test_uses_refuses_two_of_a_singleton_kind() -> None:
    """A kind only one Component may hold is refused twice, as in the app."""
    with pytest.raises(ComponentAlreadyRegisteredError, match="singleton"):
        Bulkhead("reports", uses=[Log(), Log(name="second")])


async def test_uses_opens_one_entry_listed_twice_once() -> None:
    """One instance listed twice opens once, so it cannot close twice."""
    opens = 0
    closes = 0

    class Track:
        """A scope item that counts its own lifecycle."""

        async def __aenter__(self) -> Self:
            nonlocal opens
            opens += 1
            return self

        async def __aexit__(self, *_: object) -> None:
            nonlocal closes
            closes += 1

    item = Track()
    bulkhead = Bulkhead("dup", uses=[item, item])

    assert len(bulkhead._uses) == 1

    async with Grelmicro(), bulkhead:
        pass

    assert (opens, closes) == (1, 1)


async def test_uses_leaves_a_provider_the_app_holds_alone() -> None:
    """A Provider the app already opened is not opened, or closed, again."""
    provider = _BorrowedProvider()
    bulkhead = Bulkhead("shared", uses=[provider, Cache(provider)])

    async with Grelmicro(uses=[provider]), bulkhead:
        pass

    assert provider.log == ["open", "close"]


async def test_uses_names_a_borrowed_provider_nothing_opens() -> None:
    """A Component borrowing a Provider no list carries says so on entry."""
    bulkhead = Bulkhead("omitted", uses=[Cache(_BorrowedProvider())])

    async with Grelmicro():
        with pytest.raises(OutOfContextError, match="nothing opens"):
            async with bulkhead:
                pass


async def test_uses_accepts_a_borrowed_provider_the_app_opens() -> None:
    """Listing the Provider on the app is one of the two documented ways."""
    provider = _BorrowedProvider()
    bulkhead = Bulkhead("borrowing", uses=[Cache(provider)])

    async with Grelmicro(uses=[provider]), bulkhead:
        pass

    assert provider.log == ["open", "close"]


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


def test_uses_refuses_something_that_is_not_a_scope() -> None:
    """A `uses=` entry that cannot be opened is named at construction."""
    with pytest.raises(TypeError, match="not an async context manager"):
        Bulkhead("wired-wrong", uses=[object()])  # ty: ignore[invalid-argument-type]


async def test_uses_requires_active_app() -> None:
    """Entering a `uses=` bulkhead without an app raises."""
    bulkhead = Bulkhead(
        "checkout", uses=[Coordination(lock=MemoryLockAdapter())]
    )
    with pytest.raises(NoActiveAppError, match="no active Grelmicro app"):
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

    bulkhead._uses = (_Gate(),)
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


async def test_a_retried_open_keeps_one_record_per_app() -> None:
    """A retried open reuses the app's record rather than starting a new one."""

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

    async with Grelmicro() as micro:
        for _ in range(attempts):
            with pytest.raises(ConnectionError):
                await bulkhead.__aenter__()
        assert len(micro._scoped_uses) == 1
        async with bulkhead:
            pass
        assert len(micro._scoped_uses) == 1

    assert micro._scoped_uses == {}


async def test_an_entry_while_the_scope_unwinds_raises() -> None:
    """A call landing while the app closes its items does not run against them."""
    closing = asyncio.Event()
    release = asyncio.Event()

    class Slow:
        """A scope item that blocks the app inside its own exit."""

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            closing.set()
            await release.wait()

    bulkhead = Bulkhead("unwind", uses=[Slow()])
    micro = Grelmicro()

    async def enter() -> None:
        await closing.wait()
        with pytest.raises(OutOfContextError, match="closing with the app run"):
            async with bulkhead:
                pass
        release.set()

    async with micro:
        async with bulkhead:
            pass
        task = asyncio.create_task(enter())

    await task


async def test_an_entry_after_shutdown_raises_from_an_inherited_context() -> (
    None
):
    """A task that outlives its app names the shutdown, not a bare failure."""
    bulkhead = Bulkhead("outlived", uses=[_Gate()])
    micro = Grelmicro()
    entered = asyncio.Event()
    resume = asyncio.Event()
    raised: list[BaseException] = []

    async def outlive() -> None:
        entered.set()
        await resume.wait()
        # The app is gone, but this task's context still resolves to it.
        try:
            async with bulkhead:
                pass
        except OutOfContextError as exc:
            raised.append(exc)

    async with micro:
        task = asyncio.create_task(outlive())
        await entered.wait()

    resume.set()
    await task

    assert micro._exit_stack is None
    assert "had shut down" in str(raised[0])


async def test_an_overlapping_run_borrows_the_open_scope() -> None:
    """A second live run uses the open items rather than opening its own."""
    opens = 0
    closes = 0

    class Track:
        """A scope item that counts opens and closes."""

        async def __aenter__(self) -> Self:
            nonlocal opens
            opens += 1
            return self

        async def __aexit__(self, *_: object) -> None:
            nonlocal closes
            closes += 1

    bulkhead = Bulkhead("shared", uses=[Track()])

    async with Grelmicro(allow_multiple=True):
        async with bulkhead:
            pass
        assert opens == 1
        # Only the run that owns the scope enters the items, so this one
        # cannot close them under the outer run when it exits.
        async with Grelmicro(allow_multiple=True), bulkhead:
            pass
        assert opens == 1
        assert closes == 0
        # The outer run still holds a working scope.
        async with bulkhead:
            pass
        assert opens == 1

    assert closes == 1


async def test_a_borrowing_run_gets_its_own_backend_check() -> None:
    """The check runs against the run that borrows, not the one that opened."""
    lenient = Grelmicro(allow_multiple=True, environment="development")
    strict = Grelmicro(allow_multiple=True, environment="production")
    bulkhead = Bulkhead(
        "checked", uses=[Coordination(lock=MemoryLockAdapter())]
    )

    async with lenient:
        async with bulkhead:
            pass
        async with strict:
            # A memory backend passes in development and not in production,
            # so borrowing must not inherit the owner's verdict.
            with pytest.raises(BackendScopeError):
                async with bulkhead:
                    pass


async def test_a_borrowing_run_is_checked_once_not_per_entry() -> None:
    """A recorded borrow keeps the backend check off every later entry."""
    checks = 0
    real = bulkhead_module.unmet_requirements

    def counting(items: object) -> object:
        nonlocal checks
        checks += 1
        return real(items)  # ty: ignore[invalid-argument-type]

    bulkhead = Bulkhead(
        "counted", uses=[Coordination(lock=MemoryLockAdapter())]
    )
    entries = 5

    async with Grelmicro(allow_multiple=True):
        async with bulkhead:
            pass
        async with Grelmicro(allow_multiple=True):
            bulkhead_module.unmet_requirements = counting  # ty: ignore[invalid-assignment]
            try:
                for _ in range(entries):
                    async with bulkhead:
                        pass
            finally:
                bulkhead_module.unmet_requirements = real

    assert checks == 1  # recorded on the first borrow, not repeated


async def test_a_failed_open_gives_the_scope_back() -> None:
    """An open that entered nothing does not keep the scope from other runs."""
    opens = 0

    class Brittle:
        """A scope item that fails the first time it is entered."""

        def __init__(self) -> None:
            self.fails = True

        async def __aenter__(self) -> Self:
            nonlocal opens
            if self.fails:
                self.fails = False
                msg = "server briefly down"
                raise ConnectionError(msg)
            opens += 1
            return self

        async def __aexit__(self, *_: object) -> None:
            """Leave the scope."""

    bulkhead = Bulkhead("brittle-open", uses=[Brittle()])

    async with Grelmicro(allow_multiple=True):
        with pytest.raises(ConnectionError):
            async with bulkhead:
                pass
        assert bulkhead._scoped_to is None  # the failed claim was given back
        # Another run is free to open the scope for itself.
        async with Grelmicro(allow_multiple=True), bulkhead:
            pass
        assert opens == 1


async def test_a_finished_run_leaves_no_borrow_behind() -> None:
    """The owner does not collect a record per run that borrowed from it."""

    class Track:
        """A scope item that can be opened."""

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            """Leave the scope."""

    bulkhead = Bulkhead("collector", uses=[Track()])
    borrows = 3

    async with Grelmicro(allow_multiple=True) as owner:
        async with bulkhead:
            pass
        for _ in range(borrows):
            async with Grelmicro(allow_multiple=True), bulkhead:
                pass
        held = owner._scoped_uses[bulkhead]
        assert held.borrowers == set()


async def test_a_borrowed_scope_is_reopened_once_its_owner_goes() -> None:
    """A run that outlives the owner opens the scope for itself."""
    opens = 0

    class Track:
        """A scope item that counts opens."""

        async def __aenter__(self) -> Self:
            nonlocal opens
            opens += 1
            return self

        async def __aexit__(self, *_: object) -> None:
            """Leave the scope."""

    bulkhead = Bulkhead("outliving", uses=[Track()])
    survivor = Grelmicro(allow_multiple=True)

    async with survivor:
        async with Grelmicro(allow_multiple=True) as owner:
            async with bulkhead:
                pass
            assert opens == 1
            # Borrow from the survivor while the owner is still running.
            token = bulkhead_module._current_micro.set(survivor)
            try:
                async with bulkhead:
                    pass
                assert opens == 1  # borrowed, not opened again
                held = owner._scoped_uses[bulkhead]
                assert len(held.borrowers) == 1
            finally:
                bulkhead_module._current_micro.reset(token)

        # The owner forgot the borrow on its way out, so this run does not
        # keep reporting a scope that closed with it.
        assert survivor._scoped_uses[bulkhead].opened is False
        async with bulkhead:
            pass
        assert opens == LIMIT


async def test_repeated_failed_opens_arm_one_release() -> None:
    """A retried open does not stack a release callback per attempt."""

    class Brittle:
        """A scope item that always fails to open."""

        async def __aenter__(self) -> Self:
            msg = "server down"
            raise ConnectionError(msg)

        async def __aexit__(self, *_: object) -> None:
            """Leave the scope."""

    bulkhead = Bulkhead("flapping", uses=[Brittle()])
    release = bulkhead._release_scope
    releases = 0
    attempts = 20

    def counting_release(exit_stack: AsyncExitStack) -> None:
        nonlocal releases
        releases += 1
        release(exit_stack)

    bulkhead._release_scope = counting_release  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]

    async with Grelmicro() as micro:
        for _ in range(attempts):
            with pytest.raises(ConnectionError):
                async with bulkhead:
                    pass
        assert len(micro._scoped_uses) == 1

    assert releases == 1  # armed once per run, not once per attempt


async def test_a_failing_borrow_check_records_nothing() -> None:
    """A borrow refused by the backend check leaves no trace to repeat."""
    bulkhead = Bulkhead(
        "refused", uses=[Coordination(lock=MemoryLockAdapter())]
    )
    drop = bulkhead._drop_borrow
    drops = 0
    attempts = 10

    def counting_drop(borrowed: object) -> None:
        nonlocal drops
        drops += 1
        drop(borrowed)  # ty: ignore[invalid-argument-type]

    bulkhead._drop_borrow = counting_drop  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]

    async with Grelmicro(allow_multiple=True, environment="development") as own:
        async with bulkhead:
            pass
        held = own._scoped_uses[bulkhead]
        async with Grelmicro(
            allow_multiple=True, environment="production"
        ) as borrower:
            for _ in range(attempts):
                with pytest.raises(BackendScopeError):
                    async with bulkhead:
                        pass
            assert bulkhead not in borrower._scoped_uses

        assert held.borrowers == set()

    assert drops == 0  # nothing was registered, so nothing had to be dropped


async def test_a_part_opened_scope_says_so_to_another_run() -> None:
    """A stopped open is not reported to another run as one in flight."""
    entered: list[str] = []

    class Item:
        """A scope item that fails the first time it is entered."""

        def __init__(self, label: str, *, fails: bool) -> None:
            self.label = label
            self.fails = fails

        async def __aenter__(self) -> Self:
            if self.fails:
                self.fails = False
                msg = "server briefly down"
                raise ConnectionError(msg)
            entered.append(self.label)
            return self

        async def __aexit__(self, *_: object) -> None:
            """Leave the scope."""

    bulkhead = Bulkhead(
        "part-open",
        uses=[Item("first", fails=False), Item("second", fails=True)],
    )

    async with Grelmicro(allow_multiple=True):
        with pytest.raises(ConnectionError):
            async with bulkhead:
                pass
        assert entered == ["first"]
        # Nothing is in flight, so the other run is not told to wait for one.
        async with Grelmicro(allow_multiple=True):
            with pytest.raises(OutOfContextError, match="stopped part way"):
                async with bulkhead:
                    pass


async def test_a_scope_forgotten_mid_borrow_is_refused() -> None:
    """A borrow that loses the scope while checking does not record itself."""

    class Track:
        """A scope item that can be opened."""

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            """Leave the scope."""

    bulkhead = Bulkhead("vanishing", uses=[Track()])

    async with Grelmicro(allow_multiple=True) as owner:
        async with bulkhead:
            pass
        held = owner._scoped_uses[bulkhead]

        def close_the_scope(*_: object) -> None:
            """Stand in for the owner unwinding during the borrow check."""
            owner._closing = True
            bulkhead._forget_scope(held)

        async with Grelmicro(allow_multiple=True):
            original = bulkhead_module.report_unmet_requirements
            bulkhead_module.report_unmet_requirements = close_the_scope  # ty: ignore[invalid-assignment]
            try:
                with pytest.raises(
                    OutOfContextError, match="is closing its uses="
                ):
                    async with bulkhead:
                        pass
            finally:
                bulkhead_module.report_unmet_requirements = original

        assert held.borrowers == set()


async def test_borrowing_across_owner_turnover_arms_one_drop() -> None:
    """A run that keeps borrowing does not stack a record per owner."""
    opens = 0

    class Track:
        """A scope item that counts opens."""

        async def __aenter__(self) -> Self:
            nonlocal opens
            opens += 1
            return self

        async def __aexit__(self, *_: object) -> None:
            """Leave the scope."""

    bulkhead = Bulkhead("turnover", uses=[Track()])
    drop = bulkhead._drop_borrow
    drops = 0
    turnovers = 5

    def counting_drop(borrowed: object) -> None:
        nonlocal drops
        drops += 1
        drop(borrowed)  # ty: ignore[invalid-argument-type]

    bulkhead._drop_borrow = counting_drop  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]
    survivor = Grelmicro(allow_multiple=True)

    async with survivor:
        for _ in range(turnovers):
            # A short-lived run owns the scope, then this one borrows it.
            async with Grelmicro(allow_multiple=True):
                async with bulkhead:
                    pass
                # Borrow it from the run that outlives every owner.
                token = bulkhead_module._current_micro.set(survivor)
                try:
                    async with bulkhead:
                        pass
                finally:
                    bulkhead_module._current_micro.reset(token)
        assert opens == turnovers  # one open per owner, borrowed each time

    assert drops == 1  # armed once for this run, not once per owner


async def test_a_dropped_borrow_stops_reporting_the_scope_open() -> None:
    """A run that drains after its borrow was dropped does not reuse it."""
    log: list[str] = []
    draining = asyncio.Event()
    owner_closed = asyncio.Event()

    class Track:
        """A scope item that records its own lifecycle."""

        async def __aenter__(self) -> Self:
            log.append("open")
            return self

        async def __aexit__(self, *_: object) -> None:
            log.append("close")

    bulkhead = Bulkhead("drained", uses=[Track()])

    class Drainer:
        """An app item that drains once the owner of the scope has gone."""

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            draining.set()
            await owner_closed.wait()
            try:
                async with bulkhead:
                    log.append("drain used the scope")
            except OutOfContextError:
                log.append("drain refused")

    async def borrow_then_drain() -> None:
        async with Grelmicro(uses=[Drainer()]), bulkhead:
            pass

    async with Grelmicro():
        async with bulkhead:
            pass
        task = asyncio.create_task(borrow_then_drain())
        # The borrow is dropped on the way out, before `Drainer` drains.
        await draining.wait()

    owner_closed.set()
    await task

    assert log == ["open", "close", "drain refused"]


async def test_a_multi_item_scope_forgets_itself_once() -> None:
    """One forget covers the scope, and still runs before any item closes."""
    log: list[str] = []

    class Track:
        """A scope item that records when it closes."""

        def __init__(self, label: str) -> None:
            self.label = label

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            log.append(f"close {self.label}")

    bulkhead = Bulkhead("pair", uses=[Track("first"), Track("second")])
    forget = bulkhead._forget_scope
    forgets = 0

    def counting_forget(scope: object) -> None:
        nonlocal forgets
        forgets += 1
        log.append("forget")
        forget(scope)  # ty: ignore[invalid-argument-type]

    bulkhead._forget_scope = counting_forget  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]

    async with Grelmicro(), bulkhead:
        pass

    assert forgets == 1  # one callback for the scope, not one per item
    assert log == ["forget", "close second", "close first"]


async def test_a_startup_rollback_that_fails_still_clears_the_scope() -> None:
    """An item that fails to close does not strand a resume index."""
    bulkhead = Bulkhead("rollback", uses=[_Gate()])
    micro = Grelmicro(uses=[_WarmupThenFailClose(bulkhead), _FailStartOnce()])

    with pytest.raises(ConnectionError, match="close failed"):
        await micro.__aenter__()

    # The rollback cleared its own state, so the app can start again and
    # the scope opens from the start rather than resuming at item one.
    assert micro._exit_stack is None
    assert micro._scoped_uses == {}
    async with micro, bulkhead:
        pass


async def test_two_apps_starting_at_once_do_not_both_take_the_scope() -> None:
    """The scope is claimed before the first item, so a race has one winner."""
    opens = 0
    opening = asyncio.Event()
    release = asyncio.Event()

    class Slow:
        """A scope item that parks the app that is opening it."""

        async def __aenter__(self) -> Self:
            nonlocal opens
            opening.set()
            await release.wait()
            opens += 1
            return self

        async def __aexit__(self, *_: object) -> None:
            """Leave the scope."""

    bulkhead = Bulkhead("racing", uses=[Slow()])
    refused = 0

    async def second() -> None:
        nonlocal refused
        await opening.wait()
        async with Grelmicro():
            # The first app claimed the scope before it started the item.
            with pytest.raises(OutOfContextError, match="still opening its"):
                async with bulkhead:
                    pass
            refused += 1
        release.set()

    task = asyncio.create_task(second())
    async with Grelmicro(), bulkhead:
        pass
    await task

    assert opens == 1  # the item was entered once, not once per app
    assert refused == 1


async def test_a_parked_open_keeps_the_scope_until_it_finishes() -> None:
    """An entry still in flight holds the scope its app has already left."""
    opens = 0
    closes = 0
    opening = asyncio.Event()
    release = asyncio.Event()

    class Slow:
        """A scope item that parks the app that is opening it."""

        async def __aenter__(self) -> Self:
            nonlocal opens
            opening.set()
            await release.wait()
            opens += 1
            return self

        async def __aexit__(self, *_: object) -> None:
            nonlocal closes
            closes += 1

    bulkhead = Bulkhead("inflight", uses=[Slow()])
    micro = Grelmicro()

    async def enter() -> None:
        with pytest.raises(OutOfContextError, match="had shut down"):
            async with bulkhead:
                pass

    await micro.__aenter__()
    task = asyncio.create_task(enter())
    await opening.wait()
    # The owner unwinds while that entry is still inside the item.
    await micro.__aexit__(None, None, None)

    async with Grelmicro():
        # The exit stack is done, but the entry is not, so it still holds it.
        with pytest.raises(OutOfContextError, match="a run that has shut down"):
            async with bulkhead:
                pass
        release.set()
        await task
        # The entry closed the item it opened and handed the scope back.
        assert closes == 1
        async with bulkhead:
            pass
        assert opens == LIMIT

    assert closes == LIMIT
    assert bulkhead._scoped_to is None


async def test_a_second_app_waits_for_every_item_to_close() -> None:
    """The scope frees up only once the app that held it closed its items."""
    order: list[str] = []
    closing = asyncio.Event()
    release = asyncio.Event()

    class Slow:
        """A scope item that blocks inside its own exit."""

        async def __aenter__(self) -> Self:
            order.append("open")
            return self

        async def __aexit__(self, *_: object) -> None:
            closing.set()
            await release.wait()
            order.append("close")

    bulkhead = Bulkhead("handoff", uses=[Slow()])

    async def take() -> None:
        await closing.wait()
        async with Grelmicro(allow_multiple=True):
            # The first app is still inside the item's exit.
            with pytest.raises(OutOfContextError, match="is closing its uses="):
                async with bulkhead:
                    pass
        order.append("refused")
        release.set()

    task = asyncio.create_task(take())
    async with Grelmicro(allow_multiple=True), bulkhead:
        pass
    await task

    assert order == ["open", "refused", "close"]


async def test_the_next_app_takes_the_scope_the_last_one_released() -> None:
    """Refusing a second live app does not strand the scope after shutdown."""
    opens = 0

    class Track:
        """A scope item that counts opens."""

        async def __aenter__(self) -> Self:
            nonlocal opens
            opens += 1
            return self

        async def __aexit__(self, *_: object) -> None:
            """Leave the scope."""

    bulkhead = Bulkhead("handover", uses=[Track()])

    for _ in range(APPS):
        async with Grelmicro(), bulkhead:
            pass

    assert opens == APPS
    assert bulkhead._scoped_to is None


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
    with pytest.raises(OutOfContextError, match="had shut down"):
        await task

    assert opened == ["first"]  # the second item was never started
    assert closed == ["first"]  # the first closed itself, the stack was gone
    # Nothing claims the scope is open, so the next app opens it again.
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
    assert "closing with the app run that owns it" in str(drained[0])


async def test_a_startup_opened_scope_still_drains_at_shutdown() -> None:
    """An app item drains through a scope its own items still hold open."""
    closed: list[str] = []
    drained: list[str] = []

    class Track:
        """A scope item that records when it closes."""

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            closed.append("scope")

    bulkhead = Bulkhead("db", uses=[Track()])

    class Warmup:
        """An app item that opens the scope during startup."""

        async def __aenter__(self) -> Self:
            async with bulkhead:
                pass
            return self

        async def __aexit__(self, *_: object) -> None:
            """Leave the scope."""

    class Server:
        """A later app item that drains through the bulkhead on shutdown."""

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            async with bulkhead:
                drained.append("ok")

    async with Grelmicro(uses=[Warmup(), Server()]):
        pass

    # The scope sits below `Server` on the stack, so it is still open when
    # `Server` drains through it.
    assert drained == ["ok"]
    assert closed == ["scope"]


async def test_entering_before_startup_names_startup_not_shutdown() -> None:
    """A call that beats startup is told to wait for it, not that it is late."""
    bulkhead = Bulkhead("early", uses=[_Gate()])
    micro = Grelmicro()
    token = bulkhead_module._current_micro.set(micro)
    try:
        with pytest.raises(OutOfContextError, match="before its app started"):
            async with bulkhead:
                pass
    finally:
        bulkhead_module._current_micro.reset(token)


async def test_an_open_parked_across_a_restart_pushes_nothing() -> None:
    """An entry parked in an item drops it when the app restarts under it."""
    started = asyncio.Event()
    release = asyncio.Event()
    closed: list[str] = []

    class Slow:
        """A scope item that blocks until it is released."""

        async def __aenter__(self) -> Self:
            started.set()
            await release.wait()
            return self

        async def __aexit__(self, *_: object) -> None:
            closed.append("scope")

    bulkhead = Bulkhead("restart", uses=[Slow()])
    micro = Grelmicro()

    async def enter() -> None:
        with pytest.raises(OutOfContextError, match="lost its uses= scope"):
            async with bulkhead:
                pass

    await micro.__aenter__()
    task = asyncio.create_task(enter())
    await started.wait()
    # A whole lifecycle happens while the open is parked on the item.
    await micro.__aexit__(None, None, None)
    await micro.__aenter__()
    release.set()
    await task

    # The item closed itself rather than riding the stack the app left.
    assert closed == ["scope"]
    assert micro._scoped_uses == {}
    assert bulkhead._scoped_to is None
    await micro.__aexit__(None, None, None)
    assert closed == ["scope"]


async def test_a_restarted_app_does_not_take_a_scope_it_still_holds() -> None:
    """A run still draining keeps the scope from the same app's next run."""
    opens = 0
    closes = 0
    opening = asyncio.Event()
    release = asyncio.Event()

    class Slow:
        """A scope item that parks the run that is opening it."""

        async def __aenter__(self) -> Self:
            nonlocal opens
            opening.set()
            await release.wait()
            opens += 1
            return self

        async def __aexit__(self, *_: object) -> None:
            nonlocal closes
            closes += 1

    bulkhead = Bulkhead("reused", uses=[Slow()])
    micro = Grelmicro()

    async def enter() -> None:
        with pytest.raises(OutOfContextError, match="lost its uses= scope"):
            async with bulkhead:
                pass

    await micro.__aenter__()
    task = asyncio.create_task(enter())
    await opening.wait()
    await micro.__aexit__(None, None, None)

    # The same app object starts again while the first run is still parked.
    await micro.__aenter__()
    with pytest.raises(OutOfContextError, match="earlier run of this app"):
        async with bulkhead:
            pass

    release.set()
    await task
    assert closes == 1  # the parked entry closed the item it had opened

    # The earlier run has drained, so this one can take the scope.
    async with bulkhead:
        pass
    assert opens == LIMIT
    await micro.__aexit__(None, None, None)

    assert closes == LIMIT
    assert bulkhead._scope_stack is None
    assert bulkhead._scoped_to is None


async def test_a_failing_close_still_names_the_lost_scope() -> None:
    """An item that fails to close does not hide why the entry was refused."""
    opening = asyncio.Event()
    release = asyncio.Event()

    class Brittle:
        """A scope item that raises from its own exit."""

        async def __aenter__(self) -> Self:
            opening.set()
            await release.wait()
            return self

        async def __aexit__(self, *_: object) -> None:
            msg = "pool already gone"
            raise ConnectionError(msg)

    bulkhead = Bulkhead("brittle", uses=[Brittle()])
    micro = Grelmicro()

    async def enter() -> None:
        with pytest.raises(OutOfContextError, match="had shut down") as exc:
            async with bulkhead:
                pass
        assert isinstance(exc.value.__cause__, ConnectionError)

    await micro.__aenter__()
    task = asyncio.create_task(enter())
    await opening.wait()
    await micro.__aexit__(None, None, None)
    release.set()
    await task

    assert bulkhead._scope_stack is None


async def test_an_unbound_context_uses_the_run_holding_the_scope() -> None:
    """A task with no app binding still enters a scope that is already open."""
    opens = 0

    class Track:
        """A scope item that counts opens."""

        async def __aenter__(self) -> Self:
            nonlocal opens
            opens += 1
            return self

        async def __aexit__(self, *_: object) -> None:
            """Leave the scope."""

    bulkhead = Bulkhead("unbound", uses=[Track()])
    ran = False

    async def unbound() -> None:
        nonlocal ran
        async with bulkhead:
            ran = True

    async with Grelmicro():
        async with bulkhead:
            pass
        # A fresh context carries no `Grelmicro.current()` binding, as a
        # request handler under `install(ambient=False)` does not.
        await asyncio.create_task(unbound(), context=contextvars.Context())

    assert ran is True
    assert opens == 1  # reused the open scope rather than refusing


async def test_a_failed_startup_reads_as_a_shutdown() -> None:
    """An app that started and tore itself down is not waiting to start."""

    class Boom:
        """An app item that fails startup."""

        async def __aenter__(self) -> Self:
            msg = "startup failed"
            raise RuntimeError(msg)

        async def __aexit__(self, *_: object) -> None:
            """Leave the scope."""

    bulkhead = Bulkhead("late", uses=[_Gate()])
    micro = Grelmicro(uses=[Boom()])
    with pytest.raises(RuntimeError, match="startup failed"):
        await micro.__aenter__()

    token = bulkhead_module._current_micro.set(micro)
    try:
        with pytest.raises(OutOfContextError, match="had shut down"):
            async with bulkhead:
                pass
    finally:
        bulkhead_module._current_micro.reset(token)


def test_the_open_lock_is_built_on_the_app_event_loop() -> None:
    """A bulkhead reused across event loops does not carry a bound lock."""
    bulkhead = Bulkhead("cli", uses=[_Gate()])

    async def run() -> None:
        async with Grelmicro():
            # Two tasks race the first entry, which binds the scope lock.
            await asyncio.gather(*(_enter(bulkhead) for _ in range(LIMIT)))

    asyncio.run(run())
    asyncio.run(run())


class _WarmupThenFailClose:
    """An app item that opens a bulkhead scope, then fails to close once."""

    def __init__(self, bulkhead: Bulkhead) -> None:
        self.bulkhead = bulkhead
        self.fails = True

    async def __aenter__(self) -> Self:
        async with self.bulkhead:
            pass
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.fails:
            self.fails = False
            msg = "close failed"
            raise ConnectionError(msg)


class _FailStartOnce:
    """An app item that fails startup the first time only."""

    def __init__(self) -> None:
        self.fails = True

    async def __aenter__(self) -> Self:
        if self.fails:
            self.fails = False
            msg = "startup failed"
            raise RuntimeError(msg)
        return self

    async def __aexit__(self, *_: object) -> None:
        """Leave the scope."""


class _BorrowedProvider(Provider):
    """A Provider whose adapters borrow it rather than own it."""

    short_name = "borrowed"

    def __init__(self) -> None:
        self.log: list[str] = []

    async def __aenter__(self) -> Self:
        self.log.append("open")
        return self

    async def __aexit__(self, *_: object) -> None:
        self.log.append("close")

    def cache(self, **_: object) -> MemoryCacheAdapter:
        adapter = MemoryCacheAdapter()
        adapter._provider = self  # ty: ignore[unresolved-attribute]
        adapter._owns_provider = False  # ty: ignore[unresolved-attribute]
        return adapter


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
