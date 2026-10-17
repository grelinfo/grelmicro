"""A cache, idempotency or conditional middleware added by hand.

Added by hand, not through `micro.install(app)`, each one decides from its
own `include=` and `exclude=` patterns and the request headers alone, on
FastAPI, Starlette and Litestar. It never reads the app's routes: a named
read is cached, a request carrying a credential and a response setting a
cookie are not, a replay is scoped per caller, and a named write without a
precondition is refused `428`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import pytest
from fastapi import FastAPI
from litestar import Litestar, get, post, put
from litestar import Response as LitestarResponse
from litestar.datastructures import Cookie
from litestar.middleware import DefineMiddleware
from litestar.testing import TestClient as LitestarClient
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient as StarletteClient

from grelmicro import Grelmicro
from grelmicro.cache import TTLCache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.cache.serializers import JsonSerializer
from grelmicro.http import (
    CachedResponsesMiddleware,
    ConditionalRequestsMiddleware,
    IdempotencyMiddleware,
    IdempotentRequests,
)
from grelmicro.idempotency import Idempotency
from grelmicro.integrations import fastapi as on_fastapi
from grelmicro.integrations import litestar as on_litestar
from grelmicro.integrations import starlette as on_starlette
from grelmicro.providers.memory import MemoryProvider

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from starlette.requests import Request

pytestmark = [pytest.mark.timeout(10)]

OK = 200
UNAUTHORIZED = 401
PRECONDITION_REQUIRED = 428
HEADER = "Idempotency-Key"
REPLAY_HEADER = "Idempotent-Replayed"

Framework = Literal["fastapi", "starlette", "litestar"]
FRAMEWORKS: tuple[Framework, ...] = ("fastapi", "starlette", "litestar")

CREDENTIALS = [
    pytest.param({"Authorization": "Bearer secret"}, id="authorization"),
    pytest.param({"Cookie": "session=secret"}, id="cookie"),
]


class Calls:
    """Counts what each handler of an app served."""

    def __init__(self) -> None:
        """Start every count at zero."""
        self.counts: dict[str, int] = {}

    def served(self, name: str) -> dict[str, int]:
        """Count one call of `name`, and return what its handler answers."""
        self.counts[name] = self.counts.get(name, 0) + 1
        return {"calls": self.counts[name]}


def _fastapi(
    calls: Calls, middleware: list[tuple[type, dict[str, Any]]]
) -> Any:  # noqa: ANN401
    app = FastAPI(
        middleware=[Middleware(cls, **options) for cls, options in middleware]
    )

    @app.get("/feed")
    async def feed() -> dict[str, int]:
        return calls.served("feed")

    @app.get("/other")
    async def other() -> dict[str, int]:
        return calls.served("other")

    @app.get("/session")
    async def session() -> JSONResponse:
        response = JSONResponse(calls.served("session"))
        response.set_cookie("session", "abc")
        return response

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        return calls.served("charge")

    @app.put("/feed")
    async def replace() -> dict[str, int]:
        return calls.served("replace")

    @app.put("/other")
    async def replace_other() -> dict[str, int]:
        return calls.served("replace other")

    return app


def _starlette(
    calls: Calls, middleware: list[tuple[type, dict[str, Any]]]
) -> Any:  # noqa: ANN401
    def answer(name: str) -> Any:  # noqa: ANN401
        async def endpoint(_request: Request) -> JSONResponse:
            return JSONResponse(calls.served(name))

        return endpoint

    async def session(_request: Request) -> JSONResponse:
        response = JSONResponse(calls.served("session"))
        response.set_cookie("session", "abc")
        return response

    return Starlette(
        routes=[
            Route("/feed", answer("feed"), methods=["GET"]),
            Route("/feed", answer("replace"), methods=["PUT"]),
            Route("/other", answer("other"), methods=["GET"]),
            Route("/other", answer("replace other"), methods=["PUT"]),
            Route("/session", session, methods=["GET"]),
            Route("/charge", answer("charge"), methods=["POST"]),
        ],
        middleware=[Middleware(cls, **options) for cls, options in middleware],
    )


def _litestar(
    calls: Calls, middleware: list[tuple[type, dict[str, Any]]]
) -> Any:  # noqa: ANN401
    @get("/feed")
    async def feed() -> dict[str, int]:
        return calls.served("feed")

    @get("/other")
    async def other() -> dict[str, int]:
        return calls.served("other")

    @get("/session")
    async def session() -> LitestarResponse[dict[str, int]]:
        return LitestarResponse(
            calls.served("session"),
            cookies=[Cookie(key="session", value="abc")],
        )

    @post("/charge", status_code=OK)
    async def charge() -> dict[str, int]:
        return calls.served("charge")

    @put("/feed")
    async def replace() -> dict[str, int]:
        return calls.served("replace")

    @put("/other")
    async def replace_other() -> dict[str, int]:
        return calls.served("replace other")

    return Litestar(
        route_handlers=[feed, other, session, charge, replace, replace_other],
        middleware=[
            DefineMiddleware(cls, **options) for cls, options in middleware
        ],
    )


def built(
    framework: Framework,
    calls: Calls,
    *middleware: tuple[type, dict[str, Any]],
) -> Any:  # noqa: ANN401
    """Return an app of `framework` with `middleware` added by hand."""
    build = {
        "fastapi": _fastapi,
        "starlette": _starlette,
        "litestar": _litestar,
    }[framework]
    return build(calls, list(middleware))


def client_of(app: Any) -> Any:  # noqa: ANN401
    """Return a test client of `app`."""
    if isinstance(app, Litestar):
        return LitestarClient(app)
    return StarletteClient(app)


def _store() -> TTLCache[Any]:
    """Return a store over a backend of this test's own."""
    return TTLCache(backend=MemoryCacheAdapter(), serializer=JsonSerializer())


