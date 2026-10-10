"""A registered `HealthChecks` that nothing serves is reported: `health-not-served`.

`micro.install(app)` mounts no health route, so a forgotten
`health_router()`, `health_asgi()` or `OpsServer` leaves probes on `404`.
"""

from __future__ import annotations

import gc
import warnings
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from litestar import Litestar, asgi
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.routing import Mount

from grelmicro import Grelmicro, HealthNotServedWarning
from grelmicro._config import flush_ignored_env_reports
from grelmicro.health import HealthChecks, health_asgi
from grelmicro.health._served import (
    HealthEndpoint,
    health_endpoint_in,
    unserved,
)
from grelmicro.http import OpsServer
from grelmicro.integrations.fastapi import health_router
from tests._logs import records_of

if TYPE_CHECKING:
    from grelmicro.types import Environment

pytestmark = [pytest.mark.timeout(5)]


async def _open(micro: Grelmicro) -> None:
    async with micro:
        pass


async def _open_quietly(micro: Grelmicro) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        await _open(micro)


def _row_status(micro: Grelmicro, app: object) -> str | None:
    """Return the `health-not-served` row status of `describe(app)`."""
    report = micro.describe(app)
    return next(
        (c.status for c in report.checks if c.name == "health-not-served"),
        None,
    )


async def test_health_not_served_bare_fastapi_warns_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An HTTP app with no health route warns and logs once the app opens."""
    # Arrange
    micro = Grelmicro(uses=[HealthChecks()], environment="production")
    micro.install(FastAPI())

    # Act
    with pytest.warns(HealthNotServedWarning, match="OpsServer"):
        await _open(micro)
    flush_ignored_env_reports()

    # Assert
    assert any(
        getattr(r, "diagnostic", None) == "health-not-served"
        for r in records_of(caplog, "grelmicro")
    )


async def test_health_not_served_undeclared_environment_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no environment declared, the gap is reported too."""
    # Arrange
    monkeypatch.delenv("GREL_ENVIRONMENT")
    micro = Grelmicro(uses=[HealthChecks()])
    micro.install(FastAPI())

    # Act / Assert
    with pytest.warns(HealthNotServedWarning):
        await _open(micro)


@pytest.mark.parametrize("environment", ["development", "test"])
async def test_health_not_served_quiet_tier_stays_quiet(
    environment: Environment,
) -> None:
    """`development` and `test` report nothing."""
    # Arrange
    micro = Grelmicro(uses=[HealthChecks()], environment=environment)
    micro.install(FastAPI())

    # Act / Assert
    await _open_quietly(micro)


async def test_health_not_served_included_router_stays_quiet() -> None:
    """An included `health_router()` serves the default checks."""
    # Arrange
    app = FastAPI()
    app.include_router(health_router(), prefix="/ops")
    micro = Grelmicro(uses=[HealthChecks()], environment="production")
    micro.install(app)

    # Act / Assert
    await _open_quietly(micro)


async def test_health_not_served_router_in_mounted_sub_app_stays_quiet() -> (
    None
):
    """A `health_router()` inside a mounted FastAPI sub-app counts."""
    # Arrange
    sub = FastAPI()
    sub.include_router(health_router())
    app = FastAPI()
    app.mount("/v1", sub)
    micro = Grelmicro(uses=[HealthChecks()], environment="production")
    micro.install(app)

    # Act / Assert
    await _open_quietly(micro)


async def test_health_not_served_starlette_mount_behind_middleware_stays_quiet() -> (
    None
):
    """A mounted `health_asgi()` counts, even wrapped in middleware."""
    # Arrange
    app = Starlette(
        routes=[
            Mount(
                "/ops",
                app=health_asgi(),
                middleware=[Middleware(GZipMiddleware)],
            )
        ]
    )
    micro = Grelmicro(uses=[HealthChecks()], environment="production")
    micro.install(app)

    # Act / Assert
    await _open_quietly(micro)


async def test_health_not_served_litestar_asgi_mount_stays_quiet() -> None:
    """A Litestar ASGI mount of `health_asgi()` counts."""
    # Arrange
    app = Litestar(
        route_handlers=[
            asgi("/ops", is_mount=True, copy_scope=True)(health_asgi())
        ]
    )
    micro = Grelmicro(uses=[HealthChecks()], environment="production")
    micro.install(app)

    # Act / Assert
    await _open_quietly(micro)


