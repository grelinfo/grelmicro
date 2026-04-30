"""FastAPI Health Check Router."""

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel
from typing_extensions import Doc

from grelmicro._json import json_dumps_bytes
from grelmicro.health._models import HealthStatus
from grelmicro.health._registry import HealthRegistry

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import APIRouter
    from fastapi.params import Depends


_NO_STORE_HEADERS = {"Cache-Control": "no-store"}


def _always_true() -> bool:
    return True


def _always_false() -> bool:
    return False


class CheckResultResponse(BaseModel):
    """Health status of a single check."""

    status: HealthStatus
    critical: bool = True
    error: str | None = None
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
            "(the registry installed via ``health.use_registry`` "
            "or entered as an async context manager)."
        ),
    ] = None,
    *,
    prefix: Annotated[
        str,
        Doc("URL prefix for health endpoints (e.g. '/api/v1')."),
    ] = "",
    show_details: Annotated[
        "bool | Depends",
        Doc(
            "Whether ``/healthz`` includes each check's verbose "
            "``details`` field (versions, hostnames, pool stats, ...):\n\n"
            "- ``False`` (default): details are stripped. Safe for "
            "public endpoints.\n"
            "- ``True``: details are always included. Use only if "
            "``/healthz`` is private.\n"
            "- ``Depends(fn)`` where ``fn`` returns ``bool``: wires "
            "``fn`` into FastAPI's DI graph, so ``Depends`` chains, "
            "``yield`` cleanup, ``Security``, ``Request`` injection, "
            "and async all work naturally. Return ``True`` to show "
            "details, ``False`` to strip them. Raising "
            "``HTTPException`` blocks the endpoint, so return "
            "``False`` instead when you want a soft strip."
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
        TypeError: If ``show_details`` is neither a bool nor a
            ``Depends(...)`` value.
    """
    try:
        from fastapi import APIRouter as _APIRouter  # noqa: PLC0415
        from fastapi import Depends, Query  # noqa: PLC0415
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

    show_details_dep = _resolve_show_details_dep(show_details)

    router = _APIRouter(prefix=prefix, tags=["health"])
    healthz_deps = list(healthz_dependencies or ())

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
        include_details: Annotated[bool, Depends(show_details_dep)],
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
        body: Any = (
            report
            if include_details
            else {
                "status": report["status"],
                "checks": {
                    name: {
                        "status": r["status"],
                        "critical": r["critical"],
                        "error": r["error"],
                    }
                    for name, r in report["checks"].items()
                },
            }
        )
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


def _resolve_show_details_dep(show_details: Any) -> "Callable[..., Any]":  # noqa: ANN401
    """Return the FastAPI dependency callable for ``show_details``.

    Booleans collapse to shared constant-returning helpers (identity
    stable across router builds, so FastAPI's DI can reuse them).
    ``Depends(fn)`` yields the underlying ``fn`` so FastAPI wires it
    through its DI graph on the route.
    """
    from fastapi.params import Depends as _DependsParam  # noqa: PLC0415

    if show_details is True:
        return _always_true
    if show_details is False:
        return _always_false
    if isinstance(show_details, _DependsParam):
        if show_details.dependency is None:
            msg = "show_details=Depends(None) is not allowed"
            raise TypeError(msg)
        return show_details.dependency
    msg = (
        "show_details must be bool or Depends(fn) where fn returns "
        f"bool, got {type(show_details).__name__}"
    )
    raise TypeError(msg)


def _parse_exclude(raw: str | None) -> frozenset[str]:
    """Split a comma-separated exclude list into a frozenset of names.

    ``frozenset`` so the registry's ``run(exclude=...)`` can adopt it
    without copying (CPython short-circuits ``frozenset(frozenset)``
    to the same object).
    """
    if not raw:
        return frozenset()
    return frozenset(name.strip() for name in raw.split(",") if name.strip())
