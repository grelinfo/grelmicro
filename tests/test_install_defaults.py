"""What `micro.install(app)` is allowed to change in someone else's app.

grelmicro installs into a framework the user chose. Wiring the lifecycle and
binding the app per request is what `install` is for. Changing how the
framework answers a request is not, and every capability that does it is
bought by registering a component.

These tests hold that line. They compare a framework app before and after
`install`, so a capability added later that quietly changes a default fails
here rather than in someone's service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from litestar import Litestar, get
from litestar.testing import TestClient as LitestarTestClient
from starlette.status import (
    HTTP_200_OK,
    HTTP_500_INTERNAL_SERVER_ERROR,
)

from grelmicro import Grelmicro
from grelmicro.integrations import fastapi as integration
from grelmicro.integrations.fastapi import GrelmicroMiddleware

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = [pytest.mark.timeout(5)]


class BoomError(Exception):
    """An error the app raises, which grelmicro did not."""


def _fastapi_app() -> FastAPI:
    """Build an app with one working route and one that raises."""
    app = FastAPI()

    @app.get("/ok")
    async def ok() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/boom")
    async def boom() -> dict[str, bool]:
        raise BoomError

    return app


def test_install_adds_only_the_binding_middleware() -> None:
    """The ambient binding is the one frame `install` adds to the stack."""
    # Arrange
    app = _fastapi_app()
    before = list(app.user_middleware)

    # Act
    Grelmicro(uses=[]).install(app)

    # Assert
    added = [m for m in app.user_middleware if m not in before]
    # Only grelmicro's own frames are this test's business. A global
    # OpenTelemetry instrumentor enabled by another test patches every app
    # built afterwards, and that is not something `install` did.
    mine = [m.cls for m in added if m.cls.__module__.startswith("grelmicro")]
    assert mine == [GrelmicroMiddleware]


def test_install_registers_no_exception_handler() -> None:
    """An error grelmicro did not raise is the framework's to answer."""
    # Arrange
    app = _fastapi_app()
    before = dict(app.exception_handlers)

    # Act
    Grelmicro(uses=[]).install(app)

    # Assert
    assert app.exception_handlers == before


def test_install_adds_no_route() -> None:
    """Health and metrics endpoints are routers the user includes."""
    # Arrange
    app = _fastapi_app()
    before = [getattr(route, "path", None) for route in app.routes]

    # Act
    Grelmicro(uses=[]).install(app)

    # Assert
    assert [getattr(route, "path", None) for route in app.routes] == before


def test_the_binding_changes_nothing_a_client_can_see() -> None:
    """It sets a context variable. It does not touch the response."""
    # Arrange
    plain = _fastapi_app()
    wired = _fastapi_app()
    Grelmicro(uses=[]).install(wired)

    # Act
    with (
        TestClient(plain, raise_server_exceptions=False) as bare,
        TestClient(wired, raise_server_exceptions=False) as installed,
    ):
        before = bare.get("/ok")
        after = installed.get("/ok")
        before_error = bare.get("/boom")
        after_error = installed.get("/boom")

    # Assert
    assert after.status_code == before.status_code == HTTP_200_OK
    assert after.content == before.content
    assert after_error.status_code == before_error.status_code
    assert after_error.status_code == HTTP_500_INTERNAL_SERVER_ERROR
    assert (
        after_error.headers["content-type"]
        == before_error.headers["content-type"]
    )


def test_litestar_install_registers_no_exception_handler() -> None:
    """The same line holds on Litestar, which registers handlers differently."""

    # Arrange
    @get("/boom")
    async def boom() -> dict[str, bool]:
        raise BoomError

    app = Litestar(route_handlers=[boom])
    before = dict(app.exception_handlers)

    # Act
    Grelmicro(uses=[]).install(app)

    # Assert
    assert app.exception_handlers == before

    with LitestarTestClient(app=app, raise_server_exceptions=False) as client:
        assert client.get("/boom").status_code == HTTP_500_INTERNAL_SERVER_ERROR


@pytest.mark.parametrize(
    "capability",
    [
        "install_problem_details",
    ],
)
def test_every_capability_is_component_gated(capability: str) -> None:
    """A capability an integration exposes is wired only when asked for.

    The list is the point. Adding an entry to an integration module without
    gating it on a registered component fails this test, which is the
    reminder that `install` does not get to change defaults.
    """
    # Arrange
    app = _fastapi_app()
    calls: list[object] = []

    def record(wired: object) -> None:
        calls.append(wired)

    original: Callable[..., None] = getattr(integration, capability)
    setattr(integration, capability, record)

    # Act
    try:
        Grelmicro(uses=[]).install(app)
    finally:
        setattr(integration, capability, original)

    # Assert
    assert calls == []
