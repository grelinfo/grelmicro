"""Every grelmicro decorator refuses a function a registrar already holds.

A registering decorator records the function it is handed and returns
the same one, so a decorator applied below it wraps a name nothing will
call. The guard belongs to every decorator, so the contract test here
enumerates them rather than trusting each one to have been remembered.
"""

import functools
import gc
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from grelmicro import _markers as markers
from grelmicro._markers import Registered, mark_registered, registration_of
from grelmicro._wrapping import named, refuse_registered
from grelmicro.cache import TTLCache, cached
from grelmicro.health import HealthChecks
from grelmicro.idempotency import Idempotency, idempotent
from grelmicro.metrics import measure
from grelmicro.outbox import Message, Outbox
from grelmicro.outbox.memory import MemoryOutboxAdapter
from grelmicro.resilience import (
    Bulkhead,
    CircuitBreaker,
    Fallback,
    MemoryCircuitBreakerAdapter,
    MemoryRateLimiterAdapter,
    RateLimiter,
    Retry,
    Shield,
    Stack,
    Timeout,
    fallback,
    retry,
    shield,
)
from grelmicro.task import Tasks
from grelmicro.task._interval import IntervalTask
from grelmicro.trace import instrument

pytestmark = [pytest.mark.timeout(5)]

PACKAGE = Path(__file__).parent.parent / "grelmicro"


async def scheduled_job() -> None:
    """Do nothing, from the module level a schedule requires."""


async def imperative_job() -> None:
    """Do nothing, from the module level a schedule requires."""


async def wrapped_job() -> None:
    """Do nothing, from the module level a schedule requires."""


WRAPS_WITHOUT_GUARD = {
    # Records calls on a fake backend's own methods, never a user function.
    "testing.py",
    # Delegates to `Shield.__call__`, which refuses before this wraps.
    "resilience/shield/_decorator.py",
}
"""Modules that call `functools.wraps` and need no guard of their own."""


def _decorators() -> list[tuple[str, Callable[[Any], Any]]]:
    """Return every public decorator, as a label and a way to apply it."""
    cb_backend = MemoryCircuitBreakerAdapter()
    rl_backend = MemoryRateLimiterAdapter()
    limiter = RateLimiter.token_bucket(
        "rl", capacity=1, refill_rate=1, backend=rl_backend
    )
    return [
        ("Stack", Stack("s", patterns=[Retry("r", when=Exception)])),
        ("Retry", Retry("r", when=Exception)),
        ("@retry", retry(when=Exception)),
        ("Fallback", Fallback("f", when=Exception, default=None)),
        ("@fallback", fallback(when=Exception, default=None)),
        ("Shield", Shield.api("sh")),
        ("@shield", shield),
        ("@shield.api", shield.api()),
        ("CircuitBreaker", CircuitBreaker("cb", backend=cb_backend)),
        ("Bulkhead", Bulkhead("b", max_concurrent=1)),
        ("Timeout", Timeout("t", seconds=1)),
        ("RateLimiter", limiter),
        ("RateLimiterBinding", limiter(key="k")),
        ("@cached", cached(TTLCache(ttl=1), key="k")),
        ("@idempotent", idempotent(Idempotency("i"), key=lambda: "k")),
        ("@measure", measure),
        ("@instrument", instrument),
    ]


@pytest.mark.parametrize(
    ("label", "apply"), _decorators(), ids=[label for label, _ in _decorators()]
)
@pytest.mark.parametrize(
    "kind",
    list(Registered),
    ids=[kind.name.lower() for kind in Registered],
)
def test_every_decorator_refuses_a_registered_function(
    label: str, apply: Callable[[Any], Any], kind: Registered
) -> None:
    """One registrar records it, and every decorator says so."""

    async def job() -> None:
        """Stand in for the function a registrar recorded."""

    mark_registered(job, kind)

    with pytest.raises(TypeError, match="already registered as") as caught:
        apply(job)

    assert kind.value in str(caught.value)
    assert label


def test_every_wrapping_module_carries_the_guard() -> None:
    """The fifteenth decorator is discovered, not remembered."""
    unguarded = {
        str(path.relative_to(PACKAGE))
        for path in PACKAGE.rglob("*.py")
        if "functools.wraps(" in path.read_text()
        and "refuse_registered" not in path.read_text()
    }

    assert unguarded == WRAPS_WITHOUT_GUARD


