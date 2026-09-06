"""Tests for the response cache: what it answers, and what it refuses to keep."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from fastapi import Depends, FastAPI, Response
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import StreamingResponse
from starlette.routing import Mount, Route

from grelmicro import Grelmicro
from grelmicro.cache import Cache, JsonSerializer, TTLCache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.http import (
    CachedResponses,
    CachedResponsesMiddleware,
    StoredResponse,
)
from grelmicro.http._response_cache import _WARNED_LIMIT
from grelmicro.integrations.fastapi import CachedResponse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, MutableMapping

pytestmark = [pytest.mark.timeout(5)]

HTTP_200_OK = 200
HTTP_304_NOT_MODIFIED = 304
HTTP_404_NOT_FOUND = 404
TTL = 60.0
BIG = 2048
TWICE = 2
"""How many times the handler runs when nothing was stored."""


def _app(component: CachedResponses) -> FastAPI:
    """Return an app whose `/reads` route counts what reached the handler."""
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), component])
    app = FastAPI()
    app.state.calls = 0
    micro.install(app)

    @app.get("/reads", dependencies=[CachedResponse(ttl=TTL)])
    async def reads() -> dict[str, int]:
        app.state.calls += 1
        return {"calls": app.state.calls}

    @app.get("/live")
    async def live() -> dict[str, int]:
        app.state.calls += 1
        return {"calls": app.state.calls}

    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Serve an app carrying the component."""
    with TestClient(_app(CachedResponses())) as test_client:
        yield test_client


# --- What it answers ---


def test_a_marked_route_is_answered_from_the_cache(
    client: TestClient,
) -> None:
    """The second read never reaches the handler."""
    # Act
    first = client.get("/reads")
    second = client.get("/reads")

    # Assert
    assert first.json() == second.json() == {"calls": 1}
    assert "age" in second.headers


def test_a_hit_says_how_long_it_has_been_kept(client: TestClient) -> None:
    """`Age` is what a cache tells a client instead of a proprietary header."""
    # Arrange
    client.get("/reads")

    # Act
    response = client.get("/reads")

    # Assert
    assert response.headers["age"] == "0"


def test_an_unmarked_route_is_never_cached(client: TestClient) -> None:
    """Nothing is cached until a route asks to be."""
    # Act
    first = client.get("/live")
    second = client.get("/live")

    # Assert
    assert first.json() != second.json()


def test_a_stored_response_carries_an_entity_tag(client: TestClient) -> None:
    """A cache that answers `304` needs a tag, so it adds one."""
    # Act
    response = client.get("/reads")

    # Assert
    assert response.headers["etag"].startswith('"')


def test_a_client_holding_the_tag_is_answered_304(
    client: TestClient,
) -> None:
    """The body is what a `304` saves, and the cache has it to save."""
    # Arrange
    tag = client.get("/reads").headers["etag"]

    # Act
    response = client.get("/reads", headers={"If-None-Match": tag})

    # Assert
    assert response.status_code == HTTP_304_NOT_MODIFIED
    assert response.content == b""
    assert response.headers["etag"] == tag


def test_a_head_is_answered_from_the_read_that_was_stored(
    client: TestClient,
) -> None:
    """A `HEAD` carries the headers of the `GET`, and none of its body."""
    # Arrange
    client.get("/reads")

    # Act
    response = client.head("/reads")

    # Assert
    assert response.status_code == HTTP_200_OK
    assert response.content == b""
    assert "age" in response.headers


def test_a_head_never_fills_the_cache() -> None:
    """A `HEAD` body is empty, and would answer the `GET` after it with nothing."""
    # Arrange
    calls = 0

    async def handler(request: Any) -> Response:  # noqa: ANN401, ARG001
        nonlocal calls
        calls += 1
        return Response(b"ok")

    app = Starlette(routes=[Route("/reads", handler)])
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(paths={"/reads": TTL}),
        ]
    )
    micro.install(app)

    # Act
    with TestClient(app) as client:
        head = client.head("/reads")
        read = client.get("/reads")

    # Assert
    assert head.status_code == HTTP_200_OK
    assert calls == TWICE
    assert read.content == b"ok"


