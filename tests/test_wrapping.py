"""Every grelmicro decorator refuses a function a registrar already holds.

A registering decorator records the function it is handed and returns
the same one, so a decorator applied below it wraps a name nothing will
call. The guard belongs to every decorator, so the contract test here
enumerates them rather than trusting each one to have been remembered.
"""

import ast
import asyncio
import functools
import gc
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from grelmicro import _markers as markers
from grelmicro._markers import Registered, mark_registered, registration_of
from grelmicro._wrapping import named, refuse_registered
from grelmicro.cache import TTLCache, cached
from grelmicro.health import HealthChecks
from grelmicro.idempotency import Idempotency, idempotent
from grelmicro.metrics import measure
from grelmicro.outbox import HandlerAlreadyRegisteredError, Message, Outbox
from grelmicro.outbox.memory import MemoryOutboxAdapter
from grelmicro.resilience import (
    Bulkhead,
    CircuitBreaker,
    Fallback,
    MemoryCircuitBreakerAdapter,
    MemoryRateLimiterAdapter,
    Pattern,
    RateLimiter,
    Retry,
    Shield,
    Stack,
    Timeout,
    fallback,
    retry,
    shield,
)
from grelmicro.task import Task, TaskRouter, Tasks
from grelmicro.task._interval import IntervalTask
from grelmicro.task.errors import TimezoneError
from grelmicro.trace import instrument

pytestmark = [pytest.mark.timeout(5)]

PACKAGE = Path(__file__).parent.parent / "grelmicro"

ATTEMPTS = 3
"""Retry attempts the imperative form has to make on a registered function."""


class _Registry:
    """Stands in for a registrar in a test that does not need a real one."""


REGISTRY = _Registry()
"""A registrar kept alive for the whole module, so no mark expires."""

KINDS = 2
"""Registrars holding one function, each of which keeps its own mark."""


async def scheduled_job() -> None:
    """Do nothing, from the module level a schedule requires."""


async def imperative_job() -> None:
    """Do nothing, from the module level a schedule requires."""


async def wrapped_job() -> None:
    """Do nothing, from the module level a schedule requires."""


async def constructed_job() -> None:
    """Do nothing, from the module level a schedule requires."""


async def unbuilt_job() -> None:
    """Do nothing, from the module level a schedule requires."""


async def subclassed_job() -> None:
    """Do nothing, from the module level a schedule requires."""


async def rebuilt_job() -> None:
    """Do nothing, from the module level a schedule requires."""


async def live_job() -> None:
    """Do nothing, from the module level a schedule requires."""


async def unreferenced_job() -> None:
    """Do nothing, from the module level a schedule requires."""


async def shared_job() -> None:
    """Do nothing, from the module level a schedule requires."""


async def twice_job() -> None:
    """Do nothing, from the module level a schedule requires."""


async def unowned_job() -> None:
    """Do nothing, from the module level a schedule requires."""


async def immortal_job() -> None:
    """Do nothing, from the module level a schedule requires."""


async def rogue_job() -> None:
    """Do nothing, from the module level a schedule requires."""


async def refused_job() -> None:
    """Do nothing, from the module level a schedule requires."""


async def refused_cron_job() -> None:
    """Do nothing, from the module level a schedule requires."""


async def first_check() -> None:
    """Answer a probe, from the module level."""


async def second_check() -> None:
    """Lose the name to the first check."""


