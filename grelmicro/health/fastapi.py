"""FastAPI Health Check Router."""

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel
from typing_extensions import Doc

from grelmicro._json import json_dumps_bytes
from grelmicro.health._models import CheckResult, HealthReport, HealthStatus
from grelmicro.health._registry import HealthRegistry

if TYPE_CHECKING:
    from fastapi import APIRouter, Request
    from fastapi.params import Depends


ShowDetails = Literal["never", "always", "when-authorized"]

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
        ShowDetails,
        Doc(
            "Visibility of the per-checker ``details`` field on "
            "``/healthz``. ``'never'`` strips details. ``'always'`` "
            "includes them. ``'when-authorized'`` includes them only "
            "when ``details_dependencies`` pass. Per-request override "
            "via ``?details=true|false``."
        ),
    ] = "never",
    healthz_dependencies: Annotated[
        "list[Depends] | None",
        Doc(
            "FastAPI dependencies applied to ``/healthz`` only. "
            "Use to auth-gate the aggregate endpoint while leaving "
            "``/livez`` and ``/readyz`` open to orchestrators and "
            "load balancers."
        ),
    ] = None,
    details_dependencies: Annotated[
        "list[Depends] | None",
        Doc(
            "Dependencies that determine whether details are "
            "included on ``/healthz`` when ``show_details`` is "
            "``'when-authorized'``. Failing dependencies strip "
            "details but do not block the endpoint."
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

    router = _APIRouter(prefix=prefix, tags=["health"])
    details_deps = list(details_dependencies or ())
    healthz_deps = list(healthz_dependencies or ())

    def _resolve_registry() -> HealthRegistry:
        return registry if registry is not None else get_health_registry()

    def _empty_probe(status_code: int) -> Response:
        return Response(status_code=status_code, headers=_NO_STORE_HEADERS)

    @router.get("/livez", status_code=HTTP_200_OK)
    @router.head("/livez", include_in_schema=False)
    async def livez() -> Response:
        """Liveness probe. Always returns ``200`` with an empty body."""
        return _empty_probe(HTTP_200_OK)

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
        report = await _resolve_registry().run(
            critical_only=True,
            exclude=_parse_exclude(exclude),
        )
        status_code = (
            HTTP_200_OK
            if report["status"] == HealthStatus.OK
            else HTTP_503_SERVICE_UNAVAILABLE
        )
        return _empty_probe(status_code)

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
        details: Annotated[
            bool | None,
            Query(
                description="Include per-checker details in the response.",
            ),
        ] = None,
        exclude: Annotated[
            str | None,
            Query(
                description="Comma-separated list of checker names to skip.",
            ),
        ] = None,
    ) -> Response:
        """Aggregate JSON report of all checker results."""
        report = await _resolve_registry().run(
            critical_only=False,
            exclude=_parse_exclude(exclude),
        )
        include_details = await _resolve_details(
            details=details,
            show_details=show_details,
            request=request,
            deps=details_deps,
        )
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


async def _resolve_details(
    *,
    details: bool | None,
    show_details: ShowDetails,
    request: "Request",
    deps: "list[Depends]",
) -> bool:
    """Decide whether to include per-checker details."""
    if show_details == "always":
        return details if details is not None else True
    if show_details == "never":
        return details if details is not None else False
    # when-authorized
    if details is False:
        return False
    return await _run_details_deps(request, deps)


async def _run_details_deps(request: "Request", deps: "list[Depends]") -> bool:
    """Run each details dependency and return whether all pass."""
    import inspect  # noqa: PLC0415

    from fastapi import HTTPException  # noqa: PLC0415

    if not deps:
        return False
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