def test_a_marked_route_takes_the_components_ttl_when_it_names_none() -> None:
    """`CachedResponse()` with no `ttl` is the component's."""
    # Arrange
    micro = Grelmicro(
        uses=[Cache(MemoryCacheAdapter()), CachedResponses(ttl=TTL)]
    )
    app = FastAPI()
    calls = 0
    micro.install(app)

    @app.get("/reads", dependencies=[CachedResponse()])
    async def reads() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    # Act
    with TestClient(app) as client:
        client.get("/reads")
        client.get("/reads")

    # Assert
    assert calls == 1


# --- What never reads the cache ---


@pytest.mark.parametrize(
    "header", ["Authorization", "Cookie"], ids=["authorization", "cookie"]
)
def test_a_credentialed_request_neither_reads_nor_fills(
    client: TestClient, header: str
) -> None:
    """A response authorized for one caller is never handed to another."""
    # Act
    first = client.get("/reads", headers={header: "secret"})
    second = client.get("/reads", headers={header: "secret"})

    # Assert
    assert first.json() != second.json()


def test_no_store_leaves_the_cache_out_of_it(client: TestClient) -> None:
    """A client asking for the handler gets the handler."""
    # Arrange
    client.get("/reads")

    # Act
    response = client.get("/reads", headers={"Cache-Control": "no-store"})

    # Assert
    assert response.json() == {"calls": 2}


def test_no_cache_asks_for_a_fresh_answer(client: TestClient) -> None:
    """`no-cache` skips the lookup, and the fresh answer is kept."""
    # Arrange
    client.get("/reads")

    # Act
    fresh = client.get("/reads", headers={"Cache-Control": "no-cache"})
    after = client.get("/reads")

    # Assert
    assert fresh.json() == {"calls": 2}
    assert after.json() == {"calls": 2}


def test_a_path_that_is_excluded_is_never_cached() -> None:
    """`exclude` wins over the mark the route carries."""
    # Arrange
    app = _app(CachedResponses(exclude=("/reads",)))

    # Act
    with TestClient(app) as client:
        first = client.get("/reads")
        second = client.get("/reads")

    # Assert
    assert first.json() != second.json()


def test_a_websocket_scope_passes_through() -> None:
    """A response cache is about responses, and answers nothing else."""
    # Arrange
    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        seen.append(scope["type"])

    middleware = CachedResponsesMiddleware(app, cache=_cache())

    # Act
    anyio.run(middleware, {"type": "websocket"}, _receive, _nowhere)

    # Assert
    assert seen == ["websocket"]


# --- Naming paths instead of routes ---


def test_paths_cache_a_route_that_carries_no_mark() -> None:
    """A router whose handlers you cannot mark is named by its URL."""
    # Arrange
    app = _app(CachedResponses(paths={"/live": TTL}))

    # Act
    with TestClient(app) as client:
        first = client.get("/live")
        second = client.get("/live")

    # Assert
    assert first.json() == second.json()


def test_a_prefix_pattern_covers_the_router_under_it() -> None:
    """`"/x/*"` is the same matching every grelmicro middleware uses."""
    # Arrange
    calls = 0

    async def handler(request: Any) -> Response:  # noqa: ANN401, ARG001
        nonlocal calls
        calls += 1
        return Response(b"ok")

    app = Starlette(routes=[Route("/shop/items", handler)])
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(paths={"/shop/*": TTL}),
        ]
    )
    micro.install(app)

    # Act
    with TestClient(app) as client:
        client.get("/shop/items")
        client.get("/shop/items")

    # Assert
    assert calls == 1


def test_a_mounted_route_is_read_with_its_prefix() -> None:
    """A rule under a mount names the path the request actually asks for."""
    # Arrange
    calls = 0
    inner = FastAPI()

    @inner.get("/items", dependencies=[CachedResponse(ttl=TTL)])
    async def items() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    app = FastAPI()
    app.mount("/shop", inner)
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    micro.install(app)

    # Act
    with TestClient(app) as client:
        client.get("/shop/items")
        client.get("/shop/items")

    # Assert
    assert calls == 1


