"""The first-party backend kinds, and the Component each one belongs to.

Exposes:

- `backend_kinds_of`: every backend kind an object satisfies.
- `load`: import the object an import string names.
- `most_specific_backend`: pick one kind for a backend matching several.
- `resolve_source`: read a Component's first argument as a Provider or a
  backend of its own kind, and refuse anything else.
"""

from __future__ import annotations

import sys
from functools import cache
from importlib import import_module
from inspect import getattr_static
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from grelmicro._component import instantiate_if_class
from grelmicro.providers._base import Provider

if TYPE_CHECKING:
    from collections.abc import Sequence


class BackendKind(NamedTuple):
    """One backend Protocol and the Component that takes it."""

    protocol: type
    """Runtime-checkable Protocol a backend of this kind satisfies."""

    label: str
    """How an error names the Component, `Coordination(lock=...)`."""

    factory: str | None
    """Import string of the Component built around a bare backend, `None`
    when `uses=[...]` does not wrap this kind.

    `load` imports it when the kind is first built.
    """


class _KindRef(NamedTuple):
    """One first-party backend kind, named by import strings."""

    protocol: str
    """Import string of the runtime-checkable Protocol."""

    members: frozenset[str]
    """The member names the Protocol matches on."""

    label: str
    """How an error names the Component."""

    factory: str | None
    """Import string of the Component that wraps a bare backend, or `None`."""


_COORDINATION = "grelmicro.coordination._component.Coordination"
"""Import string of the Component every coordination kind belongs to."""

_KINDS: Final = (
    _KindRef(
        "grelmicro.cache._protocol.CacheBackend",
        frozenset(
            {
                "__aenter__",
                "__aexit__",
                "_loop",
                "clear",
                "delete",
                "delete_many",
                "delete_tags",
                "get",
                "get_many",
                "set",
                "set_many",
            }
        ),
        "Cache",
        "grelmicro.cache._component.Cache",
    ),
    _KindRef(
        "grelmicro.resilience._protocol.CircuitBreakerBackend",
        frozenset({"__aenter__", "__aexit__", "_loop", "bind", "is_shared"}),
        "CircuitBreakerComponent",
        "grelmicro.resilience._components.CircuitBreakerComponent",
    ),
    _KindRef(
        "grelmicro.resilience._protocol.RateLimiterBackend",
        frozenset({"__aenter__", "__aexit__", "bind"}),
        "RateLimiterComponent",
        "grelmicro.resilience._components.RateLimiterComponent",
    ),
    _KindRef(
        "grelmicro.coordination._protocol.LockBackend",
        frozenset(
            {
                "__aenter__",
                "__aexit__",
                "_loop",
                "acquire",
                "locked",
                "owned",
                "release",
            }
        ),
        "Coordination(lock=...)",
        _COORDINATION,
    ),
    _KindRef(
        "grelmicro.coordination._protocol.ReadWriteLockBackend",
        frozenset(
            {
                "__aenter__",
                "__aexit__",
                "_loop",
                "acquire_read",
                "acquire_write",
                "cancel_intent",
                "downgrade",
                "owned_read",
                "owned_write",
                "release_read",
                "release_write",
                "state",
            }
        ),
        "Coordination(readwritelock=...)",
        _COORDINATION,
    ),
    _KindRef(
        "grelmicro.coordination._protocol.LeaderElectionBackend",
        frozenset(
            {"__aenter__", "__aexit__", "acquire_or_renew", "get", "release"}
        ),
        "Coordination(leaderelection=...)",
        _COORDINATION,
    ),
    _KindRef(
        "grelmicro.coordination._protocol.ScheduleBackend",
        frozenset({"__aenter__", "__aexit__", "_loop", "claim", "last_fired"}),
        "Coordination(schedule=...)",
        _COORDINATION,
    ),
    _KindRef(
        "grelmicro.outbox._protocol.OutboxBackend",
        frozenset(
            {
                "__aenter__",
                "__aexit__",
                "claim",
                "complete",
                "enqueue",
                "purge",
                "redrive",
                "reschedule",
                "wait_notify",
            }
        ),
        "Outbox",
        None,
    ),
)
"""Every first-party backend kind, in the order they are tested.

The coordination kinds follow `COORDINATION_BACKENDS`, one per backend a
`Coordination` holds.
"""


@cache
def load(path: str) -> Any:  # noqa: ANN401
    """Return the object `path` names, as `module.attribute`.

    Imports the module on the first call and returns the cached object after.
    """
    module, _, name = path.rpartition(".")
    return getattr(import_module(module), name)


