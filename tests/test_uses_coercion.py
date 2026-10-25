"""Tests for the forgiving `uses=[...]` / `use(...)` coercion rules.

Three shorthands are exercised here: a bare Component class, a bare Adapter
(instance or class), and a bare Provider that auto-registers the components it
serves. The explicit forms stay legal and always win over the implicit ones.
"""

from __future__ import annotations

import inspect
import sys
from contextlib import suppress
from typing import TYPE_CHECKING, ClassVar, Self

import pytest
from fastapi import FastAPI
from hypothesis import given
from hypothesis import strategies as st

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

from grelmicro import (
    AmbiguousBackendError,
    AmbiguousProviderError,
    BackendScopeError,
    ComponentAlreadyRegisteredError,
    Grelmicro,
    GrelmicroError,
    Usable,
)
from grelmicro._app import _maybe_wrap_first_party_backend
from grelmicro._backend_kinds import (
    _KINDS,
    _could_match,
    _KindRef,
    backend_kinds_of,
    load,
)
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.coordination import Coordination
from grelmicro.coordination._component import COORDINATION_BACKENDS
from grelmicro.coordination.memory import (
    MemoryLeaderElectionAdapter,
    MemoryLockAdapter,
    MemoryReadWriteLockAdapter,
    MemoryScheduleAdapter,
)
from grelmicro.providers import Provider
from grelmicro.providers.valkey import ValkeyProvider
from grelmicro.resilience._components import (
    CircuitBreakerComponent,
    RateLimiterComponent,
)
from grelmicro.resilience.circuitbreaker.memory import (
    MemoryCircuitBreakerAdapter,
)
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter


class _HealthLikeComponent:
    """A Component of a kind no Provider serves, as `HealthChecks` is."""

    kind: ClassVar[str] = "health"
    name: str = "default"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _MemoryProvider(Provider):
    """A Provider serving every kind from in-memory adapters.

    Each factory hands back a fresh memory adapter, so a bare
    `Grelmicro(uses=[_MemoryProvider()])` should auto-register one default
    Component per kind: coordination, cache, ratelimiter, circuitbreaker.
    """

    short_name: ClassVar[str] = "memory"

    def lock_backend(self, **kwargs: object) -> MemoryLockAdapter:  # noqa: ARG002
        return MemoryLockAdapter()

    def leaderelection_backend(
        self,
        **kwargs: object,  # noqa: ARG002
    ) -> MemoryLeaderElectionAdapter:
        return MemoryLeaderElectionAdapter()

    def cache_backend(self, **kwargs: object) -> MemoryCacheAdapter:  # noqa: ARG002
        return MemoryCacheAdapter()

    def ratelimiter_backend(
        self,
        **kwargs: object,  # noqa: ARG002
    ) -> MemoryRateLimiterAdapter:
        return MemoryRateLimiterAdapter()

    def circuitbreaker_backend(
        self,
        **kwargs: object,  # noqa: ARG002
    ) -> MemoryCircuitBreakerAdapter:
        return MemoryCircuitBreakerAdapter()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class _CacheOnlyProvider(Provider):
    """A Provider serving only the cache kind."""

    short_name: ClassVar[str] = "cacheonly"

    def cache_backend(self, **kwargs: object) -> MemoryCacheAdapter:  # noqa: ARG002
        return MemoryCacheAdapter()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class _RateLimiterOnlyProvider(Provider):
    """A Provider serving only the rate limiter kind, skipping cache."""

    short_name: ClassVar[str] = "ratelimiteronly"

    def ratelimiter_backend(
        self,
        **kwargs: object,  # noqa: ARG002
    ) -> MemoryRateLimiterAdapter:
        return MemoryRateLimiterAdapter()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


# --- Bare Component class ---


def test_bare_component_class_constructs_with_defaults() -> None:
    """A Component class with all-defaulting args is instantiated for you."""

    class _DefaultComponent:
        kind: ClassVar[str] = "rec"

        def __init__(self, *, name: str = "default") -> None:
            self.name = name

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    micro = Grelmicro(uses=[_DefaultComponent])

    assert micro.get("rec").name == "default"