def test_a_route_with_other_dependencies_is_read_too() -> None:
    """The declaration sits beside whatever else the route depends on."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    app = FastAPI()
    micro.install(app)
    calls = 0

    async def audit() -> None:
        """Stand in for the gate a route already declares."""

    @app.get(
        "/reads",
        dependencies=[Depends(audit), CachedResponse(ttl=TTL)],
    )
    async def reads() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    # Act
    with TestClient(app) as client:
        client.get("/reads")
        client.get("/reads")

    # Assert
    assert calls == 1


def test_a_declaration_on_a_write_is_refused_where_it_is_written() -> None:
    """A method that changes something reaches the handler every time."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    app = FastAPI()

    @app.post("/orders", dependencies=[CachedResponse(ttl=TTL)])
    async def create() -> dict[str, int]:
        return {"id": 1}

    # Act / Assert
    with pytest.raises(TypeError, match="answers POST"):
        micro.install(app)


def test_a_route_added_after_install_is_read_when_the_app_starts() -> None:
    """`install` goes before the routes, and the app start catches up."""
    # Arrange
    micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
    app = FastAPI()
    micro.install(app)
    calls = 0

    @app.get("/late", dependencies=[CachedResponse(ttl=TTL)])
    async def late() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"calls": calls}

    # Act
    with TestClient(app) as client:
        client.get("/late")
        client.get("/late")

    # Assert
    assert calls == 1


# --- What is not stored ---


async def _nothing(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
    """Stand in for an app the test never reaches."""


async def _receive() -> MutableMapping[str, Any]:
    """Stand in for the body a read never sends."""
    return {"type": "http.request", "body": b"", "more_body": False}


async def _nowhere(message: MutableMapping[str, Any]) -> None:
    """Take a response nowhere."""


def _read_scope() -> MutableMapping[str, Any]:
    """Return the scope of a plain read of `/reads`."""
    return {
        "type": "http",
        "method": "GET",
        "path": "/reads",
        "headers": [],
        "query_string": b"",
    }


def _cache() -> TTLCache[Any]:
    """Return a cache over a backend of this test's own."""
    return TTLCache(
        ttl=TTL, backend=MemoryCacheAdapter(), serializer=JsonSerializer()
    )


def _ran_twice(headers: list[tuple[bytes, bytes]]) -> int:
    """Return how many times the app ran for two identical reads."""
    calls = 0

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        nonlocal calls
        calls += 1
        await send(
            {"type": "http.response.start", "status": 200, "headers": headers}
        )
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message: MutableMapping[str, Any]) -> None:
        """Take the response nowhere. What is measured is the app running."""

    async def scenario() -> None:
        async with MemoryCacheAdapter() as backend:
            middleware = CachedResponsesMiddleware(
                app,
                cache=TTLCache(
                    ttl=TTL, backend=backend, serializer=JsonSerializer()
                ),
                paths={"/reads": TTL},
            )
            scope = {
                "type": "http",
                "method": "GET",
                "path": "/reads",
                "headers": [],
                "query_string": b"",
            }
            await middleware(scope, _receive, send)
            await middleware(dict(scope), _receive, send)

    anyio.run(scenario)
    return calls


def _served_twice(
    handler: Any,  # noqa: ANN401
    component: CachedResponses | None = None,
) -> tuple[Any, Any]:
    """Return both answers to the same read of one handler."""
    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            component if component is not None else CachedResponses(),
        ]
    )
    app = FastAPI()
    micro.install(app)
    app.add_api_route(
        "/reads",
        handler,
        methods=["GET"],
        dependencies=[CachedResponse(ttl=TTL)],
    )
    with TestClient(app) as client:
        return client.get("/reads"), client.get("/reads")


def test_a_failure_is_not_stored() -> None:
    """Only `200` is an answer worth handing to somebody else."""
    # Arrange
    calls = 0

    async def handler() -> Response:
        nonlocal calls
        calls += 1
        return Response(b"gone", status_code=HTTP_404_NOT_FOUND)

    # Act
    _served_twice(handler)

    # Assert
    assert calls == TWICE


