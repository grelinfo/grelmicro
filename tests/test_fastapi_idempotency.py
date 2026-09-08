"""Tests for the HTTP idempotency middleware."""

from __future__ import annotations

import asyncio
import gzip
import importlib
import json
import sys
from typing import TYPE_CHECKING, Annotated, Any
from unittest.mock import patch

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.security import APIKeyHeader
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    SimpleUser,
)
from starlette.background import BackgroundTasks
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.base import (
    BaseHTTPMiddleware,
    RequestResponseEndpoint,
)
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.status import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_409_CONFLICT,
    HTTP_413_CONTENT_TOO_LARGE,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_502_BAD_GATEWAY,
)

from grelmicro import Grelmicro
from grelmicro.cache import Cache, TTLCache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.cache.serializers import JsonSerializer
from grelmicro.errors import DependencyNotFoundError, OutOfContextError
from grelmicro.http import IdempotencyMiddleware, IdempotentRequests
from grelmicro.idempotency import Idempotency
from grelmicro.idempotency.errors import IdempotencyKeyMakerError
from grelmicro.integrations.fastapi import document_idempotency

if TYPE_CHECKING:
    from collections.abc import (
        AsyncIterator,
        Awaitable,
        Callable,
        Iterator,
        MutableMapping,
    )

    from starlette.requests import HTTPConnection

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]

pytestmark = [pytest.mark.timeout(10)]

KEY = {"Idempotency-Key": "key-1"}
LARGE_BODY = b"x" * 4096


class _Forking(BaseHTTPMiddleware):
    """A middleware whose task group runs the rest in a copied context."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Pass the request through untouched."""
        return await call_next(request)


class _HeaderAuthentication(AuthenticationBackend):
    """Authenticate the caller named by a custom API-key header."""

    async def authenticate(
        self, conn: HTTPConnection
    ) -> tuple[AuthCredentials, SimpleUser] | None:
        """Return a standard Starlette identity when the header is present."""
        identity = conn.headers.get("x-api-key")
        if identity is None:
            return None
        return AuthCredentials(["authenticated"]), SimpleUser(identity)


async def _private_identity(request: Request) -> JSONResponse:
    """Answer the authenticated identity or reject an anonymous request."""
    if not request.user.is_authenticated:
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED)
    return JSONResponse({"user": request.user.display_name})


def _register_result_routes(app: FastAPI, calls: dict[str, int]) -> None:
    """Register the routes covering ordinary results and failures."""

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        calls["count"] += 1
        return {"call": calls["count"]}

    @app.post("/fail")
    async def fail() -> Response:
        calls["count"] += 1
        return Response(
            content=b'{"detail":"downstream gone"}',
            status_code=502,
            media_type="application/json",
        )

    @app.post("/raise")
    async def raises() -> None:
        calls["count"] += 1
        msg = "boom"
        raise RuntimeError(msg)

    @app.post("/created")
    async def created() -> Response:
        calls["count"] += 1
        return Response(status_code=201, headers={"Location": "/orders/1"})

    @app.post("/text")
    async def text() -> Response:
        calls["count"] += 1
        return Response(content="plain", media_type="text/plain")


def _register_storage_routes(app: FastAPI, calls: dict[str, int]) -> None:
    """Register the routes covering the storage rules."""

    @app.post("/cookie")
    async def cookie() -> Response:
        calls["count"] += 1
        response = Response(content=b"{}", media_type="application/json")
        response.set_cookie("session", "abc")
        return response

    @app.post("/opt-out")
    async def opt_out() -> Response:
        calls["count"] += 1
        return Response(
            content=b'{"store":false}', media_type="application/json"
        )

    @app.post("/large")
    async def large() -> Response:
        calls["count"] += 1
        return Response(content=LARGE_BODY, media_type="text/plain")

    @app.post("/background")
    async def background() -> Response:
        calls["count"] += 1
        tasks = BackgroundTasks()
        tasks.add_task(lambda: None)
        return Response(content=b"{}", media_type="application/json")

    @app.post("/encoded")
    async def encoded() -> Response:
        calls["count"] += 1
        return Response(
            content=gzip.compress(b"{}"),
            media_type="application/json",
            headers={"Content-Encoding": "gzip"},
        )

    @app.post("/stream")
    async def stream() -> StreamingResponse:
        calls["count"] += 1

        async def chunks() -> AsyncIterator[bytes]:
            yield b"one "
            yield b"two"

        return StreamingResponse(chunks(), media_type="text/plain")

    @app.post("/no-content")
    async def no_content() -> Response:
        calls["count"] += 1
        return Response(status_code=204)

    @app.get("/read")
    async def read() -> dict[str, int]:
        calls["count"] += 1
        return {"call": calls["count"]}


def build_app(**options: Any) -> FastAPI:  # noqa: ANN401
    """Build an app whose handlers exercise the middleware's storage rules."""
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    app = FastAPI()
    app.add_middleware(
        IdempotencyMiddleware,
        idempotency=Idempotency("http", ttl=60),
        **options,
    )
    micro.install(app)
    calls = {"count": 0}
    app.state.calls = calls
    _register_result_routes(app, calls)
    _register_storage_routes(app, calls)
    return app


@pytest.fixture
def client_factory() -> Iterator[
    Callable[..., tuple[TestClient, dict[str, int]]]
]:
    """Yield a factory building a started client and its call counter."""
    clients: list[TestClient] = []

    def factory(**options: Any) -> tuple[TestClient, dict[str, int]]:  # noqa: ANN401
        app = build_app(**options)
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client, app.state.calls

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