# --- Bare Adapter instance ---


def test_bare_adapter_instance_wraps_in_component() -> None:
    """A bare adapter instance is wrapped in its matching Component."""
    micro = Grelmicro(uses=[MemoryLockAdapter()])

    coordination = micro.get("coordination")
    assert isinstance(coordination, Coordination)
    assert coordination.lock_backend is not None


@pytest.mark.parametrize(
    ("adapter", "attribute"),
    [
        (MemoryLockAdapter, "lock_backend"),
        (MemoryReadWriteLockAdapter, "readwritelock_backend"),
        (MemoryLeaderElectionAdapter, "leaderelection_backend"),
        (MemoryScheduleAdapter, "schedule_backend"),
    ],
)
def test_every_coordination_backend_wraps_into_its_slot(
    adapter: type, attribute: str
) -> None:
    """Each coordination backend lands on the slot it belongs to.

    A backend that wraps into no Component would be lifecycled and never
    resolved, so the app would start and the pattern would fail on first use.
    """
    micro = Grelmicro(uses=[adapter()])

    coordination = micro.get("coordination")
    assert isinstance(coordination, Coordination)
    assert getattr(coordination, attribute) is not None


# --- Bare Adapter class ---


def test_bare_adapter_class_constructs_then_wraps() -> None:
    """A bare adapter class is instantiated and wrapped in its Component."""
    micro = Grelmicro(uses=[MemoryCircuitBreakerAdapter])

    breakers = micro.get("circuitbreaker")
    assert isinstance(breakers, CircuitBreakerComponent)


def test_bare_adapter_class_matches_explicit_form() -> None:
    """`uses=[MemoryCacheAdapter]` matches `uses=[Cache(MemoryCacheAdapter())]`."""
    bare = Grelmicro(uses=[MemoryCacheAdapter])
    explicit = Grelmicro(uses=[Cache(MemoryCacheAdapter())])

    assert isinstance(bare.get("cache"), Cache)
    assert isinstance(explicit.get("cache"), Cache)


# --- Provider auto-registration ---


def test_provider_registers_one_component_per_served_kind() -> None:
    """A lone Provider auto-registers a default Component per kind it serves."""
    micro = Grelmicro(uses=[_MemoryProvider()])

    assert isinstance(micro.get("coordination"), Coordination)
    assert isinstance(micro.get("cache"), Cache)
    assert isinstance(micro.get("ratelimiter"), RateLimiterComponent)
    assert isinstance(micro.get("circuitbreaker"), CircuitBreakerComponent)


def test_provider_skips_kinds_it_does_not_serve() -> None:
    """A Provider serving only cache registers only the cache Component."""
    micro = Grelmicro(uses=[_CacheOnlyProvider()])

    assert isinstance(micro.get("cache"), Cache)
    kinds = {component.kind for component in micro.components}
    assert kinds == {"cache"}


def test_provider_skips_cache_when_only_ratelimiter_served() -> None:
    """A Provider serving only the rate limiter registers no cache Component."""
    micro = Grelmicro(uses=[_RateLimiterOnlyProvider()])

    assert isinstance(micro.get("ratelimiter"), RateLimiterComponent)
    kinds = {component.kind for component in micro.components}
    assert kinds == {"ratelimiter"}


def test_valkey_provider_auto_registers_like_redis() -> None:
    """A bare `ValkeyProvider` auto-registers every kind it inherits from Redis."""
    micro = Grelmicro(uses=[ValkeyProvider("redis://localhost:6379/0")])

    assert isinstance(micro.get("coordination"), Coordination)
    assert isinstance(micro.get("cache"), Cache)
    assert isinstance(micro.get("ratelimiter"), RateLimiterComponent)
    assert isinstance(micro.get("circuitbreaker"), CircuitBreakerComponent)


async def test_provider_auto_registered_components_lifecycle() -> None:
    """The Provider is lifecycled once and its auto-registered Components open."""
    provider = _MemoryProvider()
    micro = Grelmicro(uses=[provider])

    async with micro:
        assert micro.get("cache") is not None


