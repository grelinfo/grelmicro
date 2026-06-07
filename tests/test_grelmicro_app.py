"""Tests for the Grelmicro app container and Component protocol."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar, Self

import pytest

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType

    from grelmicro.coordination.abc import LeaderRecord

from grelmicro import (
    Component,
    ComponentAlreadyRegisteredError,
    ComponentNotRegisteredError,
    Grelmicro,
    LifecycleOrderError,
    MultipleActiveAppsError,
    NoActiveAppError,
)
from grelmicro.coordination import Coordination
from grelmicro.errors import OutOfContextError
from grelmicro.providers import Provider

_BOOM = "boom"
_RAISED = "raised"


class _RecordingComponent:
    """A Component implementation that records its enter/exit lifecycle."""

    kind: ClassVar[str] = "rec"

    def __init__(
        self, *, name: str = "default", log: list[str] | None = None
    ) -> None:
        self.name = name
        self.log: list[str] = log if log is not None else []
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> Self:
        self.entered += 1
        self.log.append(f"enter:{self.name}")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        self.exited += 1
        self.log.append(f"exit:{self.name}")
        return None


class _OtherComponent(_RecordingComponent):
    """Different `kind` so it can coexist with `_RecordingComponent` on `micro`."""

    kind: ClassVar[str] = "oth"


class _RaisingComponent(_RecordingComponent):
    """Raises on `__aenter__` so we can test partial-startup cleanup."""

    async def __aenter__(self) -> Self:
        self.log.append(f"enter:{self.name}")
        raise RuntimeError(_BOOM)


class _RecordingLockAdapter:
    """A `LockBackend` that borrows a provider it does not own.

    Only the lifecycle hooks run in these tests; the lock methods are
    stubs present to satisfy the `LockBackend` protocol.
    """

    def __init__(self, provider: _RecordingProvider) -> None:
        self._provider = provider
        self._owns_provider = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def acquire(
        self, *, name: str, token: str, duration: float
    ) -> int | None:
        raise NotImplementedError

    async def release(self, *, name: str, token: str) -> bool:
        raise NotImplementedError

    async def locked(self, *, name: str) -> bool:
        raise NotImplementedError

    async def owned(self, *, name: str, token: str) -> bool:
        raise NotImplementedError


class _RecordingElectionAdapter:
    """A `LeaderElectionBackend` that borrows a provider it does not own.

    Only the lifecycle hooks run in these tests; the election methods are
    stubs present to satisfy the `LeaderElectionBackend` protocol.
    """

    def __init__(self, provider: _RecordingProvider) -> None:
        self._provider = provider
        self._owns_provider = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def acquire_or_renew(
        self,
        *,
        name: str,
        token: str,
        duration: float,
        metadata: Mapping[str, str] | None = None,
    ) -> LeaderRecord:
        raise NotImplementedError

    async def release(self, *, name: str, token: str) -> bool:
        raise NotImplementedError

    async def get(self, *, name: str) -> LeaderRecord | None:
        raise NotImplementedError


class _RecordingProvider(Provider):
    """A Provider that records its enter/exit lifecycle for discovery tests.

    `Coordination(provider)` asks for both a lock backend and an election
    backend, so this Provider ships both. The two adapters borrow the same
    Provider instance, so discovery walks both backends and still adopts the
    Provider exactly once.
    """

    short_name: ClassVar[str] = "rec"

    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    def lock(self, **kwargs: object) -> _RecordingLockAdapter:  # noqa: ARG002
        return _RecordingLockAdapter(self)

    def leader_election(
        self,
        **kwargs: object,  # noqa: ARG002
    ) -> _RecordingElectionAdapter:
        return _RecordingElectionAdapter(self)

    async def __aenter__(self) -> Self:
        self.entered += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.exited += 1


# --- Component protocol ---


def test_component_protocol_is_runtime_checkable() -> None:
    """A class with kind/name/__aenter__/__aexit__ satisfies the Component protocol."""
    assert isinstance(_RecordingComponent(), Component)


# --- .components introspection ---


def test_components_returns_registered_in_order() -> None:
    """`.components` yields Component instances in registration order."""
    micro = Grelmicro()
    rec = _RecordingComponent(name="default")
    oth = _OtherComponent(name="default")
    micro.use(rec)
    micro.use(oth)
    assert micro.components == (rec, oth)


def test_components_excludes_plain_context_managers() -> None:
    """Plain async context managers are not exposed via `.components`."""

    class _PlainCM:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> bool | None:
            return None

    micro = Grelmicro()
    rec = _RecordingComponent()
    micro.use(rec)
    micro.use(_PlainCM())
    assert micro.components == (rec,)


# --- .use() registration ---


def test_use_attaches_component_on_kind_attr() -> None:
    """`.use()` exposes the component as `micro.<kind>`."""
    micro = Grelmicro()
    pattern = _RecordingComponent()
    micro.use(pattern)
    assert micro.rec is pattern


def test_use_returns_none() -> None:
    """`.use()` returns None (side-effect registration, mirrors FastAPI's include_router)."""
    micro = Grelmicro()
    pattern = _RecordingComponent()
    assert micro.use(pattern) is None


def test_use_same_instance_is_noop() -> None:
    """Re-registering the exact same instance under the same name is a no-op."""
    micro = Grelmicro()
    pattern = _RecordingComponent()
    micro.use(pattern)
    micro.use(pattern)


def test_use_different_instance_same_key_raises() -> None:
    """Two different instances under the same `(kind, name)` raises."""
    micro = Grelmicro()
    micro.use(_RecordingComponent())
    with pytest.raises(ComponentAlreadyRegisteredError):
        micro.use(_RecordingComponent())


def test_use_same_kind_different_name_coexists() -> None:
    """Multiple components of the same kind under different names coexist."""
    micro = Grelmicro()
    primary = _RecordingComponent(name="primary")
    analytics = _RecordingComponent(name="analytics")
    micro.use(primary)
    micro.use(analytics)
    assert micro.get("rec", "primary") is primary
    assert micro.get("rec", "analytics") is analytics


# --- uses= constructor ---


def test_uses_kwarg_registers_components_in_order() -> None:
    """`Grelmicro(uses=[...])` is equivalent to repeated `.use(...)` calls."""
    a = _RecordingComponent(name="a")
    b = _OtherComponent(name="b")
    micro = Grelmicro(uses=[a, b])
    assert micro.get("rec", "a") is a
    assert micro.get("oth", "b") is b


def test_uses_kwarg_accepts_none() -> None:
    """`uses=None` (the default) constructs an empty container."""
    micro = Grelmicro()
    with pytest.raises(
        ComponentNotRegisteredError, match="no components are registered"
    ):
        micro.get("rec")


def test_get_missing_component_error_lists_registered_keys() -> None:
    """The error names every `(kind, name)` pair that is registered."""
    a = _RecordingComponent(name="a")
    b = _OtherComponent(name="b")
    micro = Grelmicro(uses=[a, b])
    with pytest.raises(ComponentNotRegisteredError) as exc:
        micro.get("rec", "missing")
    msg = str(exc.value)
    assert "('rec', 'a')" in msg
    assert "('oth', 'b')" in msg


# --- Lifespan: enter, LIFO teardown ---


async def test_lifespan_enters_and_exits_in_lifo_order() -> None:
    """Components enter in registration order, exit in reverse."""
    log: list[str] = []
    a = _RecordingComponent(name="a", log=log)
    b = _RecordingComponent(name="b", log=log)
    c = _RecordingComponent(name="c", log=log)
    micro = Grelmicro(uses=[a, b, c])
    async with micro:
        assert log == ["enter:a", "enter:b", "enter:c"]
    assert log == [
        "enter:a",
        "enter:b",
        "enter:c",
        "exit:c",
        "exit:b",
        "exit:a",
    ]


async def test_lifespan_partial_startup_failure_unwinds_already_entered() -> (
    None
):
    """A failure in component N rolls back components 0..N-1 in LIFO order."""
    log: list[str] = []
    good = _RecordingComponent(name="good", log=log)
    bad = _RaisingComponent(name="bad", log=log)
    micro = Grelmicro(uses=[good, bad])
    with pytest.raises(RuntimeError, match=_BOOM):
        async with micro:
            pass
    assert log == ["enter:good", "enter:bad", "exit:good"]


async def test_lifespan_can_be_reentered_after_clean_exit() -> None:
    """A `Grelmicro` instance can be opened again after closing cleanly."""
    micro = Grelmicro(uses=[_RecordingComponent()])
    async with micro:
        pass
    async with micro:
        pass


# --- ContextVar ambient lookup ---


async def test_current_micro_returns_active_app_inside_block() -> None:
    """`Grelmicro.current()` returns the active `Grelmicro` inside `async with`."""
    micro = Grelmicro()
    async with micro:
        assert Grelmicro.current() is micro


async def test_current_micro_raises_outside_block() -> None:
    """`Grelmicro.current()` raises `NoActiveAppError` outside any active app."""
    with pytest.raises(NoActiveAppError):
        Grelmicro.current()


async def test_current_micro_is_per_task() -> None:
    """Two concurrent tasks each see their own Grelmicro."""
    # Neither app configures process-global state, so they overlap freely.
    micro_a = Grelmicro()
    micro_b = Grelmicro()
    seen: dict[str, Grelmicro] = {}

    async def run(label: str, micro: Grelmicro) -> None:
        async with micro:
            await asyncio.sleep(0)  # let the other task interleave
            seen[label] = Grelmicro.current()

    await asyncio.gather(run("a", micro_a), run("b", micro_b))
    assert seen["a"] is micro_a
    assert seen["b"] is micro_b


# --- override() ---


async def test_override_swaps_component_for_block() -> None:
    """`micro.override(...)` replaces a component for the duration of the block."""
    log: list[str] = []
    real = _RecordingComponent(name="default", log=log)
    mock = _RecordingComponent(name="default", log=[])
    mock.log = log  # share log
    micro = Grelmicro(uses=[real])
    async with micro:
        async with micro.override(mock):
            assert micro.rec is mock
        assert micro.rec is real
    # mock entered and exited inside the override block
    assert "enter:default" in log


async def test_double_aenter_raises() -> None:
    """Re-entering an already-open `Grelmicro` raises `OutOfContextError`."""
    micro = Grelmicro()
    async with micro:
        with pytest.raises(OutOfContextError):
            await micro.__aenter__()


async def test_aexit_without_aenter_raises() -> None:
    """Calling `__aexit__` on a never-entered `Grelmicro` raises `OutOfContextError`."""
    micro = Grelmicro()
    with pytest.raises(OutOfContextError):
        await micro.__aexit__(None, None, None)


class _RecordingContext:
    """Bare async context manager (no kind/name) for plain-CM `use()` tests."""

    def __init__(self, *, log: list[str], label: str) -> None:
        self.log = log
        self.label = label
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> Self:
        self.entered += 1
        self.log.append(f"enter:{self.label}")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        self.exited += 1
        self.log.append(f"exit:{self.label}")
        return None


def test_use_plain_context_manager_returns_none() -> None:
    """`.use()` on a plain async context manager returns None; caller keeps the reference."""
    micro = Grelmicro()
    item = _RecordingContext(log=[], label="x")
    assert micro.use(item) is None


async def test_use_lifecycles_plain_context_manager_with_app() -> None:
    """`async with micro:` enters and exits plain async context managers."""
    log: list[str] = []
    item = _RecordingContext(log=log, label="tasks")
    micro = Grelmicro(uses=[item])
    async with micro:
        assert item.entered == 1
        assert item.exited == 0
    assert item.exited == 1


async def test_uses_kwarg_accepts_components_and_plain_managers_in_one_list() -> (
    None
):
    """`uses=[Component(), plain_manager]` mixes both kinds in registration order."""
    log: list[str] = []
    mod = _RecordingComponent(name="default", log=log)
    inc = _RecordingContext(log=log, label="entry_point")
    micro = Grelmicro(uses=[mod, inc])
    async with micro:
        pass
    assert log == [
        "enter:default",
        "enter:entry_point",
        "exit:entry_point",
        "exit:default",
    ]


async def test_use_partial_startup_failure_unwinds() -> None:
    """A failure in one item rolls back already-entered items in LIFO order."""
    log: list[str] = []
    good = _RecordingContext(log=log, label="good")

    class _BadContext:
        async def __aenter__(self) -> Self:
            log.append("enter:bad")
            raise RuntimeError(_BOOM)

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> bool | None:
            return None

    micro = Grelmicro(uses=[good, _BadContext()])
    with pytest.raises(RuntimeError, match=_BOOM):
        async with micro:
            pass
    assert log == ["enter:good", "enter:bad", "exit:good"]


def test_runtime_type_hints_resolve_without_loading_submodules() -> None:
    """`typing.get_type_hints(Grelmicro)` does not raise even with TYPE_CHECKING imports.

    The runtime fallback `Cache = Any` / `Coordination = Any` keeps `coordination` / `cache`
    property annotations resolvable for docs tooling and frameworks that
    introspect annotations.
    """
    from typing import get_type_hints  # noqa: PLC0415

    hints = get_type_hints(Grelmicro)
    # Property names show up via class-level resolution under
    # `from __future__ import annotations`; the call must not raise.
    assert isinstance(hints, dict)


async def test_component_aenter_can_resolve_current_micro() -> None:
    """Components consulting `Grelmicro.current()` from `__aenter__` see the active app."""
    seen: list[Grelmicro] = []

    class _CurrentLookupOnEnter:
        kind: ClassVar[str] = "rec"

        def __init__(self) -> None:
            self.name = "default"

        async def __aenter__(self) -> Self:
            seen.append(Grelmicro.current())
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> bool | None:
            return None

    micro = Grelmicro(uses=[_CurrentLookupOnEnter()])
    async with micro:
        pass
    assert seen == [micro]


async def test_plain_manager_aenter_can_resolve_current_micro() -> None:
    """Plain async context managers see the active app from `__aenter__`."""
    seen: list[Grelmicro] = []

    class _CurrentLookupInclude:
        async def __aenter__(self) -> Self:
            seen.append(Grelmicro.current())
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> bool | None:
            return None

    item = _CurrentLookupInclude()
    micro = Grelmicro(uses=[item])
    async with micro:
        pass
    assert seen == [micro]


async def test_component_aexit_can_resolve_current_micro() -> None:
    """Components consulting `Grelmicro.current()` from `__aexit__` see the active app."""
    seen: list[Grelmicro] = []

    class _CurrentLookupComponent:
        kind: ClassVar[str] = "rec"

        def __init__(self) -> None:
            self.name = "default"

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> bool | None:
            seen.append(Grelmicro.current())
            return None

    micro = Grelmicro(uses=[_CurrentLookupComponent()])
    async with micro:
        pass
    assert seen == [micro]


async def test_override_outside_active_context_raises() -> None:
    """`override(...)` outside an active `async with micro:` raises `OutOfContextError`."""
    micro = Grelmicro()
    mock = _RecordingComponent(name="default")
    with pytest.raises(OutOfContextError):
        async with micro.override(mock):
            pass


async def test_unknown_kind_attribute_raises_attribute_error() -> None:
    """`micro.<unknown_kind>` raises a regular `AttributeError`."""
    micro = Grelmicro()
    with pytest.raises(AttributeError, match="no component of kind 'nope'"):
        _ = micro.nope


async def test_override_restores_on_exception() -> None:
    """`override(...)` restores prior registrations even when the block raises."""
    real = _RecordingComponent(name="default")
    mock = _RecordingComponent(name="default")
    micro = Grelmicro(uses=[real])
    async with micro:
        with pytest.raises(RuntimeError, match=_RAISED):
            async with micro.override(mock):
                raise RuntimeError(_RAISED)
        assert micro.rec is real


async def test_provider_public_export() -> None:
    """`grelmicro.providers.Provider` is importable as the base class."""
    from grelmicro.providers import Provider  # noqa: PLC0415
    from grelmicro.providers.redis import RedisProvider  # noqa: PLC0415

    assert issubclass(RedisProvider, Provider)


async def test_provider_base_lock_raises_not_implemented() -> None:
    """`Provider.lock()` raises when a subclass does not override it."""
    from grelmicro.providers import Provider  # noqa: PLC0415

    class _BareProvider(Provider):
        short_name = "bare"

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            return None

    bare = _BareProvider()
    with pytest.raises(NotImplementedError, match="no lock adapter"):
        bare.lock()


async def test_provider_base_leader_election_raises_not_implemented() -> None:
    """`Provider.leader_election()` raises when a subclass does not override it."""
    from grelmicro.providers import Provider  # noqa: PLC0415

    class _BareProvider(Provider):
        short_name = "bare"

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            return None

    bare = _BareProvider()
    with pytest.raises(NotImplementedError, match="no leader election adapter"):
        bare.leader_election()


async def test_discovers_provider_not_in_uses(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """`Coordination(provider)` without the provider in `uses=` adopts and lifecycles it."""
    provider = _RecordingProvider()
    micro = Grelmicro(uses=[Coordination(provider)])
    async with micro:
        pass

    assert provider.entered == 1
    assert provider.exited == 1
    assert not any(
        issubclass(w.category, UserWarning) and "listed" in str(w.message)
        for w in recwarn
    )


async def test_discovers_shared_provider_only_once() -> None:
    """A provider held by several components is lifecycled exactly once."""
    provider = _RecordingProvider()
    micro = Grelmicro(
        uses=[
            Coordination(provider, name="a"),
            Coordination(provider, name="b"),
        ]
    )
    async with micro:
        pass

    assert provider.entered == 1
    assert provider.exited == 1


async def test_discovers_both_providers_of_a_coordination() -> None:
    """A `Coordination` holding two providers adopts both, each lifecycled once."""
    lock_provider = _RecordingProvider()
    election_provider = _RecordingProvider()
    micro = Grelmicro(
        uses=[Coordination(lock=lock_provider, election=election_provider)]
    )
    async with micro:
        pass

    assert lock_provider.entered == 1
    assert lock_provider.exited == 1
    assert election_provider.entered == 1
    assert election_provider.exited == 1


async def test_explicit_provider_takes_precedence_over_discovery() -> None:
    """A provider listed in `uses=` is lifecycled once, not adopted again."""
    provider = _RecordingProvider()
    micro = Grelmicro(uses=[provider, Coordination(provider)])
    async with micro:
        pass

    assert provider.entered == 1
    assert provider.exited == 1


async def test_warns_when_provider_listed_after_component(
    recwarn: pytest.WarningsRecorder,
) -> None:
    """A provider listed after the Component triggers the ordering warning."""
    from grelmicro.coordination import Coordination  # noqa: PLC0415
    from grelmicro.providers.redis import RedisProvider  # noqa: PLC0415

    redis = RedisProvider("redis://localhost:6379/0")
    micro = Grelmicro(uses=[Coordination(redis), redis])
    async with micro:
        pass

    assert any(
        issubclass(w.category, UserWarning) and "listed after" in str(w.message)
        for w in recwarn
    )


async def test_strict_adopts_provider_missing_from_uses() -> None:
    """`strict=True` adopts a provider absent from `uses=` without erroring."""
    provider = _RecordingProvider()
    micro = Grelmicro(uses=[Coordination(provider)], strict=True)
    async with micro:
        pass

    assert provider.entered == 1
    assert provider.exited == 1


async def test_strict_raises_when_provider_listed_after_component() -> None:
    """`strict=True` turns the ordering warning into an error."""
    from grelmicro.coordination import Coordination  # noqa: PLC0415
    from grelmicro.providers.redis import RedisProvider  # noqa: PLC0415

    redis = RedisProvider("redis://localhost:6379/0")
    micro = Grelmicro(uses=[Coordination(redis), redis], strict=True)
    with pytest.raises(LifecycleOrderError, match="listed after"):
        async with micro:
            pass


async def test_strict_accepts_well_ordered_app() -> None:
    """`strict=True` is a no-op when provider/component order is correct."""
    from grelmicro.coordination import Coordination  # noqa: PLC0415
    from grelmicro.providers.redis import RedisProvider  # noqa: PLC0415

    redis = RedisProvider("redis://localhost:6379/0")
    micro = Grelmicro(uses=[redis, Coordination(redis)], strict=True)
    async with micro:
        pass


# --- single-active-app guard (#266) ---


class _GlobalComponent(_RecordingComponent):
    """Stands in for a process-global component such as Log or Trace."""

    kind: ClassVar[str] = "log"


async def test_second_global_app_is_blocked() -> None:
    """A second app owning global state, while one is active, is blocked."""
    async with Grelmicro(uses=[_GlobalComponent()]):
        with pytest.raises(MultipleActiveAppsError):
            async with Grelmicro(uses=[_GlobalComponent()]):
                pass


async def test_apps_without_global_state_overlap_freely() -> None:
    """Apps that do not configure global state can overlap."""
    async with (
        Grelmicro(uses=[_RecordingComponent()]),
        Grelmicro(uses=[_OtherComponent()]),
    ):
        pass  # no error: neither owns process-global state


async def test_global_app_overlaps_plain_app() -> None:
    """A global-state app overlaps a plain app (only one owns global state)."""
    async with (
        Grelmicro(uses=[_GlobalComponent()]),
        Grelmicro(uses=[_RecordingComponent()]),
    ):
        pass


async def test_sequential_global_apps_are_allowed() -> None:
    """Two global-state apps opened one after another are fine."""
    async with Grelmicro(uses=[_GlobalComponent()]):
        pass
    async with Grelmicro(uses=[_GlobalComponent()]):  # first already exited
        pass


async def test_allow_multiple_opts_out_of_guard() -> None:
    """allow_multiple=True lets a second global-state app overlap the first."""
    async with (
        Grelmicro(uses=[_GlobalComponent()]),
        Grelmicro(uses=[_GlobalComponent()], allow_multiple=True),
    ):
        pass


async def test_guard_clears_after_failed_startup() -> None:
    """A partial-startup failure removes the app from the active set."""
    log: list[str] = []
    with pytest.raises(RuntimeError, match=_BOOM):
        async with Grelmicro(uses=[_RaisingComponent(log=log)]):
            pass
    # The failed app released its slot, so a fresh global-state app can open.
    async with (
        Grelmicro(uses=[_GlobalComponent()]),
        Grelmicro(uses=[_RecordingComponent()]),
    ):
        pass


# --- bare-class ergonomics (#263) ---


async def test_uses_accepts_bare_component_class() -> None:
    """A zero-arg Component class in uses= is instantiated for you."""

    class _BareComponent(_RecordingComponent):
        kind: ClassVar[str] = "bare"

    async with Grelmicro(uses=[_BareComponent]) as micro:
        assert isinstance(micro.get("bare", "default"), _BareComponent)


async def test_use_bare_class_needing_args_raises_clear_error() -> None:
    """A class needing constructor arguments gives an actionable error."""

    class _NeedsArgs:
        def __init__(self, required: int) -> None:
            self.required = required

    with pytest.raises(TypeError, match="needs constructor arguments"):
        Grelmicro(uses=[_NeedsArgs])


async def test_use_bare_plain_context_manager_class() -> None:
    """A zero-arg plain async context manager class is instantiated and lifecycled."""
    entered: list[str] = []

    class _PlainCM:
        async def __aenter__(self) -> Self:
            entered.append("in")
            return self

        async def __aexit__(self, *exc: object) -> None:
            entered.append("out")

    async with Grelmicro(uses=[_PlainCM]):
        assert entered == ["in"]
    assert entered == ["in", "out"]
