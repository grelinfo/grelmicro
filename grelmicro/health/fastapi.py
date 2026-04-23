"""FastAPI Health Check Router."""

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel
from typing_extensions import Doc

from grelmicro._json import json_dumps_bytes
from grelmicro.health._models import CheckResult, HealthReport, HealthStatus
from grelmicro.health._registry import HealthRegistry

if TYPE_CHECKING:
    from fastapi import APIRouter, Request
    from fastapi.params import Depends


_NO_STORE_HEADERS = {"Cache-Control": "no-store"}


class CheckResultResponse(BaseModel):
    """Health status of a single check."""

    status: HealthStatus
    critical: bool = True
    error: str | None = None
    # ``Any`` here: Pydantic can't schema-ify the recursive
    # ``JSONEncodable`` alias. Strict typing lives on ``HealthCheckFunc``.
    details: dict[str, Any] | None = None


class HealthzResponse(BaseModel):
    """Aggregate health report."""

    status: HealthStatus
    checks: dict[str, CheckResultResponse]


def health_router(
    registry: Annotated[
        HealthRegistry | None,
        Doc(
            "Health registry whose checks the router runs. When "
            "omitted, the router resolves the global registry "
            "(the most recent ``HealthRegistry`` created with "
            "``auto_register=True``)."
        ),
    ] = None,
    *,
    prefix: Annotated[
        str,
        Doc("URL prefix for health endpoints (e.g. '/api/v1')."),
    ] = "",
    show_details: Annotated[
        "bool | list[Depends]",
        Doc(
            "Whether ``/healthz`` includes each check's verbose "
            "``details`` field (versions, hostnames, pool stats, ...):\n\n"
            "- ``False`` (default): details are stripped. Safe for "
            "public endpoints.\n"
            "- ``True``: details are always included. Use only if "
            "``/healthz`` is private.\n"
            "- ``list[Depends]``: details are included only when "
            "every listed dependency passes (returns without "
            "raising). A failing dependency strips details; the "
            "endpoint still returns ``200``/``503`` with the base "
            "fields. Use this to expose verbose details to admins "
            "while keeping basic status public."
        ),
    ] = False,
    healthz_dependencies: Annotated[
        "list[Depends] | None",
        Doc(
            "FastAPI dependencies applied to ``/healthz``. A failing "
            "dependency blocks the entire endpoint (``401``/``403``). "
            "Use to hide ``/healthz`` from the public while leaving "
            "``/livez`` and ``/readyz`` open to orchestrators and "
            "load balancers. Independent of ``show_details``."
        ),
    ] = None,
) -> "APIRouter":
    """Create a FastAPI router with health check endpoints.

    Provides three endpoints:

    - ``GET/HEAD {prefix}/livez``: Liveness probe. Never runs
      checkers. Always returns ``200`` with an empty body.
    - ``GET/HEAD {prefix}/readyz``: Readiness probe. Runs critical
      checkers only. Returns ``200`` or ``503`` with an empty body.
    - ``GET/HEAD {prefix}/healthz``: Aggregate JSON report.

    All responses set ``Cache-Control: no-store``.

    Raises:
        DependencyNotFoundError: If ``fastapi`` is not installed.
    """
    try:
        from fastapi import APIRouter as _APIRouter  # noqa: PLC0415
        from fastapi import Query, Request  # noqa: PLC0415
        from fastapi.responses import Response  # noqa: PLC0415
        from starlette.status import (  # noqa: PLC0415
            HTTP_200_OK,
            HTTP_503_SERVICE_UNAVAILABLE,
        )
    except ImportError:
        from grelmicro.errors import (  # noqa: PLC0415
            DependencyNotFoundError,
        )

        raise DependencyNotFoundError(module="fastapi")  # noqa: B904

    from grelmicro.health._backends import (  # noqa: PLC0415
        get_health_registry,
    )

    # Normalize ``show_details`` once. ``always`` -> show unconditionally,
    # ``never`` -> strip, ``guarded`` -> include only when every dep passes.
    if show_details is True:
        details_mode = "always"
        details_deps: list[Depends] = []
    elif isinstance(show_details, list) and show_details:
        details_mode = "guarded"
        details_deps = list(show_details)
    else:  # False, None, or empty list
        details_mode = "never"
        details_deps = []

    router = _APIRouter(prefix=prefix, tags=["health"])
    healthz_deps = list(healthz_dependencies or ())
    # Registry is resolved lazily per request so the router can be built
    # before the registry exists (FastAPI app-factory + lifespan pattern).
    # ``BackendRegistry.get`` is a single attribute check: ~100ns.

    @router.get("/livez", status_code=HTTP_200_OK)
    @router.head("/livez", include_in_schema=False)
    async def livez() -> Response:
        """Liveness probe. Always returns ``200`` with an empty body."""
        return Response(status_code=HTTP_200_OK, headers=_NO_STORE_HEADERS)

    @router.get(
        "/readyz",
        status_code=HTTP_200_OK,
        responses={
            HTTP_503_SERVICE_UNAVAILABLE: {
                "description": (
                    "At least one critical component is unhealthy."
                ),
            },
        },
    )
    @router.head("/readyz", include_in_schema=False)
    async def readyz(
        exclude: Annotated[
            str | None,
            Query(
                description="Comma-separated list of checker names to skip.",
            ),
        ] = None,
    ) -> Response:
        """Readiness probe. Runs critical checkers only."""
        report = await (registry or get_health_registry()).run(
            critical_only=True,
            exclude=_parse_exclude(exclude),
        )
        status_code = (
            HTTP_200_OK
            if report["status"] == HealthStatus.OK
            else HTTP_503_SERVICE_UNAVAILABLE
        )
        return Response(status_code=status_code, headers=_NO_STORE_HEADERS)

    @router.get(
        "/healthz",
        response_model=HealthzResponse,
        responses={
            HTTP_503_SERVICE_UNAVAILABLE: {
                "model": HealthzResponse,
                "description": "At least one critical component is unhealthy.",
            },
        },
        dependencies=healthz_deps,
    )
    @router.head("/healthz", include_in_schema=False, dependencies=healthz_deps)
    async def healthz(
        request: Request,
        exclude: Annotated[
            str | None,
            Query(
                description="Comma-separated list of checker names to skip.",
            ),
        ] = None,
    ) -> Response:
        """Aggregate JSON report of all checker results."""
        report = await (registry or get_health_registry()).run(
            critical_only=False,
            exclude=_parse_exclude(exclude),
        )
        if details_mode == "always":
            include_details = True
        elif details_mode == "guarded":
            include_details = await _all_deps_pass(request, details_deps)
        else:
            include_details = False
        body = _render_report(report, include_details=include_details)
        status_code = (
            HTTP_200_OK
            if report["status"] == HealthStatus.OK
            else HTTP_503_SERVICE_UNAVAILABLE
        )
        return Response(
            content=json_dumps_bytes(body),
            status_code=status_code,
            media_type="application/json",
            headers=_NO_STORE_HEADERS,
        )

    return router