# --- Explicit wins ---


def test_explicit_component_wins_for_its_own_kind() -> None:
    """An explicit Component keeps its kind, the Provider fills the rest."""
    backend = MemoryCacheAdapter()
    micro = Grelmicro(uses=[_MemoryProvider(), Cache(backend)])

    assert micro.get("cache").backend is backend
    kinds = {component.kind for component in micro.components}
    assert kinds == {"cache", "coordination", "ratelimiter", "circuitbreaker"}


def test_unrelated_component_does_not_suppress_defaults() -> None:
    """A Component of a kind no Provider serves leaves the defaults alone."""
    micro = Grelmicro(uses=[_MemoryProvider(), _HealthLikeComponent()])

    kinds = {component.kind for component in micro.components}
    assert kinds == {
        "cache",
        "coordination",
        "ratelimiter",
        "circuitbreaker",
        "health",
    }


async def test_explicit_provider_listed_with_component_lifecycled_once() -> (
    None
):
    """A Provider beside an explicit Component still fills the other kinds."""
    provider = _MemoryProvider()
    micro = Grelmicro(uses=[provider, Coordination(lock=MemoryLockAdapter())])

    async with micro:
        pass

    kinds = {component.kind for component in micro.components}
    assert kinds == {"coordination", "cache", "ratelimiter", "circuitbreaker"}


# --- Two-provider ambiguity ---


def test_two_providers_no_components_raises() -> None:
    """Two bare Providers with no Components make the default ambiguous."""
    micro = Grelmicro(uses=[_MemoryProvider(), _CacheOnlyProvider()])

    with pytest.raises(AmbiguousProviderError, match="multiple providers"):
        micro.get("cache")


async def test_two_providers_no_components_raise_on_open() -> None:
    """The ambiguity is raised when the app opens, if nothing read it first."""
    micro = Grelmicro(uses=[_MemoryProvider(), _CacheOnlyProvider()])

    with pytest.raises(AmbiguousProviderError, match="multiple providers"):
        async with micro:
            pass


def test_two_providers_with_explicit_components_do_not_raise() -> None:
    """Explicit Components disable auto-registration, so two providers are fine."""
    micro = Grelmicro(
        uses=[
            _MemoryProvider(),
            _CacheOnlyProvider(),
            Cache(MemoryCacheAdapter()),
        ]
    )

    assert isinstance(micro.get("cache"), Cache)


# --- micro.use parity ---


def test_use_bare_adapter_class_parity() -> None:
    """`micro.use(MemoryCacheAdapter)` matches the `uses=` shorthand."""
    micro = Grelmicro()
    micro.use(MemoryCacheAdapter)

    assert isinstance(micro.get("cache"), Cache)


def test_use_lone_provider_auto_registers() -> None:
    """`micro.use(provider)` on an empty app auto-registers served kinds."""
    micro = Grelmicro()
    micro.use(_MemoryProvider())

    assert isinstance(micro.get("cache"), Cache)
    assert isinstance(micro.get("coordination"), Coordination)


def test_use_two_providers_are_ambiguous_as_in_uses() -> None:
    """`use(p1)` then `use(p2)` raises as `uses=[p1, p2]` does."""
    micro = Grelmicro()
    micro.use(_MemoryProvider())
    micro.use(_CacheOnlyProvider())

    with pytest.raises(AmbiguousProviderError, match="multiple providers"):
        micro.get("cache")


def test_use_provider_after_component_fills_the_other_kinds() -> None:
    """`use(component)` then `use(provider)` matches the `uses=` list form."""
    backend = MemoryCacheAdapter()
    micro = Grelmicro()
    micro.use(Cache(backend))
    micro.use(_MemoryProvider())

    assert micro.get("cache").backend is backend
    kinds = {component.kind for component in micro.components}
    assert kinds == {"cache", "coordination", "ratelimiter", "circuitbreaker"}