@pytest.mark.parametrize(
    "headers",
    [
        {"Set-Cookie": "session=1"},
        {"Cache-Control": "private"},
        {"Cache-Control": "no-store"},
        {"Cache-Control": "max-age=0, no-cache"},
    ],
    ids=["set-cookie", "private", "no-store", "no-cache"],
)
def test_a_response_that_refuses_the_cache_is_not_stored(
    headers: dict[str, str],
) -> None:
    """Each of these says this response is not one to hand on."""
    # Arrange
    calls = 0

    async def handler() -> Response:
        nonlocal calls
        calls += 1
        return Response(b"ok", headers=headers)

    # Act
    _served_twice(handler)

    # Assert
    assert calls == TWICE


def test_a_compressed_response_is_not_stored() -> None:
    """Compression happens outside this middleware, so what it sees is plain."""
    # Act
    ran = _ran_twice([(b"content-encoding", b"gzip")])

    # Assert
    assert ran == TWICE


def test_a_plain_response_is_stored() -> None:
    """The same harness, to show what the refusals are measured against."""
    # Act
    ran = _ran_twice([])

    # Assert
    assert ran == 1


def test_a_vary_naming_an_undeclared_header_is_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """This is how a naive cache answers one client with another's response."""
    # Arrange
    calls = 0

    async def handler() -> Response:
        nonlocal calls
        calls += 1
        return Response(b"ok", headers={"Vary": "Accept-Language"})

    # Act
    with caplog.at_level(logging.WARNING, logger="grelmicro.http.cache"):
        _served_twice(handler)

    # Assert
    assert calls == TWICE
    assert "Vary" in caplog.text


def test_a_vary_of_everything_is_refused() -> None:
    """`Vary: *` says no two requests share an answer."""
    # Arrange
    calls = 0

    async def handler() -> Response:
        nonlocal calls
        calls += 1
        return Response(b"ok", headers={"Vary": "*"})

    # Act
    _served_twice(handler)

    # Assert
    assert calls == TWICE


def test_a_declared_vary_is_stored_and_keyed_by_its_header() -> None:
    """One language never answers a request for another."""
    # Arrange
    calls = 0

    async def handler() -> Response:
        nonlocal calls
        calls += 1
        return Response(b"ok", headers={"Vary": "Accept-Language"})

    micro = Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(vary_by_headers=("accept-language",)),
        ]
    )
    app = FastAPI()
    micro.install(app)
    app.add_api_route(
        "/reads",
        handler,
        methods=["GET"],
        dependencies=[CachedResponse(ttl=TTL)],
    )

    # Act
    with TestClient(app) as client:
        client.get("/reads", headers={"Accept-Language": "fr"})
        client.get("/reads", headers={"Accept-Language": "fr"})
        client.get("/reads", headers={"Accept-Language": "de"})

    # Assert
    assert calls == TWICE


def test_a_response_the_skip_rule_refuses_is_not_stored() -> None:
    """A route's own rule is the last word on what is kept."""
    # Arrange
    calls = 0
    seen: list[StoredResponse] = []

    async def handler() -> Response:
        nonlocal calls
        calls += 1
        return Response(b"ok")

    def skip(response: StoredResponse) -> bool:
        seen.append(response)
        return True

    # Act
    _served_twice(handler, CachedResponses(skip=skip))

    # Assert
    assert calls == TWICE
    assert seen[0]["status"] == HTTP_200_OK
    assert seen[0]["body"] == b"ok"


def test_a_body_over_the_limit_is_streamed_and_not_stored() -> None:
    """A large download is never held in memory to keep it."""
    # Arrange
    calls = 0

    async def handler() -> Response:
        nonlocal calls
        calls += 1
        return Response(b"x" * BIG)

    # Act
    first, _ = _served_twice(handler, CachedResponses(max_body_size=16))

    # Assert
    assert calls == TWICE
    assert len(first.content) == BIG