def cache(**options: Any) -> tuple[type, dict[str, Any]]:  # noqa: ANN401
    """Return the response cache added by hand, naming `/feed` and `/session`."""
    return CachedResponsesMiddleware, {
        "cache": _store(),
        "include": {"/feed": 60, "/session": 60},
        **options,
    }


def idempotency(**options: Any) -> tuple[type, dict[str, Any]]:  # noqa: ANN401
    """Return the idempotency middleware added by hand."""
    return IdempotencyMiddleware, {
        "idempotency": Idempotency("http", cache=_store()),
        **options,
    }


def conditional(**options: Any) -> tuple[type, dict[str, Any]]:  # noqa: ANN401
    """Return conditional requests added by hand, requiring one on `PUT /feed`."""
    return ConditionalRequestsMiddleware, {
        "require_precondition": ("PUT",),
        "include": ("/feed",),
        **options,
    }


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_cached_responses_middleware_named_read_is_served_from_cache_on_second_request(
    framework: Framework,
) -> None:
    """A pattern caches the read it names, with no route read for it."""
    # Arrange
    calls = Calls()
    app = built(framework, calls, cache())

    # Act
    with client_of(app) as client:
        first = client.get("/feed")
        second = client.get("/feed")

    # Assert
    assert first.json() == second.json() == {"calls": 1}
    assert "age" in second.headers


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_cached_responses_middleware_read_no_pattern_names_reaches_handler_every_time(
    framework: Framework,
) -> None:
    """A read no pattern names is left to its handler."""
    # Arrange
    calls = Calls()
    app = built(framework, calls, cache())

    # Act
    with client_of(app) as client:
        client.get("/other")
        second = client.get("/other")

    # Assert
    assert second.json() == {"calls": 2}