def _signature(
    micro: Grelmicro, pool: list[Usable | None]
) -> tuple[list[tuple[str, str]], list[object]]:
    """Return the registered keys and the lifecycle order of `micro`.

    An item from `pool` reads as its index. Any other item, a Provider
    default or a wrapped adapter, reads as its type, kind and name, plus the
    index of the backend it wraps when that backend is in `pool`.
    """
    index = {id(item): position for position, item in enumerate(pool)}
    keys = [(component.kind, component.name) for component in micro.components]
    items: list[object] = [
        index[id(item)]
        if id(item) in index
        else (
            type(item).__name__,
            getattr(item, "kind", None),
            getattr(item, "name", None),
            index.get(id(getattr(item, "backend", None))),
        )
        for item in micro._items
    ]
    return keys, items


def _assert_use_matches_uses(pool: list[Usable | None]) -> None:
    """Assert that `use()` one by one registers what `uses=` registers."""
    listed = Grelmicro(uses=pool)
    used = Grelmicro()
    for item in pool:
        if item is not None:
            used.use(item)

    assert _signature(used, pool) == _signature(listed, pool)


def test_use_component_after_provider_matches_uses() -> None:
    """`use(provider)` then `use(component)` matches the `uses=` list form."""
    provider = _MemoryProvider()
    explicit = Coordination(provider)
    micro = Grelmicro()
    micro.use(provider)
    micro.use(explicit)

    assert micro.get("coordination") is explicit
    _assert_use_matches_uses([provider, explicit])


def test_use_after_uses_provider_lets_the_component_win() -> None:
    """A Component passed to `use()` after `uses=[provider]` wins its kind."""
    provider = _MemoryProvider()
    explicit = Coordination(provider)
    micro = Grelmicro(uses=[provider])
    micro.use(explicit)

    assert micro.get("coordination") is explicit
    listed = Grelmicro(uses=[provider, explicit])
    assert _signature(micro, [provider, explicit]) == _signature(
        listed, [provider, explicit]
    )


def test_use_two_providers_and_a_component_match_uses() -> None:
    """With a Component listed, two Providers fill nothing, in both forms."""
    first = _MemoryProvider()
    second = _MemoryProvider()
    explicit = Cache(second)
    micro = Grelmicro()
    micro.use(first)
    micro.use(second)
    micro.use(explicit)

    assert [component.kind for component in micro.components] == ["cache"]
    _assert_use_matches_uses([first, second, explicit])


def test_use_named_component_after_provider_claims_its_kind() -> None:
    """A Component under another name claims its kind, as in `uses=`."""
    provider = _MemoryProvider()
    explicit = Cache(MemoryCacheAdapter(), name="analytics")
    micro = Grelmicro()
    micro.use(provider)
    micro.use(explicit)

    assert micro.get("cache") is explicit
    _assert_use_matches_uses([provider, explicit])


def test_use_second_explicit_component_of_a_kind_still_raises() -> None:
    """Only a Provider default gives way, two explicit Components still clash."""
    micro = Grelmicro()
    micro.use(_MemoryProvider())
    micro.use(Cache(MemoryCacheAdapter()))

    with pytest.raises(ComponentAlreadyRegisteredError, match="already"):
        micro.use(Cache(MemoryCacheAdapter()))


async def test_use_component_after_provider_opens_only_the_explicit() -> None:
    """The Provider default for a claimed kind is never registered or opened."""
    provider = _MemoryProvider()
    explicit = Cache(MemoryCacheAdapter())
    micro = Grelmicro()
    micro.use(provider)
    micro.use(explicit)

    async with micro:
        assert micro.cache is explicit
        caches = [
            component
            for component in micro.components
            if component.kind == "cache"
        ]
        assert caches == [explicit]


def test_use_same_provider_twice_is_not_ambiguous() -> None:
    """One Provider passed twice is one Provider, in both forms."""
    provider = _MemoryProvider()
    micro = Grelmicro()
    micro.use(provider)
    micro.use(provider)

    assert isinstance(micro.cache, Cache)
    _assert_use_matches_uses([provider, provider])


def _check_backends(micro: Grelmicro) -> None:
    """Run the backend check, whose memory backends fall short on purpose."""
    with suppress(BackendScopeError):
        micro.check_backends()


