"""The first-party backend kinds, and the Component each one belongs to.

Exposes:

- `backend_kinds`: every backend Protocol with the Component that takes it.
- `most_specific_backend`: pick one kind for a backend matching several.
- `resolve_source`: read a Component's first argument as a Provider or a
  backend of its own kind, and refuse anything else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NamedTuple

from grelmicro._component import instantiate_if_class
from grelmicro.providers._base import Provider

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from grelmicro._component import Component


class BackendKind(NamedTuple):
    """One backend Protocol and the Component that takes it."""

    protocol: type
    """Runtime-checkable Protocol a backend of this kind satisfies."""

    label: str
    """How an error names the Component, `Coordination(lock=...)`."""

    factory: Callable[[Any], Component] | None
    """Build the Component around a bare backend, `None` when `uses=[...]`
    does not wrap this kind.

    Takes `Any` because the protocol match is what proves the backend fits,
    and that proof is a runtime `isinstance` a type checker cannot follow.
    """


def backend_kinds() -> list[BackendKind]:
    """Return every first-party backend kind.

    Imports are lazy so unused submodules stay out of `import grelmicro`.
    The user importing `RedisCacheAdapter` already loads `grelmicro.cache`,
    so the lazy import here is a cache hit.
    """
    from grelmicro.cache._component import Cache  # noqa: PLC0415
    from grelmicro.cache._protocol import CacheBackend  # noqa: PLC0415
    from grelmicro.coordination._component import (  # noqa: PLC0415
        COORDINATION_BACKENDS,
        Coordination,
    )
    from grelmicro.outbox._protocol import OutboxBackend  # noqa: PLC0415
    from grelmicro.resilience._components import (  # noqa: PLC0415
        CircuitBreakerComponent,
        RateLimiterComponent,
    )
    from grelmicro.resilience._protocol import (  # noqa: PLC0415
        CircuitBreakerBackend,
        RateLimiterBackend,
    )

    return [
        BackendKind(CacheBackend, "Cache", Cache),
        BackendKind(
            CircuitBreakerBackend,
            "CircuitBreakerComponent",
            CircuitBreakerComponent,
        ),
        BackendKind(
            RateLimiterBackend, "RateLimiterComponent", RateLimiterComponent
        ),
        *[
            BackendKind(
                slot.protocol, f"Coordination({slot.keyword}=...)", Coordination
            )
            for slot in COORDINATION_BACKENDS
        ],
        BackendKind(OutboxBackend, "Outbox", None),
    ]


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
    matches = [
        kind for kind in backend_kinds() if isinstance(resolved, kind.protocol)
    ]
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
