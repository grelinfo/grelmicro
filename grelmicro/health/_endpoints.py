"""Health endpoints, served without a web framework.

One implementation of what `/livez`, `/readyz` and `/healthz` answer: the
status code, the headers, and the bytes. The FastAPI router, the ASGI app,
and `OpsServer` all render through here, so the three doors cannot drift
from one another.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Final

from typing_extensions import Doc

from grelmicro._endpoints import (
    HTTP_OK,
    HTTP_SERVICE_UNAVAILABLE,
    Rendered,
    build_asgi,
    query_value,
)
from grelmicro._json import json_dumps_bytes
from grelmicro.health._models import HealthStatus

if TYPE_CHECKING:
    from collections.abc import Mapping, MutableMapping

    from grelmicro._endpoints import ASGIApp, Handler
    from grelmicro.health._checks import HealthChecks
    from grelmicro.health._models import CheckResult

    Scope = MutableMapping[str, Any]

__all__ = ["health_asgi"]

JSON_MEDIA_TYPE: Final = "application/json"


def parse_exclude(raw: str | None) -> frozenset[str]:
    """Split a comma-separated exclude list into a frozenset of names.

    `frozenset` so the component's `run(exclude=...)` can adopt it without
    copying (CPython short-circuits `frozenset(frozenset)` to the same
    object).
    """
    if not raw:
        return frozenset()
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def status_code(status: HealthStatus) -> int:
    """Map an aggregate status to the code every door answers with."""
    return HTTP_OK if status == HealthStatus.OK else HTTP_SERVICE_UNAVAILABLE


def check_body(result: CheckResult, *, details: bool) -> dict[str, Any]:
    """Build one `/healthz` check entry, leaving out what carries no value.

    A passing check reports `status` and `critical` alone. `error` is added
    only when the check failed, and `details` only when the check returned
    some and the door is showing them.
    """
    body: dict[str, Any] = {
        "status": result["status"],
        "critical": result["critical"],
    }
    error = result["error"]
    if error is not None:
        body["error"] = error
    if details:
        check_details = result["details"]
        if check_details is not None:
            body["details"] = check_details
    return body


def report_body(
    checks: Mapping[str, CheckResult],
    status: HealthStatus,
    *,
    details: bool,
) -> bytes:
    """Serialize the aggregate report `/healthz` answers with."""
    body: Any = {
        "status": status,
        "checks": {
            name: check_body(result, details=details)
            for name, result in checks.items()
        },
    }
    return json_dumps_bytes(body)


def health_routes(
    component: HealthChecks | None = None,
    *,
    prefix: str = "",
    show_details: bool = False,
) -> dict[str, Handler]:
    """Build the three health handlers, keyed by the path each answers."""

    def resolve() -> HealthChecks:
        if component is not None:
            return component
        from grelmicro._app import Grelmicro  # noqa: PLC0415

        return Grelmicro.current().get("health", "default")

    def excluded(scope: Scope) -> frozenset[str]:
        return parse_exclude(query_value(scope, "exclude"))

    async def livez(_scope: Scope) -> Rendered:
        """Answer that the process is alive, without running a check."""
        return Rendered(HTTP_OK, b"")

    async def readyz(scope: Scope) -> Rendered:
        """Run the critical checks and answer with the code alone."""
        report = await resolve().run(
            critical_only=True, exclude=excluded(scope)
        )
        return Rendered(status_code(report["status"]), b"")

    async def healthz(scope: Scope) -> Rendered:
        """Run every check and answer with the aggregate JSON report."""
        report = await resolve().run(
            critical_only=False, exclude=excluded(scope)
        )
        return Rendered(
            status_code(report["status"]),
            report_body(
                report["checks"], report["status"], details=show_details
            ),
            JSON_MEDIA_TYPE,
        )

    return {
        f"{prefix}/livez": livez,
        f"{prefix}/readyz": readyz,
        f"{prefix}/healthz": healthz,
    }


def health_asgi(
    component: Annotated[
        HealthChecks | None,
        Doc(
            "Health checks component whose checks the app runs. When "
            "omitted, it resolves the default instance from the active "
            "`Grelmicro` app (``Grelmicro(uses=[HealthChecks(...)])``)."
        ),
    ] = None,
    *,
    prefix: Annotated[
        str,
        Doc(
            "URL prefix for the three paths. Leave it empty when the app is "
            "mounted, because the mount strips its own path first."
        ),
    ] = "",
    show_details: Annotated[
        bool,
        Doc(
            "Whether ``/healthz`` includes each check's verbose ``details`` "
            "field (versions, hostnames, pool stats, ...). ``False`` (the "
            "default) strips them, which is safe for an endpoint anyone can "
            "reach."
        ),
    ] = False,
) -> ASGIApp:
    """Create a pure-ASGI app serving the health endpoints.

    The three endpoints
    [`health_router`][grelmicro.integrations.fastapi.health_router] serves,
    rendered by the same code, with no framework anywhere:

    - ``GET/HEAD {prefix}/livez``: Liveness probe. Never runs checks.
      Always ``200`` with an empty body.
    - ``GET/HEAD {prefix}/readyz``: Readiness probe. Runs critical checks
      only. ``200`` or ``503`` with an empty body.
    - ``GET/HEAD {prefix}/healthz``: Aggregate JSON report.

    Both probe paths accept ``?exclude=name,name``, and every response sets
    ``Cache-Control: no-store``. Any other path answers ``404``, and any
    other method ``405``.

    Mount it in an ASGI framework:

    ```python
    from starlette.applications import Starlette
    from starlette.routing import Mount

    from grelmicro.health import health_asgi

    app = Starlette(routes=[Mount("", app=health_asgi())])
    ```

    Or give it a port of its own with
    [`OpsServer`][grelmicro.http.OpsServer], for a worker that runs no web
    framework at all.

    On FastAPI, prefer `health_router()`: it serves the same endpoints and
    adds the OpenAPI schema, the `Depends` gate on ``/healthz``, and the
    dependency form of ``show_details``.
    """
    return build_asgi(
        health_routes(component, prefix=prefix, show_details=show_details)
    )