def test_middleware_repeated_key_replays_stored_response(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """A retry with the same key replays the response without re-running."""
    # Arrange
    client, calls = client_factory()
    # Act
    first = client.post("/charge", headers=KEY)
    second = client.post("/charge", headers=KEY)
    # Assert
    assert first.json() == {"call": 1}
    assert second.json() == {"call": 1}
    assert calls == {"count": 1}
    assert "idempotent-replayed" not in first.headers
    assert second.headers["idempotent-replayed"] == "true"


@pytest.mark.parametrize(
    "credentials",
    [
        pytest.param({"Authorization": "Bearer secret"}, id="authorization"),
        pytest.param({"Cookie": "session=secret"}, id="cookie"),
    ],
)
def test_middleware_unscoped_key_bypasses_private_requests(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
    credentials: dict[str, str],
) -> None:
    """A private response is never stored under the shared default key."""
    # Arrange
    client, calls = client_factory()
    headers = {**KEY, **credentials}
    # Act
    first = client.post("/charge", headers=headers)
    second = client.post("/charge", headers=headers)
    # Assert
    assert first.json() == {"call": 1}
    assert second.json() == {"call": 2}
    assert calls == {"count": 2}
    assert "idempotent-replayed" not in second.headers


def test_middleware_replay_never_skips_route_authentication() -> None:
    """A cached response cannot turn an unauthenticated retry into a success."""
    # Arrange
    app = build_app()

    async def authenticate(request: Request) -> None:
        if request.headers.get("authorization") != "Bearer secret":
            raise HTTPException(status_code=HTTP_401_UNAUTHORIZED)

    @app.post("/private", dependencies=[Depends(authenticate)])
    async def private() -> dict[str, str]:
        return {"secret": "sensitive"}

    # Act
    with TestClient(app) as client:
        authorized = client.post(
            "/private",
            headers={**KEY, "Authorization": "Bearer secret"},
        )
        unauthenticated = client.post("/private", headers=KEY)

    # Assert
    assert authorized.status_code == HTTP_200_OK
    assert authorized.json() == {"secret": "sensitive"}
    assert unauthenticated.status_code == HTTP_401_UNAUTHORIZED
    assert "idempotent-replayed" not in unauthenticated.headers


def test_custom_header_dependency_runs_before_every_response() -> None:
    """An ordinary custom-header dependency cannot be skipped by a replay."""
    # Arrange
    app = build_app()

    async def authenticate(
        x_api_key: Annotated[str | None, Header()] = None,
    ) -> None:
        if x_api_key != "expected":
            raise HTTPException(status_code=HTTP_401_UNAUTHORIZED)

    @app.post("/custom-private", dependencies=[Depends(authenticate)])
    async def private() -> dict[str, str]:
        return {"private": "value"}

    # Act
    with TestClient(app) as client:
        authorized = client.post(
            "/custom-private", headers={**KEY, "X-API-Key": "expected"}
        )
        unauthenticated = client.post("/custom-private", headers=KEY)

    # Assert
    assert authorized.status_code == HTTP_200_OK
    assert unauthenticated.status_code == HTTP_401_UNAUTHORIZED
    assert "idempotent-replayed" not in unauthenticated.headers


def test_direct_wrapper_detects_fastapi_dependencies() -> None:
    """A middleware directly wrapping FastAPI still sees its dependencies."""
    # Arrange
    app = FastAPI()
    api_key = APIKeyHeader(name="X-API-Key")

    @app.post("/private", dependencies=[Depends(api_key)])
    async def private() -> dict[str, str]:
        return {"private": "value"}

    wrapped = IdempotencyMiddleware(
        app,
        idempotency=Idempotency(
            "direct",
            ttl=60,
            cache=TTLCache(
                backend=MemoryCacheAdapter(), serializer=JsonSerializer()
            ),
        ),
    )

    # Act
    with TestClient(wrapped) as client:
        authorized = client.post(
            "/private", headers={**KEY, "X-API-Key": "expected"}
        )
        unauthenticated = client.post("/private", headers=KEY)

    # Assert
    assert authorized.status_code == HTTP_200_OK
    assert unauthenticated.status_code == HTTP_401_UNAUTHORIZED
    assert "idempotent-replayed" not in unauthenticated.headers


def test_starlette_root_detects_mounted_fastapi_dependencies() -> None:
    """A non-FastAPI root still finds dependencies in a FastAPI mount."""
    # Arrange
    root = Starlette()
    app = FastAPI()
    api_key = APIKeyHeader(name="X-API-Key")

    @app.post("/private", dependencies=[Depends(api_key)])
    async def private() -> dict[str, str]:
        return {"private": "value"}

    root.mount("/api", app)
    root.add_middleware(
        IdempotencyMiddleware,
        idempotency=Idempotency(
            "mounted-private",
            ttl=60,
            cache=TTLCache(
                backend=MemoryCacheAdapter(), serializer=JsonSerializer()
            ),
        ),
    )

    # Act
    with TestClient(root) as client:
        authorized = client.post(
            "/api/private", headers={**KEY, "X-API-Key": "expected"}
        )
        unauthenticated = client.post("/api/private", headers=KEY)

    # Assert
    assert authorized.status_code == HTTP_200_OK
    assert unauthenticated.status_code == HTTP_401_UNAUTHORIZED
    assert "idempotent-replayed" not in unauthenticated.headers


def test_middleware_wrapped_fastapi_mount_detects_dependencies() -> None:
    """Middleware around a mounted FastAPI app cannot hide its dependencies."""
    # Arrange
    root = Starlette()
    app = FastAPI()
    api_key = APIKeyHeader(name="X-API-Key")

    @app.post("/private", dependencies=[Depends(api_key)])
    async def private() -> dict[str, str]:
        return {"private": "value"}

    root.mount("/api", CORSMiddleware(app, allow_origins=["*"]))
    root.add_middleware(
        IdempotencyMiddleware,
        idempotency=Idempotency(
            "wrapped-mount",
            ttl=60,
            cache=TTLCache(
                backend=MemoryCacheAdapter(), serializer=JsonSerializer()
            ),
        ),
    )

    # Act
    with TestClient(root) as client:
        authorized = client.post(
            "/api/private", headers={**KEY, "X-API-Key": "expected"}
        )
        unauthenticated = client.post("/api/private", headers=KEY)

    # Assert
    assert authorized.status_code == HTTP_200_OK
    assert unauthenticated.status_code == HTTP_401_UNAUTHORIZED
    assert "idempotent-replayed" not in unauthenticated.headers


@pytest.mark.parametrize("placement", ["outside", "inside"])
def test_authenticated_asgi_scope_never_replays_across_callers(
    placement: str,
) -> None:
    """Standard ASGI authentication bypasses an unscoped default key."""

    # Arrange
    async def private(request: Request) -> JSONResponse:
        return JSONResponse({"user": request.user.display_name})

    app = Starlette(routes=[Route("/private", private, methods=["POST"])])

    def add_idempotency() -> None:
        app.add_middleware(
            IdempotencyMiddleware,
            idempotency=Idempotency(
                "asgi-authentication",
                ttl=60,
                cache=TTLCache(
                    backend=MemoryCacheAdapter(), serializer=JsonSerializer()
                ),
            ),
        )

    if placement == "outside":
        add_idempotency()
        app.add_middleware(
            AuthenticationMiddleware, backend=_HeaderAuthentication()
        )
    else:
        app.add_middleware(
            AuthenticationMiddleware, backend=_HeaderAuthentication()
        )
        add_idempotency()

    # Act
    with TestClient(app) as client:
        alice = client.post("/private", headers={**KEY, "X-API-Key": "alice"})
        bob = client.post("/private", headers={**KEY, "X-API-Key": "bob"})

    # Assert
    assert alice.json() == {"user": "alice"}
    assert bob.json() == {"user": "bob"}
    assert "idempotent-replayed" not in bob.headers


@pytest.mark.parametrize("configuration", ["lazy", "instantiated"])
def test_wrapped_application_authentication_runs_before_replay(
    configuration: str,
) -> None:
    """Authentication configured inside the wrapper cannot be skipped."""
    # Arrange
    route = Route("/private", _private_identity, methods=["POST"])
    app = (
        Starlette(
            routes=[route],
            middleware=[
                Middleware(
                    AuthenticationMiddleware, backend=_HeaderAuthentication()
                )
            ],
        )
        if configuration == "lazy"
        else AuthenticationMiddleware(
            Starlette(routes=[route]), backend=_HeaderAuthentication()
        )
    )
    wrapped = IdempotencyMiddleware(
        app,
        idempotency=Idempotency(
            "lazy-authentication",
            ttl=60,
            cache=TTLCache(
                backend=MemoryCacheAdapter(), serializer=JsonSerializer()
            ),
        ),
    )

    # Act
    with TestClient(wrapped) as client:
        alice = client.post("/private", headers={**KEY, "X-API-Key": "alice"})
        anonymous = client.post("/private", headers=KEY)

    # Assert
    assert alice.json() == {"user": "alice"}
    assert anonymous.status_code == HTTP_401_UNAUTHORIZED
    assert "idempotent-replayed" not in anonymous.headers


def test_mounted_authentication_is_path_specific() -> None:
    """A protected mount bypasses replay without disabling a public sibling."""
    # Arrange
    private = Starlette(
        routes=[Route("/value", _private_identity, methods=["POST"])],
        middleware=[
            Middleware(
                AuthenticationMiddleware, backend=_HeaderAuthentication()
            )
        ],
    )
    calls = 0

    async def public(_request: Request) -> JSONResponse:
        nonlocal calls
        calls += 1
        return JSONResponse({"call": calls})

    root = Starlette()
    root.mount("/private", private)
    root.mount(
        "/public",
        Starlette(routes=[Route("/value", public, methods=["POST"])]),
    )
    root.add_middleware(
        IdempotencyMiddleware,
        idempotency=Idempotency(
            "mounted-authentication",
            ttl=60,
            cache=TTLCache(
                backend=MemoryCacheAdapter(), serializer=JsonSerializer()
            ),
        ),
    )

    # Act
    with TestClient(root) as client:
        alice = client.post(
            "/private/value", headers={**KEY, "X-API-Key": "alice"}
        )
        anonymous = client.post("/private/value", headers=KEY)
        public_first = client.post("/public/value", headers=KEY)
        public_replay = client.post("/public/value", headers=KEY)

    # Assert
    assert alice.json() == {"user": "alice"}
    assert anonymous.status_code == HTTP_401_UNAUTHORIZED
    assert "idempotent-replayed" not in anonymous.headers
    assert public_first.json() == {"call": 1}
    assert public_replay.json() == {"call": 1}
    assert public_replay.headers["idempotent-replayed"] == "true"


def test_middleware_replay_never_skips_api_key_security() -> None:
    """A custom credential header cannot put a gated route in a shared entry."""
    # Arrange
    app = build_app()
    api_key = APIKeyHeader(name="X-API-Key")
    calls = 0

    @app.post("/api-private", dependencies=[Depends(api_key)])
    async def private() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"call": calls}

    # Act
    with TestClient(app) as client:
        authorized = client.post(
            "/api-private", headers={**KEY, "X-API-Key": "secret"}
        )
        unauthenticated = client.post("/api-private", headers=KEY)

    # Assert
    assert authorized.json() == {"call": 1}
    assert "idempotent-replayed" not in authorized.headers
    assert unauthenticated.status_code == HTTP_401_UNAUTHORIZED
    assert "idempotent-replayed" not in unauthenticated.headers