@pytest.mark.parametrize("credentials", CREDENTIALS)
@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_cached_responses_middleware_request_with_credential_reaches_handler_every_time(
    framework: Framework,
    credentials: dict[str, str],
) -> None:
    """A request carrying a credential never reads the cache and never fills it."""
    # Arrange
    calls = Calls()
    app = built(framework, calls, cache())

    # Act
    with client_of(app) as client:
        client.get("/feed", headers=credentials)
        second = client.get("/feed", headers=credentials)
        anonymous = client.get("/feed")

    # Assert
    assert second.json() == {"calls": 2}
    assert anonymous.json() == {"calls": 3}


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_cached_responses_middleware_response_with_set_cookie_is_not_stored(
    framework: Framework,
) -> None:
    """A response setting a cookie is one caller's, so it is never stored."""
    # Arrange
    calls = Calls()
    app = built(framework, calls, cache())

    # Act
    with client_of(app) as client:
        client.get("/session")
        client.cookies.clear()
        second = client.get("/session")

    # Assert
    assert second.json() == {"calls": 2}
    assert "age" not in second.headers


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_idempotency_middleware_repeated_key_replays_stored_response(
    framework: Framework,
) -> None:
    """A retry with the same key is answered with what the first one stored."""
    # Arrange
    calls = Calls()
    app = built(framework, calls, idempotency())

    # Act
    with client_of(app) as client:
        first = client.post("/charge", headers={HEADER: "k"})
        second = client.post("/charge", headers={HEADER: "k"})

    # Assert
    assert first.json() == second.json() == {"calls": 1}
    assert second.headers[REPLAY_HEADER] == "true"


@pytest.mark.parametrize("credentials", CREDENTIALS)
@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_idempotency_middleware_request_with_credential_is_never_replayed(
    framework: Framework,
    credentials: dict[str, str],
) -> None:
    """The default key is shared, so a credentialed request runs its handler."""
    # Arrange
    calls = Calls()
    app = built(framework, calls, idempotency())

    # Act
    with client_of(app) as client:
        client.post("/charge", headers={HEADER: "k", **credentials})
        second = client.post("/charge", headers={HEADER: "k", **credentials})

    # Assert
    assert second.json() == {"calls": 2}
    assert REPLAY_HEADER not in second.headers


def _per_caller(scope: MutableMapping[str, Any], key: str) -> str:
    """Scope the stored key to the caller the `X-Caller` header names."""
    caller = dict(scope["headers"]).get(b"x-caller", b"").decode()
    return f"{caller}:{key}"


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_idempotency_middleware_key_scoped_per_caller_replays_to_that_caller_only(
    framework: Framework,
) -> None:
    """A key function naming the caller keeps one caller's replay from another."""
    # Arrange
    calls = Calls()
    app = built(framework, calls, idempotency(key=_per_caller))

    # Act
    with client_of(app) as client:
        client.post("/charge", headers={HEADER: "k", "X-Caller": "alice"})
        alice = client.post(
            "/charge", headers={HEADER: "k", "X-Caller": "alice"}
        )
        bob = client.post("/charge", headers={HEADER: "k", "X-Caller": "bob"})

    # Assert
    assert alice.json() == {"calls": 1}
    assert alice.headers[REPLAY_HEADER] == "true"
    assert bob.json() == {"calls": 2}
    assert REPLAY_HEADER not in bob.headers


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_conditional_requests_middleware_named_write_without_precondition_is_refused_428(
    framework: Framework,
) -> None:
    """A write the pattern names needs `If-Match` or `If-None-Match`."""
    # Arrange
    calls = Calls()
    app = built(framework, calls, conditional())

    # Act
    with client_of(app) as client:
        refused = client.put("/feed")
        other = client.put("/other")

    # Assert
    assert refused.status_code == PRECONDITION_REQUIRED
    assert other.status_code == OK
    assert calls.counts == {"replace other": 1}


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_cached_responses_middleware_excluded_read_reaches_handler_every_time(
    framework: Framework,
) -> None:
    """`exclude=` wins over `include=`."""
    # Arrange
    calls = Calls()
    app = built(framework, calls, cache(exclude=("/feed",)))

    # Act
    with client_of(app) as client:
        client.get("/feed")
        second = client.get("/feed")

    # Assert
    assert second.json() == {"calls": 2}


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_idempotency_middleware_excluded_write_is_never_replayed(
    framework: Framework,
) -> None:
    """A path `exclude=` names runs its handler for every retry."""
    # Arrange
    calls = Calls()
    app = built(framework, calls, idempotency(exclude=("/charge",)))

    # Act
    with client_of(app) as client:
        client.post("/charge", headers={HEADER: "k"})
        second = client.post("/charge", headers={HEADER: "k"})

    # Assert
    assert second.json() == {"calls": 2}
    assert REPLAY_HEADER not in second.headers


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_conditional_requests_middleware_excluded_write_without_precondition_reaches_handler(
    framework: Framework,
) -> None:
    """A write `exclude=` names is not refused, every other one is."""
    # Arrange
    calls = Calls()
    app = built(framework, calls, conditional(include=(), exclude=("/feed",)))

    # Act
    with client_of(app) as client:
        excluded = client.put("/feed")
        refused = client.put("/other")

    # Assert
    assert excluded.status_code == OK
    assert refused.status_code == PRECONDITION_REQUIRED
    assert calls.counts == {"replace": 1}