WRAPS_WITHOUT_GUARD = {
    # Records calls on a fake backend's own methods, never a user function.
    "testing.py",
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

    mark_registered(job, kind, REGISTRY)

    with pytest.raises(TypeError, match="already registered as") as caught:
        apply(job)

    assert kind.value in str(caught.value)
    assert label


_WRAPPING_CALLS = frozenset({"wraps", "update_wrapper"})
"""Names that copy `__dict__`, and so carry a mark onto a wrapper."""

_UPDATE_WRAPPER = "update_wrapper"
"""The one form whose first argument is the wrapper, not the wrapped."""


def _is_update_wrapper(call: ast.Call) -> bool:
    """Return whether `call` is the `update_wrapper(wrapper, fn)` form."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr == _UPDATE_WRAPPER
    return isinstance(func, ast.Name) and func.id == _UPDATE_WRAPPER


def _is_wraps(call: ast.Call) -> bool:
    """Return whether `call` copies a function onto a wrapper.

    `functools.update_wrapper` copies the same `__dict__` that `wraps`
    does, so a decorator written with it has the same hole and has to
    be seen the same way.
    """
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr in _WRAPPING_CALLS
    return isinstance(func, ast.Name) and func.id in _WRAPPING_CALLS


def _called_name(call: ast.Call) -> str | None:
    """Return the name `call` calls, plain or as a method."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


_Func = ast.FunctionDef | ast.AsyncFunctionDef


def _parents(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    """Return each node's parent, so a node can name what encloses it."""
    return {
        child: node
        for node in ast.walk(tree)
        for child in ast.iter_child_nodes(node)
    }


_Scoped = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _qualified(node: _Scoped, parents: dict[ast.AST, ast.AST]) -> str:
    """Return `node`'s dotted path, so two `__call__`s are told apart.

    Every pattern class spells its decorator entry point `__call__`, so
    a bare name would let a guarded one cover an unguarded one in the
    same module.
    """
    parts = [node.name]
    current = parents.get(node)
    while current is not None:
        if isinstance(current, _Scoped):
            parts.append(current.name)
        current = parents.get(current)
    return ".".join(reversed(parts))


def _enclosing(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[str]:
    """Return the functions `node` sits inside, innermost first."""
    names: list[str] = []
    current = parents.get(node)
    while current is not None:
        if isinstance(current, _Func):
            names.append(_qualified(current, parents))
        current = parents.get(current)
    return names


def _wrappers(tree: ast.Module, parents: dict[ast.AST, ast.AST]) -> set[str]:
    """Return the functions built as a copy of the one they wrap.

    A guard inside one of these runs per call, long after the
    registration has taken the function it was handed, so it is not a
    guard on the decorator at all.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, _Func):
            if any(
                isinstance(decorator, ast.Call) and _is_wraps(decorator)
                for decorator in node.decorator_list
            ):
                names.add(_qualified(node, parents))
        elif (
            isinstance(node, ast.Call)
            and _is_update_wrapper(node)
            and node.args
        ):
            first = node.args[0]
            if isinstance(first, ast.Name):
                chain = _enclosing(node, parents)
                scope = f"{chain[0]}." if chain else ""
                names.add(f"{scope}{first.id}")
    return names


def _decorated(node: ast.Call, parents: dict[ast.AST, ast.AST]) -> str | None:
    """Return the function `node` decorates, when it is a decorator.

    The runtime wrapper is not where a guard belongs. A guard inside it
    runs per call, long after the registration has taken the function
    it was handed, so the wrapper is dropped from the chain that counts.
    """
    parent = parents.get(node)
    if isinstance(parent, _Func) and any(
        decorator is node for decorator in parent.decorator_list
    ):
        return _qualified(parent, parents)
    return None


class _Scan(NamedTuple):
    """What one pass over a module found."""

    guarded: set[str]
    calls: dict[str, set[str]]
    sites: list[set[str]]


def _scan(tree: ast.Module) -> _Scan:
    """Read the guards, the call graph, and the wrapping sites at once.

    Walking each function separately re-reads every nested body once
    per enclosing function, which is slow enough over a whole package
    to matter under coverage.
    """
    parents = _parents(tree)
    functions = {
        _qualified(n, parents) for n in ast.walk(tree) if isinstance(n, _Func)
    }
    by_name: dict[str, set[str]] = {}
    for qualified in functions:
        by_name.setdefault(qualified.rsplit(".", 1)[-1], set()).add(qualified)
    wrappers = _wrappers(tree, parents)
    scan = _Scan(set(), {name: set() for name in functions}, [])

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        chain = _enclosing(node, parents)
        name = _called_name(node)
        if name == "refuse_registered":
            if not any(holder in wrappers for holder in chain):
                scan.guarded.update(chain[:1])
        elif name is not None and name in by_name:
            for holder in chain[:1]:
                scan.calls[holder].update(by_name[name])
        if _is_wraps(node):
            wrapper = _decorated(node, parents)
            scan.sites.append({holder for holder in chain if holder != wrapper})
    return scan


def _covered(scan: _Scan) -> set[str]:
    """Return the functions a guard covers, directly or through a caller.

    A decorator may build its wrapper in a helper, as `@cached` does.
    The helper is covered when everything that calls it is covered, so
    the set grows until it stops changing.
    """
    guarded = set(scan.guarded)
    callers: dict[str, set[str]] = {name: set() for name in scan.calls}
    for caller, called in scan.calls.items():
        for name in called:
            callers[name].add(caller)

    while True:
        grown = {
            name
            for name, who in callers.items()
            if name not in guarded and who and who <= guarded
        }
        if not grown:
            return guarded
        guarded |= grown


def _unguarded_wraps_sites(source: str) -> int:
    """Count wrapping sites no guard covers."""
    scan = _scan(ast.parse(source))
    guarded = _covered(scan)
    return sum(1 for chain in scan.sites if not chain & guarded)


@pytest.mark.timeout(60)
def test_every_wrapping_decorator_carries_the_guard() -> None:
    """The fifteenth decorator is discovered, not remembered.

    Counted per wrapping site rather than per module, so a second
    decorator added to a module that already guards one is caught.
    """
    unguarded = {
        str(path.relative_to(PACKAGE))
        for path in PACKAGE.rglob("*.py")
        if _unguarded_wraps_sites(path.read_text())
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

    with pytest.raises(TypeError, match="was applied to a wrapper around"):
        Retry("r", when=Exception)(wrapper)


def test_a_mark_stops_counting_once_its_function_is_gone() -> None:
    """A registration keeps what it recorded, so a dead mark holds nothing."""

    async def job() -> None:
        """Stand in for a function a registrar recorded."""

    mark_registered(job, Registered.TASK, REGISTRY)

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
    mark_registered(unmarkable, Registered.TASK, REGISTRY)

    assert registration_of(unmarkable) is None


def test_a_generator_producer_is_still_reached_by_the_guard() -> None:
    """`@cached` streams async generators, so its guard runs before that."""

    async def produce() -> AsyncIterator[int]:
        """Stream items."""
        yield 1

    mark_registered(produce, Registered.OUTBOX_HANDLER, REGISTRY)

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
    registration = markers.Registration(Registered.TASK, None, None, None)

    assert registration.holds(object()) is True


def test_naming_a_function_never_swallows_an_interrupt() -> None:
    """Naming a value for a message runs caller code as much as reading it."""

    class Interrupting:
        """A value whose name interrupts."""

        def __getattr__(self, name: str) -> object:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        named(Interrupting())


def test_the_contract_test_fails_on_an_unguarded_decorator() -> None:
    """A guard nothing checks is a guard the next decorator skips."""
    guarded = """
import functools
def decorate(fn):
    refuse_registered(fn, "@x")

    @functools.wraps(fn)
    def wrapper(): ...
    return wrapper
"""
    added = (
        guarded
        + """
def decorate_too(fn):
    @functools.wraps(fn)
    def wrapper(): ...
    return wrapper
"""
    )
    assert _unguarded_wraps_sites(guarded) == 0
    assert _unguarded_wraps_sites(added) == 1


def test_the_contract_test_sees_a_guard_that_runs_too_late() -> None:
    """A guard inside the wrapper fires per call, after the registration."""
    too_late = """
import functools
def decorate(fn):
    @functools.wraps(fn)
    def wrapper():
        refuse_registered(fn, "@x")
    return wrapper
"""
    assert _unguarded_wraps_sites(too_late) == 1


def test_the_contract_test_sees_update_wrapper_too() -> None:
    """`update_wrapper` copies the same `__dict__` that `wraps` does."""
    unguarded = """
import functools
def decorate(fn):
    def wrapper(): ...
    functools.update_wrapper(wrapper, fn)
    return wrapper
"""
    guarded = """
import functools
def decorate(fn):
    refuse_registered(fn, "@x")

    def wrapper(): ...
    functools.update_wrapper(wrapper, fn)
    return wrapper
"""
    assert _unguarded_wraps_sites(unguarded) == 1
    assert _unguarded_wraps_sites(guarded) == 0


def test_a_method_of_another_instance_is_left_alone() -> None:
    """The mark sits on the class function every instance shares."""

    class Repo:
        """Two objects of one class, one of them registered."""

        async def ping(self) -> None:
            """Answer a probe."""

    checks = HealthChecks()
    registered, other = Repo(), Repo()
    checks.add("db", registered.ping)

    assert registration_of(registered.ping) is not None
    assert registration_of(other.ping) is None

    Timeout("t", seconds=1)(other.ping)

    with pytest.raises(TypeError, match="already registered as a health check"):
        Timeout("t", seconds=1)(registered.ping)


def test_a_handler_the_registry_refused_is_never_marked() -> None:
    """A mark records what a registrar holds, so it follows the registry."""
    outbox = Outbox(MemoryOutboxAdapter())

    async def first(message: Message[Any]) -> None:
        """Handle a message."""

    async def second(message: Message[Any]) -> None:
        """Lose the topic to the first handler."""

    outbox.handler("topic")(first)

    with pytest.raises(HandlerAlreadyRegisteredError):
        outbox.handler("topic")(second)

    assert registration_of(second) is None


def test_a_task_passed_to_the_constructor_is_marked_too() -> None:
    """`Tasks(tasks=[...])` reaches the schedule without `add_task`."""
    tasks = Tasks(tasks=[IntervalTask(function=constructed_job, seconds=60)])

    assert registration_of(constructed_job) is not None
    assert tasks.tasks


def test_reading_what_a_method_is_bound_to_never_raises() -> None:
    """`__self__` is caller code on a proxy, so both readers answer."""

    class Hostile:
        """A value whose `__self__` raises."""

        def __getattr__(self, name: str) -> object:
            msg = "unbound proxy"
            raise RuntimeError(msg)

    mark_registered(Hostile(), Registered.TASK, REGISTRY)

    assert registration_of(Hostile()) is None


def test_an_owner_no_reference_can_name_is_left_unmarked() -> None:
    """A mark that cannot name its instance would refuse every sibling."""

    class Owner:
        """An instance that refuses a weak reference."""

        __slots__ = ()

        async def ping(self) -> None:
            """Answer a probe."""

    registered, sibling = Owner(), Owner()
    mark_registered(registered.ping, Registered.TASK, REGISTRY)

    assert registration_of(registered.ping) is None
    assert registration_of(sibling.ping) is None


def test_an_interrupt_while_reading_the_owner_is_never_swallowed() -> None:
    """A mark reads `__self__` as well, so its guard answers too."""

    class Interrupting:
        """A value that interrupts only on what it is bound to."""

        def __getattr__(self, name: str) -> object:
            if name == "__self__":
                raise KeyboardInterrupt
            raise AttributeError(name)

    with pytest.raises(KeyboardInterrupt):
        mark_registered(Interrupting(), Registered.TASK, REGISTRY)


def test_a_mark_attribute_that_is_not_a_mark_is_ignored() -> None:
    """The attribute name is not private enough to trust what it holds."""

    async def job() -> None:
        """Stand in for a function something else wrote an attribute on."""

    setattr(job, markers.REGISTRATION, "not a registration")

    assert registration_of(job) is None


def _marked(kind: Registered = Registered.TASK) -> tuple[markers.Registration]:
    """Return a mark naming nothing, so only the read under test matters."""
    return (markers.Registration(kind, None, None, None),)


def test_an_interrupt_while_reading_the_owner_of_a_mark_is_never_swallowed() -> (
    None
):
    """A marked value still has its `__self__` read, so that guard answers."""

    class Interrupting:
        """A marked value that interrupts on what it is bound to."""

        def __init__(self) -> None:
            self.__dict__[markers.REGISTRATION] = _marked()

        def __getattr__(self, name: str) -> object:
            if name == "__self__":
                raise KeyboardInterrupt
            raise AttributeError(name)

    with pytest.raises(KeyboardInterrupt):
        registration_of(Interrupting())


def test_a_hostile_owner_on_a_marked_value_answers_rather_than_raising() -> (
    None
):
    """`__self__` is caller code, so a marked value cannot raise out of it."""

    class Hostile:
        """A marked value whose `__self__` raises."""

        def __init__(self) -> None:
            self.__dict__[markers.REGISTRATION] = _marked()

        def __getattr__(self, name: str) -> object:
            if name == "__self__":
                msg = "unbound proxy"
                raise RuntimeError(msg)
            raise AttributeError(name)

    assert registration_of(Hostile()) is None


def test_a_value_that_holds_no_attributes_reads_as_unmarked() -> None:
    """Reading what an object holds refuses an object that holds nothing."""

    class Slotted:
        """A value with no `__dict__` of its own."""

        __slots__ = ()

    assert registration_of(Slotted()) is None


def _hostile_dict(error: type[BaseException]) -> object:
    """Return a value whose `__dict__` raises `error` when it is read.

    Built at runtime, because a class that declares `__dict__` as a
    property is not something a type checker will accept written out.
    """

    def raising(_self: object) -> dict[str, object]:
        raise error

    return type("Hostile", (), {"__dict__": property(raising)})()


def test_a_hostile_dict_answers_rather_than_raising() -> None:
    """`__dict__` is caller code on a proxy, so reading it cannot raise."""
    assert registration_of(_hostile_dict(RuntimeError)) is None


def test_an_interrupt_while_reading_the_dict_is_never_swallowed() -> None:
    """A real interrupt still gets out of the raise-proof read."""
    with pytest.raises(KeyboardInterrupt):
        registration_of(_hostile_dict(KeyboardInterrupt))


def test_an_instance_of_a_registered_class_is_left_alone() -> None:
    """A mark on a class is not a mark on everything built from it."""

    class Probe:
        """A callable class a registrar was handed."""

        async def __call__(self) -> None:
            """Answer a probe."""

    mark_registered(Probe, Registered.HEALTH_CHECK, REGISTRY)

    assert registration_of(Probe) is not None
    assert registration_of(Probe()) is None

    Retry("r", when=Exception)(Probe())


def test_a_mark_of_another_kind_is_never_superseded() -> None:
    """Two registrars holding one function each keep their own mark."""

    async def job() -> None:
        """Stand in for a function two registrars hold."""

    mark_registered(job, Registered.TASK, REGISTRY)
    mark_registered(job, Registered.OUTBOX_HANDLER, REGISTRY)

    assert len(getattr(job, markers.REGISTRATION)) == KINDS


async def test_a_stack_runs_a_registered_function_imperatively() -> None:
    """`Stack.run` wraps the call it makes, not the registration."""
    checks = HealthChecks()
    calls: list[int] = []

    @checks.check("db")
    async def probe() -> None:
        """Fail every attempt, counting the calls."""
        calls.append(1)
        raise RuntimeError

    stack = Stack("s", patterns=[Retry("r", when=Exception, attempts=ATTEMPTS)])

    with pytest.raises(RuntimeError):
        await stack.run(probe)

    assert len(calls) == ATTEMPTS

    with pytest.raises(TypeError, match="already registered as a health check"):
        stack(probe)


def test_a_sibling_instance_survives_being_wrapped_twice() -> None:
    """Wrapping drops `__self__`, so answering for a copy refuses siblings.

    A registrar that took a bound method is answered for that instance
    alone. Catching a `functools.wraps` copy of it would cost every
    other instance of the class, which `add_provider` creates by the
    handful, so the narrower miss is the one worth keeping.
    """

    class Svc:
        """Two objects of one class, one of them registered."""

        async def check(self) -> None:
            """Answer a probe."""

    checks = HealthChecks()
    registered, sibling = Svc(), Svc()
    checks.add("a", registered.check)

    layered = Timeout("t", seconds=1)(sibling.check)
    Retry("r", when=Exception)(layered)

    with pytest.raises(TypeError, match="already registered as a health check"):
        Timeout("t", seconds=1)(registered.check)


def test_the_class_function_of_a_registered_method_is_refused() -> None:
    """The registration runs through the class function, so it is held too.

    A bound method keeps the function it was built from, so decorating
    the class function afterwards leaves the registration calling the
    original. A sibling instance still names itself and stays free.
    """

    class Repo:
        """An object whose method answers a probe."""

        async def ping(self) -> None:
            """Answer a probe."""

    checks = HealthChecks()
    repo, sibling = Repo(), Repo()
    checks.add("db", repo.ping)

    with pytest.raises(TypeError, match="already registered as a health check"):
        Retry("r", when=Exception)(Repo.ping)

    Retry("r", when=Exception)(sibling.ping)


def test_a_registration_whose_instance_is_gone_stops_counting() -> None:
    """Nothing holds a method of an object that no longer exists."""

    class Repo:
        """An object whose method answers a probe."""

        async def ping(self) -> None:
            """Answer a probe."""

    repo = Repo()
    mark_registered(repo.ping, Registered.HEALTH_CHECK, REGISTRY)
    del repo
    gc.collect()

    assert registration_of(Repo.ping) is None


def test_a_task_exposing_no_function_is_added_unmarked() -> None:
    """A `Task` of your own is marked when it exposes one, not otherwise."""

    class Custom:
        """A task that runs no function of its own."""

        function = "not callable"

        @property
        def name(self) -> str:
            """Name the task."""
            return "custom"

        async def __call__(
            self,
            *,
            ready: asyncio.Future[None] | None = None,
            stop: asyncio.Event | None = None,
        ) -> None:
            """Run the task."""

    tasks = Tasks()
    tasks.add_task(Custom())

    assert len(tasks.tasks) == 1


def test_a_bound_mark_never_evicts_a_plain_one() -> None:
    """Two registrations of one function are both live, so both are kept."""

    class Svc:
        """A class registered both by itself and by an instance."""

        async def check(self) -> None:
            """Answer a probe."""

    instance = Svc()
    mark_registered(Svc.check, Registered.HEALTH_CHECK, REGISTRY)
    mark_registered(instance.check, Registered.HEALTH_CHECK, REGISTRY)

    assert registration_of(Svc.check) is not None
    assert registration_of(instance.check) is not None


def test_a_shield_refusal_names_the_decorator_the_user_wrote() -> None:
    """`Shield` names itself after the function, which names nothing."""
    checks = HealthChecks()

    @checks.check("db")
    async def check_db() -> None:
        """Answer a probe."""

    with pytest.raises(TypeError, match=r"@shield would only"):
        shield(check_db)

    with pytest.raises(TypeError, match=r"@shield\.api would only"):
        shield.api()(check_db)


def test_a_task_whose_function_attribute_raises_is_still_added() -> None:
    """A `Task` is caller code, so reading what it exposes cannot raise."""

    class Hostile:
        """A task whose `function` refuses to be read."""

        @property
        def function(self) -> object:
            """Raise instead of naming the function."""
            msg = "not ready"
            raise RuntimeError(msg)

        @property
        def name(self) -> str:
            """Name the task."""
            return "hostile"

        async def __call__(
            self,
            *,
            ready: asyncio.Future[None] | None = None,
            stop: asyncio.Event | None = None,
        ) -> None:
            """Run the task."""

    tasks = Tasks()
    tasks.add_task(Hostile())

    assert len(tasks.tasks) == 1


def test_a_task_whose_function_attribute_interrupts_is_never_swallowed() -> (
    None
):
    """A real interrupt still gets out of the raise-proof read."""

    class Interrupting:
        """A task whose `function` interrupts."""

        @property
        def function(self) -> object:
            """Interrupt instead of naming the function."""
            raise KeyboardInterrupt

        @property
        def name(self) -> str:
            """Name the task."""
            return "interrupting"

        async def __call__(
            self,
            *,
            ready: asyncio.Future[None] | None = None,
            stop: asyncio.Event | None = None,
        ) -> None:
            """Run the task."""

    with pytest.raises(KeyboardInterrupt):
        Tasks().add_task(Interrupting())


def test_a_router_that_refuses_its_timezone_marks_nothing() -> None:
    """A mark records what a router holds, so it follows construction."""
    with pytest.raises(TimezoneError):
        TaskRouter(
            tasks=[IntervalTask(function=unbuilt_job, seconds=60)],
            timezone="Not/AZone",
        )

    assert registration_of(unbuilt_job) is None


def test_a_subclass_keeps_its_own_construction_order() -> None:
    """The constructor never runs an override against a half-built object."""

    class Audited(TaskRouter):
        """A router that records what it was asked to add."""

        def __init__(self, *, tasks: list[Task]) -> None:
            super().__init__(tasks=tasks)
            self.seen: list[Task] = []

        def add_task(self, task: Task) -> None:
            """Record the task, then add it."""
            self.seen.append(task)
            super().add_task(task)

    router = Audited(tasks=[IntervalTask(function=subclassed_job, seconds=60)])

    assert router.seen == []
    assert len(router.tasks) == 1
    assert registration_of(subclassed_job) is not None

    router.add_task(IntervalTask(function=constructed_job, seconds=30))

    assert len(router.seen) == 1


def test_the_outermost_registrar_is_the_one_named() -> None:
    """Decorators apply upwards, so the last registrar is the one read."""

    async def job() -> None:
        """Stand in for a function two registrars hold."""

    mark_registered(job, Registered.TASK, REGISTRY)
    mark_registered(job, Registered.HEALTH_CHECK, REGISTRY)

    registration = registration_of(job)
    assert registration is not None
    assert registration.kind is Registered.HEALTH_CHECK


@pytest.mark.parametrize(
    "pattern",
    ["timeout", "retry", "fallback", "bulkhead", "breaker"],
)
async def test_every_pattern_composes_a_registered_function_in_run(
    pattern: str,
) -> None:
    """`Stack.run` wraps the call it makes, whichever patterns it holds."""
    checks = HealthChecks()

    @checks.check("db")
    async def probe() -> None:
        """Answer a probe."""

    async with MemoryCircuitBreakerAdapter() as backend:
        patterns: dict[str, Pattern] = {
            "timeout": Timeout("t", seconds=5),
            "retry": Retry("r", when=Exception),
            "fallback": Fallback("f", when=Exception, default=None),
            "bulkhead": Bulkhead("b", max_concurrent=1),
            "breaker": CircuitBreaker("c", backend=backend),
        }
        stack = Stack(pattern, patterns=[patterns[pattern]])

        await stack.run(probe)

        assert registration_of(probe) is not None


def _mark_from_a_passing_registry(function: object) -> None:
    """Mark `function` from a registrar that does not outlive this call.

    The registrar is built and dropped inside this frame, so it is gone
    by the time the caller looks, with no reliance on when a collection
    happens to run.
    """

    class Registry:
        """Stand in for a `Tasks`, a `HealthChecks`, or an `Outbox`."""

    mark_registered(function, Registered.TASK, Registry())


def test_a_mark_dies_with_the_registry_that_wrote_it() -> None:
    """An app factory builds one module-level function into a fresh app."""
    _mark_from_a_passing_registry(rebuilt_job)
    gc.collect()

    assert registration_of(rebuilt_job) is None

    tasks = Tasks()
    tasks.every(seconds=60)(Retry("r", when=Exception)(rebuilt_job))

    assert tasks.tasks


def test_a_mark_answers_while_its_registry_is_alive() -> None:
    """The mark holds only as long as something can still run it."""

    class Registry:
        """Stand in for a `Tasks`, a `HealthChecks`, or an `Outbox`."""

    registry = Registry()
    mark_registered(live_job, Registered.TASK, registry)

    assert registration_of(live_job) is not None
    assert registry is not None


def test_a_second_registry_never_evicts_a_live_one() -> None:
    """Two registries holding one function each keep their own mark."""
    live = Tasks()
    live.every(seconds=60)(shared_job)

    _mark_from_a_passing_registry(shared_job)
    gc.collect()

    assert registration_of(shared_job) is not None
    assert live.tasks

    with pytest.raises(TypeError, match="already registered as a task"):
        Retry("r", when=Exception)(shared_job)


def test_the_same_registry_replaces_its_own_mark() -> None:
    """One registry registering twice needs one mark, not two."""
    tasks = Tasks()
    tasks.every(seconds=60)(twice_job)
    tasks.every(seconds=30)(twice_job)

    assert len(getattr(twice_job, markers.REGISTRATION)) == 1


def test_a_registry_no_reference_can_name_leaves_no_mark() -> None:
    """A mark that cannot be dropped would refuse for the whole process."""

    class Registry:
        """A registrar with no `__weakref__` of its own."""

        __slots__ = ()

    for _ in range(3):
        mark_registered(immortal_job, Registered.TASK, Registry())

    assert getattr(immortal_job, markers.REGISTRATION, ()) == ()
    assert registration_of(immortal_job) is None

    Retry("r", when=Exception)(immortal_job)


def test_a_task_decorator_marks_whatever_the_router_does_with_it() -> None:
    """The guard does not rest on how a subclass routes the task."""

    class Rogue(TaskRouter):
        """A router that schedules without the inherited adder."""

        def add_task(self, task: Task) -> None:
            """Schedule by another route entirely."""
            self._tasks.append(task)

    router = Rogue()
    router.every(seconds=60)(rogue_job)

    assert registration_of(rogue_job) is not None
    assert len(getattr(rogue_job, markers.REGISTRATION)) == 1
    assert router.tasks


def test_a_task_the_router_refused_is_never_marked() -> None:
    """A mark records what a registrar holds, so it follows the schedule."""
    tasks = Tasks()

    with pytest.raises(ValueError, match="seconds must be greater than 0"):
        tasks.every(seconds=-1)(refused_job)

    assert registration_of(refused_job) is None

    Retry("r", when=Exception)(refused_job)


def test_a_cron_the_router_refused_is_never_marked() -> None:
    """The same holds for the other task decorator."""
    tasks = Tasks()

    with pytest.raises(ValueError, match=r"[Cc]ron"):
        tasks.cron("not a cron expression")(refused_cron_job)

    assert registration_of(refused_cron_job) is None


def test_a_check_the_registry_refused_is_never_marked() -> None:
    """Every registrar marks only once it holds what it was handed."""
    checks = HealthChecks()
    checks.add("db", first_check)

    with pytest.raises(ValueError, match="already registered"):
        checks.add("db", second_check)

    assert registration_of(second_check) is None

    Retry("r", when=Exception)(second_check)


def test_the_contract_test_does_not_credit_a_shared_factory() -> None:
    """A guard covers the decorator it is in, not every factory around it."""
    shared = """
import functools
def factory():
    def wrap(fn):
        refuse_registered(fn, "@x")

        @functools.wraps(fn)
        def wrapper(): ...
        return wrapper

    def wrap_two(fn):
        @functools.wraps(fn)
        def wrapper(): ...
        return wrapper
    return wrap, wrap_two
"""
    assert _unguarded_wraps_sites(shared) == 1


def test_the_contract_test_sees_a_late_guard_in_the_other_form() -> None:
    """`update_wrapper` names the wrapper, so the late guard is seen there."""
    too_late = """
import functools
def decorate(fn):
    def wrapper():
        refuse_registered(fn, "@x")
    functools.update_wrapper(wrapper, fn)
    return wrapper
"""
    assert _unguarded_wraps_sites(too_late) == 1


def test_the_contract_test_keeps_a_guard_beside_a_function_named_fn() -> None:
    """`wraps(fn)` names the wrapped function, which is not the wrapper."""
    named_fn = """
import functools
def fn(): ...

def decorate(target):
    refuse_registered(target, "@x")
    return functools.wraps(target)(lambda: None)
"""
    assert _unguarded_wraps_sites(named_fn) == 0