def test_standalone_middleware_detects_api_key_security() -> None:
    """Hand-added middleware reads security metadata from the request app."""
    # Arrange
    app = FastAPI()
    app.add_middleware(
        IdempotencyMiddleware,
        idempotency=Idempotency(
            "standalone",
            ttl=60,
            cache=TTLCache(backend=MemoryCacheAdapter()),
        ),
    )
    api_key = APIKeyHeader(name="X-API-Key")

    @app.post("/private", dependencies=[Depends(api_key)])
    async def private() -> dict[str, str]:
        return {"secret": "sensitive"}

    # Act
    with TestClient(app) as client:
        authorized = client.post(
            "/private", headers={**KEY, "X-API-Key": "secret"}
        )
        unauthenticated = client.post("/private", headers=KEY)

    # Assert
    assert authorized.status_code == HTTP_200_OK
    assert unauthenticated.status_code == HTTP_401_UNAUTHORIZED
    assert "idempotent-replayed" not in unauthenticated.headers


def test_parent_security_does_not_disable_public_mounted_idempotency() -> None:
    """A parent's dependency is not enforced inside a mounted application."""
    # Arrange
    api_key = APIKeyHeader(name="X-API-Key")
    root = FastAPI(dependencies=[Depends(api_key)])
    root.add_middleware(
        IdempotencyMiddleware,
        idempotency=Idempotency(
            "mounted",
            ttl=60,
            cache=TTLCache(
                backend=MemoryCacheAdapter(), serializer=JsonSerializer()
            ),
        ),
    )
    sub = FastAPI()
    calls = 0

    @sub.post("/charge")
    async def charge() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"call": calls}

    root.mount("/sub", sub)

    # Act
    with TestClient(root) as client:
        first = client.post("/sub/charge", headers=KEY)
        replayed = client.post("/sub/charge", headers=KEY)

    # Assert
    assert first.json() == {"call": 1}
    assert replayed.json() == {"call": 1}
    assert replayed.headers["idempotent-replayed"] == "true"


@pytest.mark.parametrize("credential", ["Authorization", "Cookie"])
def test_private_header_does_not_bypass_required_key(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
    credential: str,
) -> None:
    """Credentialed requests still obey the required-key contract."""
    # Arrange
    client, calls = client_factory(require_key=True)
    # Act
    response = client.post("/charge", headers={credential: "ignored"})
    # Assert
    assert response.status_code == HTTP_400_BAD_REQUEST
    assert calls == {"count": 0}