class _KeyRequired:
    """Refuse `401` a request without `X-API-Key: expected`, as an app's own authentication."""

    def __init__(self, app: Any) -> None:  # noqa: ANN401
        """Wrap the next app."""
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        """Refuse the request, or pass it on."""
        if dict(scope["headers"]).get(b"x-api-key") != b"expected":
            await send(
                {"type": "http.response.start", "status": 401, "headers": []}
            )
            await send({"type": "http.response.body", "body": b""})
            return
        await self.app(scope, receive, send)


@pytest.mark.parametrize("registered", [False, True])
def test_idempotency_middleware_defined_on_litestar_replays_behind_its_authentication(
    registered: bool,  # noqa: FBT001
) -> None:
    """The placement `middleware-placement` names: the app's authentication runs first."""
    # Arrange
    calls = Calls()

    @post("/charge", status_code=OK)
    async def charge() -> dict[str, int]:
        return calls.served("charge")

    app = Litestar(
        route_handlers=[charge],
        middleware=[
            DefineMiddleware(_KeyRequired),
            DefineMiddleware(
                IdempotencyMiddleware,  # ty: ignore[invalid-argument-type]
                idempotency=Idempotency("http"),
            ),
        ],
    )
    uses: list[Any] = [MemoryProvider()]
    if registered:
        uses.append(IdempotentRequests())
    Grelmicro(uses=uses).install(app)
    headers = {HEADER: "k", "X-API-Key": "expected"}

    # Act
    with LitestarClient(app) as client:
        first = client.post("/charge", headers=headers)
        replayed = client.post("/charge", headers=headers)
        refused = client.post("/charge", headers={HEADER: "k"})

    # Assert
    assert first.json() == replayed.json() == {"calls": 1}
    assert replayed.headers[REPLAY_HEADER] == "true"
    assert refused.status_code == UNAUTHORIZED
    assert REPLAY_HEADER not in refused.headers
    assert calls.counts == {"charge": 1}


@pytest.fixture
def route_reads(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record every app an integration is asked to list the routes of."""
    reads: list[object] = []

    def listed(app: object) -> list[Any]:
        reads.append(app)
        return []

    for integration in (on_fastapi, on_litestar, on_starlette):
        monkeypatch.setattr(integration, "route_declarations", listed)
    return reads


@pytest.mark.parametrize("framework", FRAMEWORKS)
def test_hand_added_middleware_serving_requests_reads_no_route(
    framework: Framework,
    route_reads: list[object],
) -> None:
    """Built, started and serving, none of the three lists the app's routes."""
    # Arrange
    calls = Calls()
    app = built(framework, calls, cache(), idempotency(), conditional())

    # Act
    with client_of(app) as client:
        client.get("/feed")
        client.get("/feed")
        client.post("/charge", headers={HEADER: "k"})
        client.post("/charge", headers={HEADER: "k"})
        client.put("/feed")

    # Assert
    assert route_reads == []