def _could_match(item: object, ref: _KindRef) -> bool:
    """Return whether `item` may satisfy the Protocol `ref` names.

    An object lacking one of the members cannot satisfy it structurally, and
    cannot be a nominal subclass while the Protocol's module is not loaded,
    so that module stays unloaded.
    """
    if ref.protocol.rpartition(".")[0] in sys.modules:
        return True
    for member in ref.members:
        try:
            getattr_static(item, member)
        except AttributeError:
            return False
    return True


def backend_kinds_of(
    item: object, *, wrappable: bool = False
) -> list[BackendKind]:
    """Return every first-party backend kind `item` satisfies.

    Loads the Protocol of a kind only when `item` has every member it
    matches on, so an object of no kind loads none of them.

    Args:
        item: The object to classify.
        wrappable: Only test the kinds `uses=[...]` wraps in a Component.
    """
    kinds = []
    for ref in _KINDS:
        if wrappable and ref.factory is None:
            continue
        if not _could_match(item, ref):
            continue
        protocol = load(ref.protocol)
        if isinstance(item, protocol):
            kinds.append(BackendKind(protocol, ref.label, ref.factory))
    return kinds


def _protocol_members(protocol: type) -> frozenset[str]:
    """Return the member names a runtime-checkable Protocol matches on.

    `isinstance` against a `runtime_checkable` Protocol tests exactly these
    names and never checks signatures, so they are also what decides which of
    two matching protocols is the more specific.
    """
    return frozenset(getattr(protocol, "__protocol_attrs__", ()))


def most_specific_backend[K: BackendKind](
    matches: Sequence[K], item: object
) -> K:
    """Return the match whose protocol subsumes every other match.

    A backend can satisfy more than one protocol, because `runtime_checkable`
    compares member names only. `CircuitBreakerBackend` declares everything
    `RateLimiterBackend` does plus `_loop` and `is_shared`, so every circuit
    breaker backend also matches `RateLimiterBackend`. The more specific
    protocol wins, which keeps the answer independent of the order the
    protocols are tested in.

    Raises:
        AmbiguousBackendError: If no single protocol subsumes the others, so
            the backend names two unrelated kinds and only the caller knows
            which was meant.
    """
    from grelmicro._app import AmbiguousBackendError  # noqa: PLC0415

    for candidate in matches:
        members = _protocol_members(candidate.protocol)
        if all(
            members >= _protocol_members(other.protocol)
            for other in matches
            if other.protocol is not candidate.protocol
        ):
            return candidate
    names = ", ".join(sorted(match.protocol.__name__ for match in matches))
    kinds = ", ".join(sorted(match.label for match in matches))
    msg = (
        f"{type(item).__name__} matches more than one backend protocol "
        f"({names}), so grelmicro cannot tell which kind it is. Wrap it in "
        f"the component you mean, one of: {kinds}."
    )
    raise AmbiguousBackendError(msg)


def resolve_source(
    source: object,
    *,
    owner: str,
    expects: str,
    protocols: Sequence[type],
) -> object:
    """Return `source` as a Provider or a backend of the owner's kind.

    A zero-argument class is instantiated first. Checked once, at
    construction, so nothing is paid per call.

    Args:
        source: What the Component was given as its first argument.
        owner: The Component's name, as the error shows it.
        expects: The backend it takes, as the error shows it, such as
            `a CacheBackend`.
        protocols: The backend Protocols a backend of its kind satisfies.

    Raises:
        TypeError: If `source` is neither a Provider nor a backend matching
            one of `protocols`, naming the Component it belongs to when it
            is a backend of another kind.
    """
    from grelmicro._app import AmbiguousBackendError  # noqa: PLC0415

    resolved = instantiate_if_class(source)
    if isinstance(resolved, Provider) or any(
        isinstance(resolved, protocol) for protocol in protocols
    ):
        return resolved
    got = type(resolved).__name__
    msg = f"{owner} expects a Provider or {expects}, got {got}."
    matches = backend_kinds_of(resolved)
    if not matches:
        raise TypeError(msg)
    try:
        home = most_specific_backend(matches, resolved)
    except AmbiguousBackendError:
        raise TypeError(msg) from None
    msg = (
        f"{owner} expects a Provider or {expects}, got {got}, which is a "
        f"{home.protocol.__name__}. Pass it to {home.label} instead."
    )
    raise TypeError(msg)