async def test_health_not_served_no_http_app_stays_quiet() -> None:
    """A worker with no installed HTTP app reports nothing."""
    # Arrange
    micro = Grelmicro(uses=[HealthChecks()], environment="production")

    # Act / Assert
    await _open_quietly(micro)


async def test_health_not_served_named_checks_beside_default_door_warns() -> (
    None
):
    """A door with no argument serves the default checks, not a named one."""
    # Arrange
    app = FastAPI()
    app.include_router(health_router())
    micro = Grelmicro(
        uses=[HealthChecks(), HealthChecks(name="internal")],
        environment="production",
    )
    micro.install(app)

    # Act / Assert
    with pytest.warns(HealthNotServedWarning, match="'internal'"):
        await _open(micro)


async def test_health_not_served_named_checks_with_own_door_stays_quiet() -> (
    None
):
    """A door built for a named instance serves it."""
    # Arrange
    internal = HealthChecks(name="internal")
    app = FastAPI()
    app.include_router(health_router(internal))
    micro = Grelmicro(uses=[internal], environment="production")
    micro.install(app)

    # Act / Assert
    await _open_quietly(micro)


def test_health_not_served_describe_row_warns() -> None:
    """`describe(app)` carries the check as a row."""
    # Arrange
    app = FastAPI()
    micro = Grelmicro(uses=[HealthChecks()])
    micro.install(app)

    # Act / Assert
    assert _row_status(micro, app) == "warn"


def test_health_not_served_ops_server_serves_the_default() -> None:
    """A registered `OpsServer` serves the default checks."""
    # Arrange
    app = FastAPI()
    micro = Grelmicro(uses=[HealthChecks(), OpsServer(port=9464)])
    micro.install(app)

    # Act / Assert
    assert _row_status(micro, app) == "ok"


def test_health_not_served_ops_server_skips_a_sole_named_instance() -> None:
    """`OpsServer` serves only the instance named `"default"`."""
    # Arrange
    app = FastAPI()
    micro = Grelmicro(
        uses=[HealthChecks(name="internal"), OpsServer(port=9464)]
    )
    micro.install(app)

    # Act
    report = micro.describe(app)

    # Assert
    row = next(c for c in report.checks if c.name == "health-not-served")
    assert row.status == "warn"
    assert "OpsServer" not in row.detail
    assert "health_router()" in row.detail


def test_health_not_served_asgi_faststream_route_serves() -> None:
    """An `AsgiFastStream` route to `health_asgi()` serves the default checks."""
    # Arrange
    from faststream.asgi import AsgiFastStream  # noqa: PLC0415
    from faststream.redis import RedisBroker  # noqa: PLC0415

    app = AsgiFastStream(RedisBroker(), asgi_routes=[("/ops", health_asgi())])
    micro = Grelmicro(uses=[HealthChecks()])
    micro.install(app)

    # Act / Assert
    assert _row_status(micro, app) == "ok"


def test_health_not_served_plain_faststream_is_not_checked() -> None:
    """A FastStream app with no HTTP is not checked."""
    # Arrange
    from faststream import FastStream  # noqa: PLC0415
    from faststream.redis import RedisBroker  # noqa: PLC0415

    app = FastStream(RedisBroker())
    micro = Grelmicro(uses=[HealthChecks()])
    micro.install(app)

    # Act / Assert
    assert _row_status(micro, app) is None


def test_health_not_served_collected_app_is_not_checked() -> None:
    """An installed app that is gone is no longer read."""
    # Arrange
    micro = Grelmicro(uses=[HealthChecks()])
    micro.install(FastAPI())
    gc.collect()

    # Act / Assert
    assert _row_status(micro, None) is None


class _Loop:
    """An app whose `.app` points back at itself, as a broken wrapper might."""

    def __init__(self) -> None:
        self.app = self


def test_health_endpoint_in_self_wrapping_app_gives_up() -> None:
    """Unwrapping stops after a bounded number of layers."""
    # Act / Assert
    assert health_endpoint_in(_Loop()) is None


def test_unserved_default_door_without_default_serves_nothing() -> None:
    """An endpoint built with no argument serves nothing when no default exists."""
    # Arrange
    internal = object()

    # Act
    missing = unserved(
        [internal], None, [HealthEndpoint(None)], ops_server=None
    )

    # Assert
    assert missing == [internal]