def test_middleware_request_without_key_passes_through(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """A request with no key runs every time and stores nothing."""
    # Arrange
    client, _calls = client_factory()
    # Act
    client.post("/charge")
    second = client.post("/charge")
    # Assert
    assert second.json() == {"call": 2}


def test_middleware_unlisted_method_passes_through(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """A GET carrying a key is untouched, because only POST is listed."""
    # Arrange
    client, _calls = client_factory()
    # Act
    client.get("/read", headers=KEY)
    second = client.get("/read", headers=KEY)
    # Assert
    assert second.json() == {"call": 2}


def test_middleware_different_key_executes_again(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """A different key is a different operation."""
    # Arrange
    client, _calls = client_factory()
    # Act
    client.post("/charge", headers=KEY)
    second = client.post("/charge", headers={"Idempotency-Key": "key-2"})
    # Assert
    assert second.json() == {"call": 2}


def test_middleware_same_key_on_another_route_executes_again(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """The stored key is scoped per route, so routes never cross-replay."""
    # Arrange
    client, _calls = client_factory()
    # Act
    client.post("/charge", headers=KEY)
    other = client.post("/created", headers=KEY)
    # Assert
    assert other.status_code == HTTP_201_CREATED


def test_default_storage_key_does_not_read_legacy_entries() -> None:
    """An upgraded process never replays an entry from the unsafe key format."""
    # Arrange
    middleware = IdempotencyMiddleware(
        FastAPI(), idempotency=Idempotency("http", ttl=60)
    )
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/private",
        "query_string": b"",
    }
    legacy_key = "POST\x1f/private\x1fkey-1"

    # Act
    storage_key = middleware._storage_key(scope, "key-1")

    # Assert
    assert storage_key == "v2\x1fPOST\x1f/private\x1fkey-1"
    assert storage_key != legacy_key


def test_middleware_query_string_is_part_of_the_key(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """A different query string is a different operation."""
    # Arrange
    client, _calls = client_factory()
    # Act
    client.post("/charge?dry_run=1", headers=KEY)
    second = client.post("/charge", headers=KEY)
    # Assert
    assert second.json() == {"call": 2}


def test_middleware_key_maker_isolates_two_callers(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """One caller never replays another caller's stored response."""
    # Arrange
    client, _calls = client_factory(
        key_maker=lambda scope, key: (
            dict(scope["headers"]).get(b"x-tenant", b"").decode()
            + "|"
            + scope["path"]
            + "|"
            + key
        )
    )
    # Act
    first = client.post("/charge", headers={**KEY, "X-Tenant": "a"})
    second = client.post("/charge", headers={**KEY, "X-Tenant": "b"})
    # Assert
    assert first.json() == {"call": 1}
    assert second.json() == {"call": 2}


def test_middleware_error_response_is_replayed(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """A returned 5xx is stored, so a retry cannot re-run the side effect."""
    # Arrange
    client, calls = client_factory()
    # Act
    first = client.post("/fail", headers=KEY)
    second = client.post("/fail", headers=KEY)
    # Assert
    assert first.status_code == HTTP_502_BAD_GATEWAY
    assert second.status_code == HTTP_502_BAD_GATEWAY
    assert second.headers["idempotent-replayed"] == "true"
    assert calls == {"count": 1}


def test_middleware_raised_exception_stores_nothing(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """A handler that raises leaves the key free for a fresh retry."""
    # Arrange
    client, calls = client_factory()
    # Act
    with pytest.raises(RuntimeError):
        client.post("/raise", headers=KEY)
    with pytest.raises(RuntimeError):
        client.post("/raise", headers=KEY)
    # Assert
    assert calls == {"count": 2}


def test_middleware_empty_body_response_is_replayed(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """A 201 with no body replays, which JSON-only storage would miss."""
    # Arrange
    client, calls = client_factory()
    # Act
    client.post("/created", headers=KEY)
    second = client.post("/created", headers=KEY)
    # Assert
    assert second.status_code == HTTP_201_CREATED
    assert second.headers["location"] == "/orders/1"
    assert second.headers["idempotent-replayed"] == "true"
    assert calls == {"count": 1}


def test_middleware_non_json_response_is_replayed(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """Bodies are opaque bytes, so a text response replays like any other."""
    # Arrange
    client, calls = client_factory()
    # Act
    client.post("/text", headers=KEY)
    second = client.post("/text", headers=KEY)
    # Assert
    assert second.text == "plain"
    assert calls == {"count": 1}


def test_middleware_set_cookie_response_is_not_stored(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """Replaying a cookie would leak a session, so the response is skipped."""
    # Arrange
    client, calls = client_factory()
    # Act
    client.post("/cookie", headers=KEY)
    second = client.post("/cookie", headers=KEY)
    # Assert
    assert calls == {"count": 2}
    assert "idempotent-replayed" not in second.headers


def test_middleware_skip_predicate_blocks_storage(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """`skip` sees the finished response and can refuse to store it."""
    # Arrange
    client, calls = client_factory(
        skip=lambda response: b'"store":false' in response["body"]
    )
    # Act
    client.post("/opt-out", headers=KEY)
    client.post("/opt-out", headers=KEY)
    # Assert
    assert calls == {"count": 2}


def test_middleware_skip_predicate_allows_other_responses(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """A response the predicate passes still replays."""
    # Arrange
    client, calls = client_factory(
        skip=lambda response: response["status"] != HTTP_200_OK
    )
    # Act
    client.post("/charge", headers=KEY)
    second = client.post("/charge", headers=KEY)
    # Assert
    assert second.headers["idempotent-replayed"] == "true"
    assert calls == {"count": 1}


def test_middleware_oversized_response_is_not_stored(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """A body over max_body_size streams through unstored."""
    # Arrange
    client, calls = client_factory(max_body_size=1024)
    # Act
    first = client.post("/large", headers=KEY)
    client.post("/large", headers=KEY)
    # Assert
    assert first.content == LARGE_BODY
    assert calls == {"count": 2}


def test_middleware_background_task_response_is_replayed(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """A response with a background task still stores and replays."""
    # Arrange
    client, calls = client_factory()
    # Act
    client.post("/background", headers=KEY)
    second = client.post("/background", headers=KEY)
    # Assert
    assert second.headers["idempotent-replayed"] == "true"
    assert calls == {"count": 1}


def test_middleware_missing_key_rejected_when_required(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """`require_key` turns a missing header into a 400."""
    # Arrange
    client, calls = client_factory(require_key=True)
    # Act
    response = client.post("/charge")
    # Assert
    assert response.status_code == HTTP_400_BAD_REQUEST
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["type"].endswith("#idempotency-key-invalid")
    assert "Idempotency-Key" in body["detail"]
    assert body["instance"] == "/charge"
    assert calls == {"count": 0}


def test_middleware_oversized_key_rejected(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """An unbounded key would grow the store, so it is rejected."""
    # Arrange
    client, calls = client_factory()
    # Act
    response = client.post("/charge", headers={"Idempotency-Key": "x" * 256})
    # Assert
    assert response.status_code == HTTP_400_BAD_REQUEST
    assert calls == {"count": 0}


def test_middleware_reused_key_with_new_body_conflicts(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """`fingerprint_body` answers 422 when the payload changed."""
    # Arrange
    client, calls = client_factory(fingerprint_body=True)
    # Act
    first = client.post("/charge", headers=KEY, content=b'{"a":1}')
    second = client.post("/charge", headers=KEY, content=b'{"a":2}')
    # Assert
    assert first.status_code == HTTP_200_OK
    assert second.status_code == HTTP_422_UNPROCESSABLE_CONTENT
    assert second.headers["content-type"] == "application/problem+json"
    assert second.json()["type"].endswith("#idempotency-key-reused")
    assert calls == {"count": 1}


def test_middleware_reused_key_with_same_body_replays(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """The buffered body reaches the handler and the same payload replays."""
    # Arrange
    client, calls = client_factory(fingerprint_body=True)
    # Act
    client.post("/charge", headers=KEY, content=b'{"a":1}')
    second = client.post("/charge", headers=KEY, content=b'{"a":1}')
    # Assert
    assert second.headers["idempotent-replayed"] == "true"
    assert calls == {"count": 1}


def test_middleware_oversized_body_rejected_when_fingerprinting(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """A body too large to hash is a 413 rather than an unbounded buffer."""
    # Arrange
    client, calls = client_factory(fingerprint_body=True, max_body_size=16)
    # Act
    response = client.post("/charge", headers=KEY, content=b"x" * 64)
    # Assert
    assert response.status_code == HTTP_413_CONTENT_TOO_LARGE
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"].endswith("#request-body-too-large")
    assert calls == {"count": 0}


def test_middleware_bodyless_status_replays_without_content_length(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """A 204 replay carries no Content-Length, which its status forbids."""
    # Arrange
    client, calls = client_factory()
    # Act
    client.post("/no-content", headers=KEY)
    second = client.post("/no-content", headers=KEY)
    # Assert
    assert second.status_code == HTTP_204_NO_CONTENT
    assert "content-length" not in second.headers
    assert second.headers["idempotent-replayed"] == "true"
    assert calls == {"count": 1}


def test_middleware_content_encoding_response_is_not_stored(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """Encoded bytes must not reach a client that never negotiated them."""
    # Arrange
    client, calls = client_factory()
    # Act
    client.post("/encoded", headers=KEY)
    client.post("/encoded", headers=KEY)
    # Assert
    assert calls == {"count": 2}


def test_middleware_streaming_response_is_replayed(
    client_factory: Callable[..., tuple[TestClient, dict[str, int]]],
) -> None:
    """A chunked response is stored whole once its last chunk arrives."""
    # Arrange
    client, calls = client_factory()
    # Act
    first = client.post("/stream", headers=KEY)
    second = client.post("/stream", headers=KEY)
    # Assert
    assert first.text == "one two"
    assert second.text == "one two"
    assert calls == {"count": 1}


def test_middleware_added_after_install_still_resolves_the_cache() -> None:
    """Adding the middleware after `install` replays as adding it before does."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    app = FastAPI()
    micro.install(app)
    app.add_middleware(
        IdempotencyMiddleware, idempotency=Idempotency("http", ttl=60)
    )
    calls = {"count": 0}

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        calls["count"] += 1
        return {"call": calls["count"]}

    # Act
    with TestClient(app) as client:
        first = client.post("/charge", headers=KEY)
        second = client.post("/charge", headers=KEY)

    # Assert
    assert first.json() == {"call": 1}
    assert second.json() == {"call": 1}
    assert second.headers["idempotent-replayed"] == "true"
    assert calls == {"count": 1}


def test_middleware_resolves_the_cache_under_a_forking_middleware() -> None:
    """A `BaseHTTPMiddleware` in between copies the context, and binding holds."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    app = FastAPI()
    micro.install(app)
    app.add_middleware(
        IdempotencyMiddleware, idempotency=Idempotency("http", ttl=60)
    )
    app.add_middleware(_Forking)
    calls = {"count": 0}

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        calls["count"] += 1
        return {"call": calls["count"]}

    # Act
    with TestClient(app) as client:
        first = client.post("/charge", headers=KEY)
        second = client.post("/charge", headers=KEY)

    # Assert
    assert first.json() == {"call": 1}
    assert second.json() == {"call": 1}
    assert second.headers["idempotent-replayed"] == "true"
    assert calls == {"count": 1}


def _app_with_key_maker(key_maker: Any) -> FastAPI:  # noqa: ANN401
    """Build an installed app whose middleware uses `key_maker`."""
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    app = FastAPI()
    micro.install(app)
    app.add_middleware(
        IdempotencyMiddleware,
        idempotency=Idempotency("http", ttl=60),
        key_maker=key_maker,
    )

    @app.post("/charge")
    async def charge() -> dict[str, bool]:
        return {"ok": True}

    return app


def test_key_maker_carrying_an_unresolved_none_is_refused() -> None:
    """A key built from a value that was not set yet merges every caller.

    This is the shape a scope-reading `key_maker` takes when the middleware
    it depends on runs inside this one. The key still looks per-caller and
    separates nobody, so it is refused rather than stored under.
    """
    # Arrange
    app = _app_with_key_maker(
        lambda scope, key: f"{scope.get('state', {}).get('missing')}|{key}"
    )

    # Act / Assert
    with (
        TestClient(app) as client,
        pytest.raises(IdempotencyKeyMakerError, match="unresolved None"),
    ):
        client.post("/charge", headers=KEY)


def test_key_maker_dropping_the_client_key_is_refused() -> None:
    """Without the client's key every request to the route shares one entry."""
    # Arrange
    app = _app_with_key_maker(lambda scope, _key: scope["path"])

    # Act / Assert
    with (
        TestClient(app) as client,
        pytest.raises(IdempotencyKeyMakerError, match="drops the client"),
    ):
        client.post("/charge", headers=KEY)


def test_key_maker_returning_an_empty_key_is_refused() -> None:
    """An empty key is one bucket for the whole application."""
    # Arrange
    app = _app_with_key_maker(lambda _scope, _key: "")

    # Act / Assert
    with (
        TestClient(app) as client,
        pytest.raises(IdempotencyKeyMakerError, match="non-empty key"),
    ):
        client.post("/charge", headers=KEY)


def test_key_maker_keeping_a_none_lookalike_is_accepted() -> None:
    """A tenant whose name merely contains None is not a partial key."""
    # Arrange
    app = _app_with_key_maker(lambda _scope, key: f"NoneSuchCorp\x1f{key}")

    # Act
    with TestClient(app) as client:
        first = client.post("/charge", headers=KEY)
        second = client.post("/charge", headers=KEY)

    # Assert
    assert first.status_code == HTTP_200_OK
    assert second.headers["idempotent-replayed"] == "true"


def test_middleware_without_grelmicro_scope_names_the_fix() -> None:
    """The out-of-context error tells the reader to call `micro.install`."""
    # Arrange
    app = FastAPI()
    app.add_middleware(
        IdempotencyMiddleware, idempotency=Idempotency("http", ttl=60)
    )

    @app.post("/charge")
    async def charge() -> dict[str, str]:
        return {"status": "ok"}

    # Act
    with (
        TestClient(app) as client,
        pytest.raises(OutOfContextError, match=r"micro\.install"),
    ):
        client.post("/charge", headers=KEY)


async def _drive(
    middleware: IdempotencyMiddleware, messages: list[Message]
) -> list[Message]:
    """Run one request through the middleware and collect what it sends."""
    incoming = iter(messages)
    sent: list[Message] = []

    async def receive() -> Message:
        return next(incoming, {"type": "http.disconnect"})

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/charge",
        "headers": [(b"idempotency-key", b"key-1")],
    }
    await middleware(scope, receive, send)
    return sent


async def test_middleware_hashes_a_chunked_request_body() -> None:
    """A multi-chunk body is buffered whole and replayed to the handler."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    seen: list[bytes] = []

    async def app(
        scope: Scope,  # noqa: ARG001
        receive: Receive,
        send: Send,
    ) -> None:
        chunks = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        seen.append(b"".join(chunks))
        # A second read falls through to the original receive.
        assert (await receive())["type"] == "http.disconnect"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    middleware = IdempotencyMiddleware(
        app,
        idempotency=Idempotency("http", ttl=60),
        fingerprint_body=True,
    )
    # Act
    async with micro:
        sent = await _drive(
            middleware,
            [
                {"type": "http.request", "body": b"one ", "more_body": True},
                {"type": "http.request", "body": b"two", "more_body": False},
            ],
        )
    # Assert
    assert seen == [b"one two"]
    assert sent[0]["status"] == HTTP_200_OK


async def test_middleware_disconnect_before_body_is_not_fingerprinted() -> None:
    """A truncated body is never hashed as if it were whole."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])

    async def app(
        scope: Scope,  # noqa: ARG001
        receive: Receive,  # noqa: ARG001
        send: Send,
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    middleware = IdempotencyMiddleware(
        app,
        idempotency=Idempotency("http", ttl=60),
        fingerprint_body=True,
    )
    # Act
    async with micro:
        sent = await _drive(middleware, [{"type": "http.disconnect"}])
    # Assert
    assert sent[0]["status"] == HTTP_200_OK


async def test_middleware_forwards_unknown_response_messages() -> None:
    """A message that is neither start nor body passes through untouched."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])

    async def app(
        scope: Scope,  # noqa: ARG001
        receive: Receive,  # noqa: ARG001
        send: Send,
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"trailer", b"expires")],
                "trailers": True,
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})
        await send(
            {
                "type": "http.response.trailers",
                "headers": [(b"expires", b"0")],
            }
        )

    middleware = IdempotencyMiddleware(
        app, idempotency=Idempotency("http", ttl=60)
    )
    # Act
    async with micro:
        sent = await _drive(
            middleware,
            [{"type": "http.request", "body": b"", "more_body": False}],
        )
    # Assert
    assert sent[-1]["type"] == "http.response.trailers"
    assert not any(
        message.get("headers")
        and any(
            name == b"idempotent-replayed" for name, _ in message["headers"]
        )
        for message in sent
    )


async def test_middleware_duplicate_in_flight_waits_and_replays() -> None:
    """A duplicate mid-flight waits for the first, then replays it."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    app = FastAPI()
    app.add_middleware(
        IdempotencyMiddleware, idempotency=Idempotency("http", ttl=60)
    )
    micro.install(app)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    @app.post("/slow")
    async def slow() -> dict[str, int]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"call": calls}

    async with micro:
        from httpx import ASGITransport, AsyncClient  # noqa: PLC0415

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            # Act
            first = asyncio.create_task(client.post("/slow", headers=KEY))
            await started.wait()
            second = asyncio.create_task(client.post("/slow", headers=KEY))
            await asyncio.sleep(0)
            release.set()
            first_response, second_response = await asyncio.gather(
                first, second
            )

    # Assert
    assert first_response.json() == {"call": 1}
    assert second_response.json() == {"call": 1}
    assert second_response.headers["idempotent-replayed"] == "true"
    assert calls == 1


async def test_middleware_duplicate_in_flight_times_out_with_conflict() -> None:
    """A duplicate past `wait_timeout` gets 409 instead of holding the socket."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    app = FastAPI()
    app.add_middleware(
        IdempotencyMiddleware,
        idempotency=Idempotency("http", ttl=60),
        wait_timeout=0.05,
    )
    micro.install(app)
    started = asyncio.Event()
    release = asyncio.Event()

    @app.post("/slow")
    async def slow() -> dict[str, str]:
        started.set()
        await release.wait()
        return {"status": "done"}

    async with micro:
        from httpx import ASGITransport, AsyncClient  # noqa: PLC0415

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            # Act
            first = asyncio.create_task(client.post("/slow", headers=KEY))
            await started.wait()
            second = await client.post("/slow", headers=KEY)
            release.set()
            await first

    # Assert
    assert second.status_code == HTTP_409_CONFLICT
    assert second.headers["retry-after"] == "1"
    assert second.headers["content-type"] == "application/problem+json"
    assert second.json()["type"].endswith("#idempotency-in-flight")


def _documented_app(**options: Any) -> FastAPI:  # noqa: ANN401
    """Build an app whose schema is annotated by `document_idempotency`."""
    app = build_app(**options)

    @app.post("/declared")
    async def declared(
        key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> dict[str, str]:
        return {"key": key}

    document_idempotency(app)
    return app


def test_document_idempotency_adds_the_header_and_responses() -> None:
    """A covered operation gains the header parameter and the responses."""
    # Arrange
    app = _documented_app(fingerprint_body=True)
    # Act
    operation = app.openapi()["paths"]["/charge"]["post"]
    # Assert
    assert ("Idempotency-Key", "header") in [
        (p["name"], p["in"]) for p in operation["parameters"]
    ]
    assert {"400", "409", "413", "422"} <= set(operation["responses"])


def test_document_idempotency_keeps_a_declaration_of_its_own() -> None:
    """A route that documents the marker itself is left with one entry."""
    # Arrange
    app = build_app()

    @app.post(
        "/documented",
        responses={
            200: {
                "headers": {
                    "idempotent-replayed": {"schema": {"type": "string"}}
                }
            }
        },
    )
    async def documented() -> dict[str, int]:
        return {"amount": 100}

    document_idempotency(app)

    # Act
    headers = app.openapi()["paths"]["/documented"]["post"]["responses"]["200"][
        "headers"
    ]

    # Assert
    assert list(headers) == ["idempotent-replayed"]


def test_document_idempotency_annotates_a_schema_once() -> None:
    """A second call over one schema must not read the first one's work."""
    # Arrange
    app = build_app()
    document_idempotency(app)
    document_idempotency(app)

    # Act
    responses = app.openapi()["paths"]["/charge"]["post"]["responses"]

    # Assert
    assert list(responses["200"]["headers"]) == ["Idempotent-Replayed"]
    # Declared once, whatever the number of wrappers over the schema.
    assert list(responses["409"]["headers"]) == ["Idempotent-Replayed"]


def test_document_idempotency_describes_every_installed_middleware() -> None:
    """Two sets of rules on one app, each with its own paths and headers."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    app = FastAPI()
    app.add_middleware(
        IdempotencyMiddleware,
        idempotency=Idempotency("a"),
        include=("/a/*",),
    )
    app.add_middleware(
        IdempotencyMiddleware,
        idempotency=Idempotency("b"),
        include=("/b/*",),
        key_header="X-Idempotency-Key",
        replay_header="X-Replayed",
    )

    @app.post("/a/charge")
    async def charge_a() -> dict[str, int]:
        return {"amount": 100}

    @app.post("/b/charge")
    async def charge_b() -> dict[str, int]:
        return {"amount": 100}

    micro.install(app)
    document_idempotency(app)

    # Act
    paths = app.openapi()["paths"]

    # Assert
    first = paths["/a/charge"]["post"]
    second = paths["/b/charge"]["post"]
    assert [p["name"] for p in first["parameters"]] == ["Idempotency-Key"]
    assert [p["name"] for p in second["parameters"]] == ["X-Idempotency-Key"]
    assert "Idempotent-Replayed" in first["responses"]["200"]["headers"]
    assert "X-Replayed" in second["responses"]["200"]["headers"]


def test_document_idempotency_marks_overlapping_rules_once_each() -> None:
    """Two middlewares on one path describe two markers, and no refusal."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    app = FastAPI()
    app.add_middleware(
        IdempotencyMiddleware,
        idempotency=Idempotency("a"),
        key_header="X-A-Key",
        replay_header="X-A",
    )
    app.add_middleware(
        IdempotencyMiddleware,
        idempotency=Idempotency("b"),
        key_header="X-B-Key",
        replay_header="X-B",
    )

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        return {"amount": 100}

    micro.install(app)
    document_idempotency(app)

    # Act
    responses = app.openapi()["paths"]["/charge"]["post"]["responses"]

    # Assert
    assert set(responses["200"]["headers"]) == {"X-A", "X-B"}
    # A refusal one answers is stored and replayed by the other above it.
    assert set(responses["409"]["headers"]) == {"X-A", "X-B"}


def test_document_idempotency_leaves_another_method_alone() -> None:
    """An `include` that matches a path under another verb is followed."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    app = FastAPI()
    app.add_middleware(
        IdempotencyMiddleware,
        idempotency=Idempotency("http"),
        include=("/payments/*",),
    )

    @app.post("/orders")
    async def order() -> dict[str, int]:
        return {"amount": 100}

    @app.get("/payments/{payment_id}")
    async def payment(payment_id: int) -> dict[str, int]:
        return {"amount": payment_id}

    micro.install(app)
    document_idempotency(app)

    # Act
    operation = app.openapi()["paths"]["/orders"]["post"]

    # Assert
    assert "parameters" not in operation
    assert "409" not in operation["responses"]


def test_document_idempotency_follows_an_exclude_that_empties_it() -> None:
    """A service naming its own routes is followed, not second-guessed."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    app = FastAPI()
    app.add_middleware(
        IdempotencyMiddleware,
        idempotency=Idempotency("http"),
        exclude=("/webhook",),
    )

    @app.post("/webhook")
    async def webhook() -> dict[str, int]:
        return {"amount": 100}

    micro.install(app)
    document_idempotency(app)

    # Act
    operation = app.openapi()["paths"]["/webhook"]["post"]

    # Assert
    assert "parameters" not in operation
    assert "409" not in operation["responses"]


def test_document_idempotency_reads_a_subclass_of_its_own() -> None:
    """A subclass may take keywords the middleware never declared."""

    # Arrange
    class TenantIdempotencyMiddleware(IdempotencyMiddleware):
        def __init__(
            self,
            app: Any,  # noqa: ANN401
            *,
            tenant_key: str,
            **options: Any,  # noqa: ANN401
        ) -> None:
            self.tenant_key = tenant_key
            super().__init__(app, **options)

    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    app = FastAPI()
    app.add_middleware(
        TenantIdempotencyMiddleware,
        idempotency=Idempotency("http"),
        tenant_key="acme",
    )

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        return {"amount": 100}

    micro.install(app)
    document_idempotency(app)

    # Act
    operation = app.openapi()["paths"]["/charge"]["post"]

    # Assert
    assert [p["name"] for p in operation["parameters"]] == ["Idempotency-Key"]


def test_document_idempotency_reads_a_subclass_holding_its_own_store() -> None:
    """A subclass may build the `Idempotency` rather than be handed one."""

    # Arrange
    class TenantIdempotencyMiddleware(IdempotencyMiddleware):
        def __init__(self, app: Any, **options: Any) -> None:  # noqa: ANN401
            super().__init__(app, idempotency=Idempotency("http"), **options)

    # The component names its own store, which this middleware is not.
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), IdempotentRequests()])
    app = FastAPI()
    app.add_middleware(TenantIdempotencyMiddleware)

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        return {"amount": 100}

    micro.install(app)

    # Act
    operation = app.openapi()["paths"]["/charge"]["post"]

    # Assert
    assert [p["name"] for p in operation["parameters"]] == ["Idempotency-Key"]


def test_document_idempotency_reads_a_subclass_default() -> None:
    """A subclass naming its own header is described under that name."""

    # Arrange
    class RenamedIdempotencyMiddleware(IdempotencyMiddleware):
        def __init__(
            self,
            app: Any,  # noqa: ANN401
            *,
            key_header: str = "X-Request-Key",
            **options: Any,  # noqa: ANN401
        ) -> None:
            super().__init__(app, key_header=key_header, **options)

    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    app = FastAPI()
    app.add_middleware(
        RenamedIdempotencyMiddleware, idempotency=Idempotency("http")
    )

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        return {"amount": 100}

    micro.install(app)
    document_idempotency(app)

    # Act
    operation = app.openapi()["paths"]["/charge"]["post"]

    # Assert
    assert [p["name"] for p in operation["parameters"]] == ["X-Request-Key"]


def test_a_root_path_shortens_only_a_whole_segment() -> None:
    """`/api` is a prefix of `/api/keys`, and not of `/apikeys`."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    app = FastAPI(root_path="/")
    app.add_middleware(
        IdempotencyMiddleware,
        idempotency=Idempotency("http"),
        include=("/charge",),
    )

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        return {"amount": 100}

    micro.install(app)

    # Act
    with TestClient(app) as client:
        client.post("/charge", headers=KEY)
        second = client.post("/charge", headers=KEY)

    # Assert
    assert second.headers["idempotent-replayed"] == "true"


def test_a_replay_from_a_nested_middleware_reaches_the_client() -> None:
    """The one below stores it, and the one above forwards what it says."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware, idempotency=Idempotency("inner"))
    app.add_middleware(
        IdempotencyMiddleware,
        idempotency=Idempotency("outer"),
        # The one above stores nothing, so only the one below replays.
        skip=lambda response: True,  # noqa: ARG005
    )

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        return {"amount": 100}

    micro.install(app)

    # Act
    with TestClient(app) as client:
        first = client.post("/charge", headers=KEY)
        second = client.post("/charge", headers=KEY)

    # Assert
    assert "idempotent-replayed" not in first.headers
    assert second.headers["idempotent-replayed"] == "true"


def test_a_mounted_app_selects_paths_by_its_own_routes() -> None:
    """A pattern is the route, not the prefix the mount adds to the wire."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    sub = FastAPI()
    sub.add_middleware(
        IdempotencyMiddleware,
        idempotency=Idempotency("http"),
        include=("/charge",),
    )

    @sub.post("/charge")
    async def charge() -> dict[str, int]:
        return {"amount": 100}

    micro.install(sub)
    document_idempotency(sub)
    root = FastAPI()
    root.mount("/sub", sub)

    # Act
    with TestClient(root) as client:
        first = client.post("/sub/charge", headers=KEY)
        second = client.post("/sub/charge", headers=KEY)
    operation = sub.openapi()["paths"]["/charge"]["post"]

    # Assert
    assert "idempotent-replayed" not in first.headers
    assert second.headers["idempotent-replayed"] == "true"
    assert [p["name"] for p in operation["parameters"]] == ["Idempotency-Key"]


def test_document_idempotency_describes_a_middleware_added_by_hand() -> None:
    """A hand-added middleware serves the component, so it is described."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), IdempotentRequests()])
    app = FastAPI()
    app.add_middleware(
        IdempotencyMiddleware, idempotency=Idempotency("by-hand")
    )

    @app.post("/charge")
    async def charge() -> dict[str, int]:
        return {"amount": 100}

    # `install` adds no middleware of its own where the app wired one.
    micro.install(app)

    # Act
    operation = app.openapi()["paths"]["/charge"]["post"]

    # Assert
    assert [p["name"] for p in operation["parameters"]] == ["Idempotency-Key"]
    assert "409" in operation["responses"]


def test_document_idempotency_describes_rules_sharing_one_store() -> None:
    """Two sets of rules may write through one `Idempotency`."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    app = FastAPI()
    shared = Idempotency("http")
    app.add_middleware(
        IdempotencyMiddleware,
        idempotency=shared,
        include=("/a/*",),
        key_header="X-A-Key",
    )
    app.add_middleware(
        IdempotencyMiddleware,
        idempotency=shared,
        include=("/b/*",),
        key_header="X-B-Key",
    )

    @app.post("/a/charge")
    async def charge_a() -> dict[str, int]:
        return {"amount": 100}

    @app.post("/b/charge")
    async def charge_b() -> dict[str, int]:
        return {"amount": 100}

    micro.install(app)
    document_idempotency(app)

    # Act
    paths = app.openapi()["paths"]

    # Assert
    assert [p["name"] for p in paths["/a/charge"]["post"]["parameters"]] == [
        "X-A-Key"
    ]
    assert [p["name"] for p in paths["/b/charge"]["post"]["parameters"]] == [
        "X-B-Key"
    ]


def test_document_idempotency_follows_the_path_selection() -> None:
    """An operation the middleware passes through is not described as covered."""
    # Arrange
    app = _documented_app(include=("/charge",))

    # Act
    paths = app.openapi()["paths"]

    # Assert
    covered = paths["/charge"]["post"]
    passed_through = paths["/declared"]["post"]
    assert "409" in covered["responses"]
    assert "409" not in passed_through["responses"]
    assert "headers" not in passed_through["responses"]["200"]


def test_document_idempotency_describes_the_replay_marker() -> None:
    """The name is a service's to pick, so the schema is where it is read."""
    # Arrange
    app = build_app(replay_header="X-Idempotent-Replayed")

    @app.post("/refuses", responses={400: {"description": "Refused."}})
    async def refuses() -> dict[str, int]:
        return {"amount": 100}

    document_idempotency(app)

    # Act
    responses = app.openapi()["paths"]["/refuses"]["post"]["responses"]

    # Assert
    header = responses["200"]["headers"]["X-Idempotent-Replayed"]
    assert header["schema"] == {"type": "string", "enum": ["true"]}
    # A stored error replays like any other response the app answers.
    assert "X-Idempotent-Replayed" in responses["400"]["headers"]


def test_document_idempotency_publishes_the_problem_body() -> None:
    """A client generated from the schema knows the body it will get."""
    # Arrange
    app = _documented_app(fingerprint_body=True)
    # Act
    schema = app.openapi()
    response = schema["paths"]["/charge"]["post"]["responses"]["409"]
    # Assert
    assert response["content"] == {
        "application/problem+json": {
            "schema": {"$ref": "#/components/schemas/ProblemDetail"}
        }
    }
    assert "ProblemDetail" in schema["components"]["schemas"]


def test_document_idempotency_never_points_at_someone_elses_model() -> None:
    """An app may publish a `ProblemDetail` of its own under that name.

    Pointing the middleware's responses at it would hand a generated client
    the wrong shape to decode, so grelmicro's goes beside it.
    """

    # Arrange
    class ProblemDetail(BaseModel):
        """A model of the app's own that happens to share the name."""

        mine: str

    app = build_app()

    @app.post("/declined", responses={402: {"model": ProblemDetail}})
    async def declined() -> dict[str, str]:
        return {"ok": "yes"}

    document_idempotency(app)

    # Act
    schema = app.openapi()
    schemas = schema["components"]["schemas"]
    content = schema["paths"]["/charge"]["post"]["responses"]["409"]["content"]

    # Assert
    assert schemas["ProblemDetail"]["properties"] == {
        "mine": {"type": "string", "title": "Mine"}
    }
    assert content["application/problem+json"]["schema"] == {
        "$ref": "#/components/schemas/GrelmicroProblemDetail"
    }
    assert "type" in schemas["GrelmicroProblemDetail"]["properties"]


def test_document_idempotency_says_nothing_when_both_names_are_taken() -> None:
    """Naming the wrong shape is worse than naming none.

    Taking both names is a deliberate act, so the responses are still
    described and simply carry no body schema.
    """

    # Arrange
    class ProblemDetail(BaseModel):
        """The app's own, under the plain name."""

        mine: str

    class GrelmicroProblemDetail(BaseModel):
        """The app's own, under the qualified name too."""

        also_mine: str

    app = build_app()

    @app.post(
        "/declined",
        responses={
            402: {"model": ProblemDetail},
            403: {"model": GrelmicroProblemDetail},
        },
    )
    async def declined() -> dict[str, str]:
        return {"ok": "yes"}

    document_idempotency(app)

    # Act
    response = app.openapi()["paths"]["/charge"]["post"]["responses"]["409"]

    # Assert
    assert "still in flight" in response["description"]
    assert "content" not in response


def test_document_idempotency_publishes_nothing_when_nothing_matched() -> None:
    """An app the middleware never covers gains no unreferenced component."""
    # Arrange
    app = build_app(methods=("PATCH",))
    document_idempotency(app)
    # Act
    schema = app.openapi()
    # Assert
    assert "ProblemDetail" not in schema.get("components", {}).get(
        "schemas", {}
    )


def test_document_idempotency_leaves_other_methods_alone() -> None:
    """A method the middleware ignores keeps its schema untouched."""
    # Arrange
    app = _documented_app()
    # Act
    operation = app.openapi()["paths"]["/read"]["get"]
    # Assert
    assert not [
        p for p in operation.get("parameters", []) if p["in"] == "header"
    ]
    assert "409" not in operation["responses"]


def test_document_idempotency_keeps_a_declared_header() -> None:
    """An operation that declares the header keeps its own declaration."""
    # Arrange
    app = _documented_app()
    # Act
    parameters = app.openapi()["paths"]["/declared"]["post"]["parameters"]
    headers = [p for p in parameters if p["in"] == "header"]
    # Assert
    assert len(headers) == 1
    assert headers[0]["required"] is True


def test_document_idempotency_keeps_the_validation_error_schema() -> None:
    """The auto-generated 422 keeps its schema and gains the description."""
    # Arrange
    app = _documented_app(fingerprint_body=True)
    # Act
    # `/declared` validates a required header, so FastAPI generates a 422.
    response = app.openapi()["paths"]["/declared"]["post"]["responses"]["422"]
    # Assert
    assert "application/json" in response["content"]
    assert "Validation Error" in response["description"]
    assert "different request payload" in response["description"]


def test_document_idempotency_is_stable_across_calls() -> None:
    """Rebuilding the schema does not duplicate the injected entries."""
    # Arrange
    app = _documented_app(fingerprint_body=True)
    # Act
    first = json.dumps(app.openapi())
    cached = json.dumps(app.openapi())  # served from the cache, annotated once
    app.openapi_schema = None
    rebuilt = json.dumps(app.openapi())  # regenerated from the routes
    # Assert
    assert cached == first
    assert rebuilt == first


def test_document_idempotency_marks_a_required_key() -> None:
    """`require_key` makes the documented parameter required."""
    # Arrange
    app = _documented_app(require_key=True)
    # Act
    parameters = app.openapi()["paths"]["/charge"]["post"]["parameters"]
    header = next(p for p in parameters if p["in"] == "header")
    # Assert
    assert header["required"] is True


def test_document_idempotency_rejects_an_app_without_the_middleware() -> None:
    """A clear error beats silently documenting nothing."""
    # Arrange
    app = FastAPI()
    # Act / Assert
    with pytest.raises(TypeError, match="no IdempotencyMiddleware"):
        document_idempotency(app)


def test_document_idempotency_rejects_a_plain_starlette_app() -> None:
    """Only FastAPI builds an OpenAPI schema to annotate."""
    # Arrange
    app = Starlette()
    # Act / Assert
    with pytest.raises(TypeError, match="needs a FastAPI app"):
        document_idempotency(app)  # ty: ignore[invalid-argument-type]


def test_document_idempotency_raises_without_fastapi() -> None:
    """`document_idempotency` reports the missing dependency.

    The module is put back exactly as it was, not reimported. A fresh
    import would mint a second `HealthzResponse` class, and every test that
    had already imported the first would then compare two classes of the
    same name that are not the same object.
    """
    # Arrange
    name = "grelmicro.integrations.fastapi"
    original = sys.modules.get(name)
    try:
        with patch.dict(sys.modules, {"fastapi": None}):
            sys.modules.pop(name, None)
            module = importlib.import_module(name)
            # Act / Assert
            with pytest.raises(DependencyNotFoundError):
                module.document_idempotency(None)  # ty: ignore[invalid-argument-type]
    finally:
        if original is not None:  # pragma: no branch
            sys.modules[name] = original
            # `import_module` also rebinds the submodule as an attribute of
            # its package, and restoring `sys.modules` does not undo that.
            # A later `from grelmicro.integrations import fastapi` would
            # otherwise reach the throwaway module and its duplicate classes.
            setattr(  # noqa: B010
                sys.modules["grelmicro.integrations"], "fastapi", original
            )


def test_document_idempotency_covers_a_custom_method() -> None:
    """A method the middleware covers is annotated, standard or not."""
    # Arrange
    app = build_app(methods=("POST", "PURGE"))

    async def purge() -> dict[str, bool]:
        return {"purged": True}

    app.add_api_route("/thing", purge, methods=["PURGE"])
    document_idempotency(app)
    # Act
    operation = app.openapi()["paths"]["/thing"]["purge"]
    # Assert
    assert [p["name"] for p in operation["parameters"]] == ["Idempotency-Key"]
    assert "409" in operation["responses"]


def test_document_idempotency_finds_a_subclass() -> None:
    """A subclass of the middleware is still the middleware."""

    # Arrange
    class TenantIdempotencyMiddleware(IdempotencyMiddleware):
        """A project's own subclass."""

    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    app = FastAPI()
    app.add_middleware(
        TenantIdempotencyMiddleware, idempotency=Idempotency("http", ttl=60)
    )
    micro.install(app)

    @app.post("/charge")
    async def charge() -> dict[str, bool]:
        return {"ok": True}

    document_idempotency(app)
    # Act
    operation = app.openapi()["paths"]["/charge"]["post"]
    # Assert
    assert [p["name"] for p in operation["parameters"]] == ["Idempotency-Key"]


def test_document_idempotency_ignores_a_callable_middleware() -> None:
    """A non-class middleware factory never breaks the lookup."""

    # Arrange
    def passthrough(app: Any) -> Any:  # noqa: ANN401
        return app

    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware, idempotency=Idempotency("http"))
    app.add_middleware(passthrough)
    micro.install(app)

    @app.post("/charge")
    async def charge() -> dict[str, bool]:
        return {"ok": True}

    document_idempotency(app)
    # Act
    operation = app.openapi()["paths"]["/charge"]["post"]
    # Assert
    assert [p["name"] for p in operation["parameters"]] == ["Idempotency-Key"]


def test_document_idempotency_gives_each_operation_its_own_parameter() -> None:
    """Editing one injected parameter never edits another operation's."""
    # Arrange
    app = _documented_app()
    schema = app.openapi()
    # Act
    charge = schema["paths"]["/charge"]["post"]["parameters"][-1]
    created = schema["paths"]["/created"]["post"]["parameters"][-1]
    charge["description"] = "edited"
    # Assert
    assert created["description"] != "edited"
