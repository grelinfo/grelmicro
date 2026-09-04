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
from fastapi import FastAPI, Header, Request, Response
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.background import BackgroundTasks
from starlette.middleware.base import (
    BaseHTTPMiddleware,
    RequestResponseEndpoint,
)
from starlette.responses import StreamingResponse
from starlette.status import (
    HTTP_200_OK,
    HTTP_201_CREATED,
    HTTP_204_NO_CONTENT,
    HTTP_400_BAD_REQUEST,
    HTTP_409_CONFLICT,
    HTTP_413_CONTENT_TOO_LARGE,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_502_BAD_GATEWAY,
)

from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.errors import DependencyNotFoundError, OutOfContextError
from grelmicro.http import IdempotencyMiddleware
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
    # What the middleware answers before the app runs never replays.
    assert "headers" not in responses["409"]


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