@pytest.mark.parametrize(
    ("read", "reason"),
    [
        pytest.param(
            lambda micro: micro.cache, "micro.cache was read", id="micro.cache"
        ),
        pytest.param(
            lambda micro: micro.coordination,
            "micro.coordination was read",
            id="micro.coordination",
        ),
        pytest.param(
            lambda micro: micro.get("cache"),
            "micro.get('cache') was called",
            id="get",
        ),
        pytest.param(
            lambda micro: micro.get(Cache),
            "micro.get('cache') was called",
            id="get-class",
        ),
        pytest.param(
            lambda micro: micro.components,
            "micro.components was read",
            id="components",
        ),
        pytest.param(
            lambda micro: micro.providers,
            "micro.providers was read",
            id="providers",
        ),
        pytest.param(
            lambda micro: micro.describe(),
            "micro.describe() was called",
            id="describe",
        ),
        pytest.param(
            _check_backends,
            "micro.check_backends() was called",
            id="check_backends",
        ),
        pytest.param(
            lambda micro: micro.check_ambient_binding(FastAPI()),
            "micro.check_ambient_binding(app) was called",
            id="check_ambient_binding",
        ),
        pytest.param(
            lambda micro: micro.install(FastAPI()),
            "micro.install(app) was called",
            id="install",
        ),
    ],
)
def test_use_component_after_a_read_names_the_read(
    read: Callable[[Grelmicro], object],
    reason: str,
) -> None:
    """A read registers the Provider defaults, and the refusal names it."""
    micro = Grelmicro()
    micro.use(_MemoryProvider())
    read(micro)

    with pytest.raises(ComponentAlreadyRegisteredError) as caught:
        micro.use(Cache(MemoryCacheAdapter()))
    assert f"registered when {reason}" in str(caught.value)


async def test_use_component_after_open_names_the_open() -> None:
    """An opened app keeps its Provider defaults, open or closed."""
    micro = Grelmicro()
    micro.use(_MemoryProvider())

    async with micro:
        with pytest.raises(
            ComponentAlreadyRegisteredError, match="when the app opened"
        ):
            micro.use(Cache(MemoryCacheAdapter()))
    with pytest.raises(
        ComponentAlreadyRegisteredError, match="when the app opened"
    ):
        micro.use(Cache(MemoryCacheAdapter()))


def test_check_ambient_binding_sees_the_provider_defaults() -> None:
    """The ambient check counts the defaults a pending Provider registers."""
    micro = Grelmicro(uses=[_MemoryProvider()])

    assert micro.check_ambient_binding(FastAPI()) is False


def test_use_named_component_after_a_read_raises() -> None:
    """A Component under another name raises too, once the defaults settle."""
    micro = Grelmicro()
    micro.use(_MemoryProvider())
    default = micro.coordination

    with pytest.raises(
        ComponentAlreadyRegisteredError, match=r"micro\.coordination was read"
    ):
        micro.use(Coordination(lock=MemoryLockAdapter(), name="analytics"))
    assert micro.coordination is default


def _unwrap(micro: Grelmicro) -> object:
    """Unwrap `micro` the way `inspect` and decorators do."""
    return inspect.unwrap(micro)  # ty: ignore[invalid-argument-type]


_UNRELATED_READS: list[Callable[[Grelmicro], object]] = [
    lambda micro: hasattr(micro, "__wrapped__"),
    lambda micro: getattr(micro, "nope", None),
    lambda micro: getattr(micro, "health", None),
    lambda micro: getattr(micro, "log", None),
    lambda micro: getattr(micro, "trace", None),
    _unwrap,
]
"""Reads that name no kind a Provider default can fill."""


@pytest.mark.parametrize("read", _UNRELATED_READS)
def test_unrelated_read_leaves_the_defaults_pending(
    read: Callable[[Grelmicro], object],
) -> None:
    """A read of a name no Provider fills lets a later Component win."""
    provider = _MemoryProvider()
    explicit = Coordination(provider)
    micro = Grelmicro(uses=[provider, _HealthLikeComponent()])
    read(micro)
    micro.use(explicit)

    assert micro.coordination is explicit