def _parse_exclude(raw: str | None) -> set[str]:
    """Split a comma-separated exclude list into a set of names."""
    if not raw:
        return set()
    return {name.strip() for name in raw.split(",") if name.strip()}


async def _all_deps_pass(request: "Request", deps: "list[Depends]") -> bool:
    """Return True if every dep runs without raising."""
    import inspect  # noqa: PLC0415

    from fastapi import HTTPException  # noqa: PLC0415

    for dep in deps:
        call = dep.dependency
        if call is None:
            continue
        try:
            sig = inspect.signature(call)
            kwargs: dict[str, Any] = {}
            for param_name, param in sig.parameters.items():
                if param_name == "request" or _is_request_type(
                    param.annotation
                ):
                    kwargs[param_name] = request
            result = call(**kwargs)
            if inspect.iscoroutine(result):
                await result
        except HTTPException:
            return False
        except Exception:  # noqa: BLE001
            return False
    return True


def _is_request_type(annotation: Any) -> bool:  # noqa: ANN401
    """Check whether the annotation resolves to ``fastapi.Request``."""
    from fastapi import Request  # noqa: PLC0415

    return annotation is Request


def _render_report(
    report: HealthReport, *, include_details: bool
) -> dict[str, Any]:
    """Shape the report for JSON serialization."""
    return {
        "status": report["status"],
        "checks": {
            name: _render_check(result, include_details=include_details)
            for name, result in report["checks"].items()
        },
    }


def _render_check(
    result: CheckResult, *, include_details: bool
) -> dict[str, Any]:
    """Shape a single check result for JSON serialization."""
    rendered: dict[str, Any] = {
        "status": result["status"],
        "critical": result["critical"],
        "error": result["error"],
    }
    if include_details:
        rendered["details"] = result["details"]
    return rendered