async def test_each_registrar_marks_what_it_records() -> None:
    """Three registrars hold a function the same way, so all three mark."""
    tasks = Tasks()
    checks = HealthChecks()
    outbox = Outbox(MemoryOutboxAdapter())

    tasks.every(seconds=60)(scheduled_job)

    @checks.check("db")
    async def probe() -> None:
        """Answer a probe."""

    @outbox.handler("topic")
    async def handle(message: Message[Any]) -> None:
        """Handle a message."""

    marks = {}
    for fn in (scheduled_job, probe, handle):
        registration = registration_of(fn)
        assert registration is not None
        marks[fn.__name__] = registration.kind

    assert marks == {
        "scheduled_job": Registered.TASK,
        "probe": Registered.HEALTH_CHECK,
        "handle": Registered.OUTBOX_HANDLER,
    }


def test_a_task_added_imperatively_is_marked_too() -> None:
    """The decorators are one door to the schedule, not the only one."""
    tasks = Tasks()

    tasks.add_task(IntervalTask(function=imperative_job, seconds=60))

    assert registration_of(imperative_job) is not None


def test_a_wraps_copy_is_named_as_a_wrapper_not_as_the_registration() -> None:
    """`functools.wraps` copies the mark, so the message says which it is."""
    tasks = Tasks()
    tasks.every(seconds=60)(wrapped_job)

    @functools.wraps(wrapped_job)
    async def wrapper() -> None:
        """Stand in for a decorator grelmicro does not own."""

    registration = registration_of(wrapper)
    assert registration is not None
    assert registration.holds(wrapped_job)
    assert not registration.holds(wrapper)

    with pytest.raises(TypeError, match="wraps a function already registered"):
        Retry("r", when=Exception)(wrapper)


def test_a_mark_stops_counting_once_its_function_is_gone() -> None:
    """A registration keeps what it recorded, so a dead mark holds nothing."""

    async def job() -> None:
        """Stand in for a function a registrar recorded."""

    mark_registered(job, Registered.TASK)

    async def wrapper() -> None:
        """Carry the copied mark past the end of the original."""

    wrapper.__dict__.update(job.__dict__)
    del job
    gc.collect()

    assert registration_of(wrapper) is None


def test_an_unmarked_function_is_wrapped_untouched() -> None:
    """The guard costs a lookup and nothing else."""

    async def job() -> None:
        """Stand in for an ordinary function."""

    refuse_registered(job, "Retry 'r'")

    assert registration_of(job) is None


def test_reading_a_mark_never_raises_and_never_swallows_an_interrupt() -> None:
    """The read runs on caller code, so it answers rather than propagates."""

    class Hostile:
        """A value whose attributes raise."""

        def __getattr__(self, name: str) -> object:
            msg = "unbound proxy"
            raise RuntimeError(msg)

    class Interrupting:
        """A value whose attribute access interrupts."""

        def __getattr__(self, name: str) -> object:
            raise KeyboardInterrupt

    assert registration_of(Hostile()) is None
    assert named(Hostile()) is not None

    with pytest.raises(KeyboardInterrupt):
        registration_of(Interrupting())


def test_a_callable_that_takes_no_mark_is_left_alone() -> None:
    """Best effort: an unmarkable callable keeps the behaviour it had."""

    class Slotted:
        """A callable that accepts no attribute of its own."""

        __slots__ = ()

        async def __call__(self) -> None:
            """Do nothing."""

    unmarkable = Slotted()
    mark_registered(unmarkable, Registered.TASK)

    assert registration_of(unmarkable) is None


def test_a_generator_producer_is_still_reached_by_the_guard() -> None:
    """`@cached` streams async generators, so its guard runs before that."""

    async def produce() -> AsyncIterator[int]:
        """Stream items."""
        yield 1

    mark_registered(produce, Registered.OUTBOX_HANDLER)

    with pytest.raises(TypeError, match="already registered as an outbox"):
        cached(TTLCache(ttl=1), key="k")(produce)


def test_the_marker_module_names_what_it_exports() -> None:
    """The private surface stays the one the wrappers import."""
    assert set(markers.__all__) == {
        "Registered",
        "Registration",
        "mark_registered",
        "registration_of",
    }


def test_a_mark_that_could_not_name_its_function_still_counts() -> None:
    """A callable no weak reference can name is held by presence alone."""
    registration = markers.Registration(Registered.TASK, None)

    assert registration.holds(object()) is True


def test_reading_the_mark_alone_never_swallows_an_interrupt() -> None:
    """The read reaches past `__func__`, so its own guard answers too."""

    class Selective:
        """A value that interrupts only the mark's own attribute."""

        def __getattr__(self, name: str) -> object:
            if name == markers.REGISTRATION:
                raise KeyboardInterrupt
            raise AttributeError(name)

    with pytest.raises(KeyboardInterrupt):
        registration_of(Selective())


def test_naming_a_function_never_swallows_an_interrupt() -> None:
    """Naming a value for a message runs caller code as much as reading it."""

    class Interrupting:
        """A value whose name interrupts."""

        def __getattr__(self, name: str) -> object:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        named(Interrupting())