@pytest.mark.parametrize("read", _UNRELATED_READS)
def test_unrelated_read_with_two_providers_does_not_raise(
    read: Callable[[Grelmicro], object],
) -> None:
    """A read of a name no Provider fills raises no ambiguity."""
    micro = Grelmicro(uses=[_MemoryProvider(), _CacheOnlyProvider()])

    read(micro)


def test_get_of_an_unrelated_kind_leaves_the_defaults_pending() -> None:
    """`micro.get` of a kind no Provider fills lets a later Component win."""
    provider = _MemoryProvider()
    explicit = Coordination(provider)
    health = _HealthLikeComponent()
    micro = Grelmicro(uses=[provider, health])

    assert micro.get("health") is health
    micro.use(explicit)
    assert micro.coordination is explicit


class _PlainContext:
    """A plain async context manager, lifecycled only."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _pool() -> list[Usable | None]:
    """Build one fresh instance of every item shape the sequences draw from."""
    provider = _MemoryProvider()
    return [
        provider,
        _CacheOnlyProvider(),
        _RateLimiterOnlyProvider(),
        Cache(MemoryCacheAdapter()),
        Cache(MemoryCacheAdapter(), name="analytics"),
        Cache(provider),
        Coordination(lock=MemoryLockAdapter()),
        Coordination(lock=MemoryLockAdapter(), name="analytics"),
        RateLimiterComponent(MemoryRateLimiterAdapter()),
        _HealthLikeComponent(),
        MemoryCacheAdapter(),
        MemoryLockAdapter,
        _PlainContext(),
        None,
    ]


def _outcome(
    build: Callable[[], Grelmicro], pool: list[Usable | None]
) -> object:
    """Return what building and reading an app gives, error or signature."""
    try:
        micro = build()
        return _signature(micro, pool)
    except GrelmicroError as error:
        return type(error)


@given(st.data())
def test_use_one_by_one_matches_uses_for_any_sequence(
    data: st.DataObject,
) -> None:
    """`uses=` then `use()` calls give the keys and lifecycle order of `uses=`.

    The sequence may repeat items. It is split between `uses=` and later
    `use()` calls, with reads of unrelated names in between.
    """
    pool = _pool()
    order = data.draw(st.lists(st.integers(0, len(pool) - 1), max_size=10))
    sequence = [pool[position] for position in order]
    split = data.draw(st.integers(0, len(sequence)))
    reads = data.draw(
        st.lists(
            st.integers(0, len(_UNRELATED_READS) - 1),
            min_size=len(sequence) - split,
            max_size=len(sequence) - split,
        )
    )

    def listed() -> Grelmicro:
        return Grelmicro(uses=sequence)

    def used() -> Grelmicro:
        micro = Grelmicro(uses=sequence[:split])
        for item, read in zip(sequence[split:], reads, strict=True):
            _UNRELATED_READS[read](micro)
            if item is not None:
                micro.use(item)
        return micro

    assert _outcome(used, pool) == _outcome(listed, pool)


def test_use_provider_after_a_read_overlapping_a_default_raises() -> None:
    """A late Provider that would fill a kind a default holds is refused."""
    late = _MemoryProvider()
    micro = Grelmicro(uses=[_CacheOnlyProvider()])
    cache = micro.cache

    with pytest.raises(
        ComponentAlreadyRegisteredError,
        match=r"registered when micro\.cache was read",
    ):
        micro.use(late)
    assert micro.cache is cache
    assert all(provider is not late for provider in micro.providers)


def test_use_provider_after_a_read_registers_its_defaults_at_once() -> None:
    """A late Provider that overlaps no default fills its kinds right away."""
    micro = Grelmicro(uses=[_CacheOnlyProvider()])
    _ = micro.cache
    micro.use(_RateLimiterOnlyProvider())

    assert isinstance(micro.get("ratelimiter"), RateLimiterComponent)
    with pytest.raises(
        ComponentAlreadyRegisteredError,
        match=r"registered when micro\.use\(_RateLimiterOnlyProvider\)",
    ):
        micro.use(RateLimiterComponent(MemoryRateLimiterAdapter()))


def test_late_providers_never_make_a_read_ambiguous() -> None:
    """Providers passed after the defaults are registered raise no ambiguity."""
    micro = Grelmicro(uses=[_MemoryProvider()])
    cache = micro.cache

    for late in (_CacheOnlyProvider(), _RateLimiterOnlyProvider()):
        with pytest.raises(ComponentAlreadyRegisteredError):
            micro.use(late)
    assert micro.cache is cache


def test_use_provider_after_lifecycle_only_providers_fills_at_once() -> None:
    """A late Provider fills the unclaimed kinds when no default exists yet."""
    explicit = Cache(MemoryCacheAdapter())
    micro = Grelmicro(uses=[_MemoryProvider(), _CacheOnlyProvider(), explicit])
    _ = micro.components
    micro.use(_RateLimiterOnlyProvider())

    assert micro.cache is explicit
    assert isinstance(micro.ratelimiter, RateLimiterComponent)


_RESOLVING_READS: list[Callable[[Grelmicro], object]] = [
    lambda micro: micro.components,
    lambda micro: getattr(micro, "cache", None),
    lambda micro: getattr(micro, "coordination", None),
]
"""Reads that register the pending Provider defaults."""


@given(st.data())
def test_reads_after_registration_never_raise_ambiguity(
    data: st.DataObject,
) -> None:
    """Once the defaults are registered, later Providers never make a read raise.

    A late `use(provider)` either is refused or leaves nothing pending.
    """
    pool = _pool()
    micro = Grelmicro()
    registered = False
    steps = data.draw(
        st.lists(
            st.one_of(
                st.tuples(st.just("use"), st.integers(0, len(pool) - 1)),
                st.tuples(
                    st.just("read"),
                    st.integers(0, len(_RESOLVING_READS) - 1),
                ),
            ),
            max_size=12,
        )
    )
    for step, position in steps:
        if step == "read":
            pending = bool(micro._pending_providers)
            try:
                _RESOLVING_READS[position](micro)
            except AmbiguousProviderError:
                assert not registered
            else:
                registered = registered or (
                    pending and not micro._pending_providers
                )
            continue
        item = pool[position]
        if item is None:
            continue
        items = [id(entry) for entry in micro._items]
        keys = {key: id(entry) for key, entry in micro._by_key.items()}
        try:
            micro.use(item)
        except ComponentAlreadyRegisteredError:
            assert [id(entry) for entry in micro._items] == items
            assert {
                key: id(entry) for key, entry in micro._by_key.items()
            } == keys
            continue
        if registered and isinstance(item, Provider):
            assert micro._pending_providers == []


# --- conditional registration ---


def test_uses_skips_none_entries() -> None:
    """A `None` entry in `uses=` registers nothing and does not raise."""
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), None])

    assert {component.kind for component in micro.components} == {"cache"}


def test_uses_all_none_registers_nothing() -> None:
    """A list of only `None` leaves an empty app rather than failing."""
    micro = Grelmicro(uses=[None, None])

    assert micro.components == ()


def test_uses_none_does_not_block_provider_defaults() -> None:
    """A skipped entry still leaves a lone Provider auto-registering its kinds."""
    micro = Grelmicro(uses=[None, _MemoryProvider()])

    assert isinstance(micro.get("cache"), Cache)
    assert isinstance(micro.get("coordination"), Coordination)


async def test_uses_none_is_not_lifecycled() -> None:
    """A skipped entry never reaches the exit stack."""
    micro = Grelmicro(uses=[None, Cache(MemoryCacheAdapter())])

    async with micro:
        assert isinstance(micro.get("cache"), Cache)


def test_use_none_raises_pointing_at_the_alternative() -> None:
    """`micro.use(None)` is a mistake and says what to write instead."""
    micro = Grelmicro()

    with pytest.raises(TypeError, match=r"use\(None\) registers nothing"):
        micro.use(None)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    ("adapter", "expected_kind"),
    [
        (MemoryCacheAdapter, "cache"),
        (MemoryCircuitBreakerAdapter, "circuitbreaker"),
        (MemoryRateLimiterAdapter, "ratelimiter"),
        (MemoryLockAdapter, "coordination"),
        (MemoryReadWriteLockAdapter, "coordination"),
        (MemoryLeaderElectionAdapter, "coordination"),
        (MemoryScheduleAdapter, "coordination"),
    ],
)
def test_bare_backend_resolves_to_one_kind(
    adapter: type,
    expected_kind: str,
) -> None:
    """Every first-party backend wraps into exactly the component it belongs to.

    `isinstance` against a `runtime_checkable` Protocol compares member names
    only, so a backend can satisfy more than one. `CircuitBreakerBackend`
    declares everything `RateLimiterBackend` does plus `_loop` and
    `is_shared`, so a breaker backend matches both. Pinning the resolved kind
    here keeps that from regressing into a silent misclassification.
    """
    component = _maybe_wrap_first_party_backend(adapter())

    assert component is not None
    assert component.kind == expected_kind


def test_backend_matching_unrelated_protocols_raises() -> None:
    """A backend naming two unrelated kinds is refused rather than guessed at."""

    class LockAndElection:
        """Satisfies `LockBackend` and `LeaderElectionBackend`, neither subsuming."""

        _loop = None

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def acquire(self, *args: object, **kwargs: object) -> None: ...
        async def locked(self, *args: object, **kwargs: object) -> None: ...
        async def owned(self, *args: object, **kwargs: object) -> None: ...
        async def release(self, *args: object, **kwargs: object) -> None: ...
        async def get(self, *args: object, **kwargs: object) -> None: ...
        async def acquire_or_renew(
            self, *args: object, **kwargs: object
        ) -> None: ...

    with pytest.raises(
        AmbiguousBackendError,
        match=r"matches more than one backend protocol",
    ):
        _maybe_wrap_first_party_backend(LockAndElection())


# --- Backend kinds by import string ---


@pytest.mark.parametrize("ref", _KINDS, ids=lambda ref: ref.label)
def test_backend_kind_members_match_its_protocol(ref: _KindRef) -> None:
    """Each kind lists exactly the members its Protocol matches on."""
    # Act
    protocol = load(ref.protocol)

    # Assert
    assert ref.members == protocol.__protocol_attrs__


def test_coordination_kinds_follow_the_coordination_slots() -> None:
    """The coordination kinds name the slots' protocols, in slot order."""
    # Act
    protocols = [
        load(ref.protocol)
        for ref in _KINDS
        if ref.label.startswith("Coordination(")
    ]
    labels = [
        ref.label for ref in _KINDS if ref.label.startswith("Coordination(")
    ]

    # Assert
    assert protocols == [slot.protocol for slot in COORDINATION_BACKENDS]
    assert labels == [
        f"Coordination({slot.keyword}=...)" for slot in COORDINATION_BACKENDS
    ]


@pytest.mark.parametrize(
    ("item", "expected"),
    [(object(), False), (MemoryRateLimiterAdapter(), True)],
)
def test_unloaded_protocol_matches_only_an_object_with_every_member(
    monkeypatch: pytest.MonkeyPatch, item: object, *, expected: bool
) -> None:
    """Without its module loaded, a kind may match only an object with all its members."""
    # Arrange
    ref = next(ref for ref in _KINDS if ref.label == "RateLimiterComponent")
    monkeypatch.delitem(sys.modules, ref.protocol.rpartition(".")[0])

    # Act
    could = _could_match(item, ref)

    # Assert
    assert could is expected


def test_object_of_no_kind_loads_no_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An object with none of the members matches no kind and loads no module."""
    # Arrange
    for ref in _KINDS:
        monkeypatch.delitem(
            sys.modules, ref.protocol.rpartition(".")[0], raising=False
        )

    # Act
    kinds = backend_kinds_of(object())

    # Assert
    assert kinds == []
    assert not any(
        ref.protocol.rpartition(".")[0] in sys.modules for ref in _KINDS
    )
