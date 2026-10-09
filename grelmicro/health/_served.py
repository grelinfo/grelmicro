"""Find the health endpoints an app serves, and the checks nothing serves."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

_MARK: Final = "_grelmicro_health_endpoint"
"""The attribute a health endpoint carries, naming the checks it serves."""

_UNWRAP_LIMIT: Final = 32
"""How many `.app` layers of middleware an endpoint is unwrapped through."""


@dataclass(frozen=True, slots=True)
class HealthEndpoint:
    """A health endpoint, and the `HealthChecks` it serves.

    `component` is `None` for an endpoint that serves the app's default
    `HealthChecks`.
    """

    component: object | None


def mark_health_endpoint(endpoint: object, component: object | None) -> None:
    """Mark `endpoint` as serving `component`, `None` for the default."""
    setattr(endpoint, _MARK, HealthEndpoint(component))


def health_endpoint_in(target: object) -> HealthEndpoint | None:
    """Return the health endpoint `target` is, through middleware `.app` layers.

    Returns `None` when `target` serves no health.
    """
    for _ in range(_UNWRAP_LIMIT):
        if target is None:
            return None
        found = getattr(target, _MARK, None)
        if isinstance(found, HealthEndpoint):
            return found
        target = getattr(target, "app", None)
    return None


def health_endpoints_in(routes: Iterable[object]) -> Iterator[HealthEndpoint]:
    """Yield the health endpoints among Starlette or FastAPI `routes`.

    A route's endpoint, a mounted app and the routes under a mount or an
    included router are all read.
    """
    seen: set[int] = set()
    pending = list(routes)
    while pending:
        route = pending.pop()
        if id(route) in seen:
            continue
        seen.add(id(route))
        found = health_endpoint_in(
            getattr(route, "endpoint", None)
        ) or health_endpoint_in(getattr(route, "app", None))
        if found is not None:
            yield found
        pending.extend(getattr(route, "routes", None) or ())
        original = getattr(route, "original_router", None)
        if original is not None:
            pending.extend(getattr(original, "routes", None) or ())


def unserved(
    registered: Iterable[object],
    default: object | None,
    endpoints: Iterable[HealthEndpoint],
    *,
    ops_server: object | None,
) -> list[object]:
    """Return each of `registered` that no endpoint and no `OpsServer` serves.

    An endpoint built with no checks serves `default`. `ops_server` is the
    `HealthChecks` an `OpsServer` serves, or `None` without one.
    """
    served = {id(ops_server)} if ops_server is not None else set()
    for endpoint in endpoints:
        component = (
            default if endpoint.component is None else endpoint.component
        )
        if component is not None:
            served.add(id(component))
    return [checks for checks in registered if id(checks) not in served]