def test_a_streamed_response_is_forwarded_as_it_comes() -> None:
    """Holding a stream to keep it would turn it into one message at the end."""
    # Arrange
    calls = 0

    async def chunks() -> AsyncIterator[bytes]:
        yield b"one"
        yield b"two"

    async def handler() -> Response:
        nonlocal calls
        calls += 1
        return StreamingResponse(chunks())

    # Act
    first, _ = _served_twice(handler)

    # Assert
    assert calls == TWICE
    assert first.content == b"onetwo"


def test_a_response_the_app_never_finished_is_released() -> None:
    """What the app sent goes out, and nothing half-written is stored."""
    # Arrange
    sent: list[MutableMapping[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    middleware = CachedResponsesMiddleware(
        app, cache=_cache(), paths={"/reads": TTL}
    )

    # Act
    anyio.run(middleware, _read_scope(), _receive, send)

    # Assert
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
    ]


def test_a_response_declaring_trailers_is_forwarded() -> None:
    """Trailers follow the body, so the response cannot be held and reordered."""
    # Arrange
    sent: list[MutableMapping[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [],
                "trailers": True,
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})
        await send({"type": "http.response.trailers", "headers": []})

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    middleware = CachedResponsesMiddleware(
        app, cache=_cache(), paths={"/reads": TTL}
    )

    # Act
    anyio.run(middleware, _read_scope(), _receive, send)

    # Assert
    assert sent[-1]["type"] == "http.response.trailers"


# --- The key ---


def test_the_query_string_is_part_of_the_key(client: TestClient) -> None:
    """Two different questions are two different answers."""
    # Act
    first = client.get("/reads?page=1")
    second = client.get("/reads?page=2")

    # Assert
    assert first.json() != second.json()


def test_the_order_of_the_query_string_is_not(client: TestClient) -> None:
    """The same question asked in another order is the same question."""
    # Act
    first = client.get("/reads?a=1&b=2")
    second = client.get("/reads?b=2&a=1")

    # Assert
    assert first.json() == second.json()


def test_only_the_named_query_parameters_are_read() -> None:
    """A tracking parameter is not a different resource."""
    # Arrange
    app = _app(CachedResponses(vary_by_query=("page",)))

    # Act
    with TestClient(app) as client:
        first = client.get("/reads?page=1&utm=ad")
        second = client.get("/reads?page=1&utm=mail")

    # Assert
    assert first.json() == second.json()


def test_a_key_builder_replaces_the_key() -> None:
    """A service that knows what makes two requests the same says so."""
    # Arrange
    app = _app(CachedResponses(key=lambda scope: scope["path"]))

    # Act
    with TestClient(app) as client:
        first = client.get("/reads?page=1")
        second = client.get("/reads?page=2")

    # Assert
    assert first.json() == second.json()


def test_a_key_builder_that_returns_none_leaves_the_request_alone() -> None:
    """`None` is how a builder says this one is not cached."""
    # Arrange
    app = _app(CachedResponses(key=lambda _scope: None))

    # Act
    with TestClient(app) as client:
        first = client.get("/reads")
        second = client.get("/reads")

    # Assert
    assert first.json() != second.json()


# --- Folding and purging ---


def test_one_cold_key_runs_the_handler_once() -> None:
    """A cold key must not fan the same computation out to every caller."""
    # Arrange
    calls = 0
    started = anyio.Event()

    async def scenario() -> int:
        from httpx import ASGITransport, AsyncClient  # noqa: PLC0415

        micro = Grelmicro(uses=[Cache(MemoryCacheAdapter()), CachedResponses()])
        app = FastAPI()
        micro.install(app)

        @app.get("/reads", dependencies=[CachedResponse(ttl=TTL)])
        async def reads() -> dict[str, int]:
            nonlocal calls
            calls += 1
            started.set()
            await anyio.sleep(0.05)
            return {"calls": calls}

        async with micro:
            transport = ASGITransport(app=app)
            async with (
                AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client,
                anyio.create_task_group() as tasks,
            ):
                for _ in range(5):
                    tasks.start_soon(client.get, "/reads")
        return calls

    # Act
    ran = anyio.run(scenario)

    # Assert
    assert ran == 1


def test_purge_drops_what_the_component_stored() -> None:
    """A write invalidates what the cache would go on answering with."""
    # Arrange
    component = CachedResponses()
    app = _app(component)

    @app.post("/purge")
    async def purge() -> dict[str, bool]:
        await component.purge()
        return {"purged": True}

    # Act
    with TestClient(app) as client:
        first = client.get("/reads")
        client.post("/purge")
        second = client.get("/reads")

    # Assert
    assert first.json() != second.json()


def test_a_response_that_carries_its_own_tag_keeps_it() -> None:
    """A tag the handler set is the resource's, and this adds none over it."""
    # Arrange
    tag = '"seven"'

    async def handler() -> Response:
        return Response(b"ok", headers={"ETag": tag})

    # Act
    first, second = _served_twice(handler)

    # Assert
    assert first.headers["etag"] == second.headers["etag"] == tag


def test_a_path_no_pattern_names_is_left_alone() -> None:
    """A rule that names another path decides nothing about this one."""
    # Arrange
    app = _app(CachedResponses(paths={"/elsewhere": TTL}))

    # Act
    with TestClient(app) as client:
        first = client.get("/live")
        second = client.get("/live")

    # Assert
    assert first.json() != second.json()


def test_a_component_that_read_no_app_starts_anyway() -> None:
    """A framework declaring no routes grelmicro can read still runs."""

    # Arrange
    async def scenario() -> str:
        async with CachedResponses() as component:
            return component.name

    # Act / Assert
    assert anyio.run(scenario) == "default"


def test_something_that_is_not_a_route_is_passed_over() -> None:
    """What a router holds is the router's business, not this one's."""
    # Arrange
    component = CachedResponses()

    # Act
    component.read_routes(SimpleNamespace(routes=[object()]))

    # Assert
    assert component.name == "default"


def test_a_mount_of_nothing_carries_no_handler_to_read() -> None:
    """A route with no endpoint has no mark on it."""
    # Arrange
    component = CachedResponses()

    # Act
    component.read_routes(Starlette(routes=[Mount("/empty", routes=[])]))

    # Assert
    assert component.name == "default"


def test_an_app_that_answers_nothing_is_forwarded_as_it_is() -> None:
    """Nothing to hold means nothing to send, and no entry either."""
    # Arrange
    sent: list[MutableMapping[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        """Return without answering, as a mounted app that matched nothing."""

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    middleware = CachedResponsesMiddleware(
        app, cache=_cache(), paths={"/reads": TTL}
    )

    # Act
    anyio.run(middleware, _read_scope(), _receive, send)

    # Assert
    assert sent == []


def test_a_message_that_is_neither_start_nor_body_releases_what_is_held() -> (
    None
):
    """Whatever is held goes first, so nothing reaches the client out of order."""
    # Arrange
    sent: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send({"type": "http.response.trailers", "headers": []})

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message["type"])

    middleware = CachedResponsesMiddleware(
        app, cache=_cache(), paths={"/reads": TTL}
    )

    # Act
    anyio.run(middleware, _read_scope(), _receive, send)

    # Assert
    assert sent == ["http.response.start", "http.response.trailers"]


def test_the_vary_warning_stops_remembering_what_it_warned_about() -> None:
    """A service with many paths must not grow a set of them forever."""
    # Arrange
    middleware = CachedResponsesMiddleware(_nothing, cache=_cache())
    headers = {"vary": "accept-language"}

    # Act
    for index in range(_WARNED_LIMIT + 2):
        middleware._storable(headers, path=f"/p{index}")

    # Assert
    assert len(middleware._warned) <= _WARNED_LIMIT


def test_the_cache_it_stores_in_is_readable() -> None:
    """An operator asking what is kept reaches the store the component holds."""
    # Arrange
    own = _cache()

    # Act
    component = CachedResponses(cache=own)

    # Assert
    assert component.cache is own
    assert component.name == "default"
