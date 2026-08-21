"""Tests for conditional requests: entity tags, preconditions, and `304`."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import anyio
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.applications import Starlette

from grelmicro import Grelmicro
from grelmicro.errors import OutOfContextError
from grelmicro.http import (
    ConditionalRequests,
    ConditionalRequestsMiddleware,
    ErrorResponses,
    PreconditionFailedError,
    check_freshness,
    check_precondition,
    etag_of,
)
from grelmicro.http._paths import selects
from grelmicro.integrations.fastapi import (
    Conditional,
    ConditionalRequired,
    document_conditional_requests,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = [pytest.mark.timeout(5)]

HTTP_200_OK = 200
BACKGROUND_WORK = 0.2
HTTP_204_NO_CONTENT = 204
HTTP_404_NOT_FOUND = 404
HTTP_304_NOT_MODIFIED = 304
HTTP_412_PRECONDITION_FAILED = 412
HTTP_428_PRECONDITION_REQUIRED = 428
FIRST_VERSION = 1
VERSION = 3
"""The version the cart carries before any write."""
NEXT_VERSION = 4
"""The version a write moves it to."""


_JSON_A = b'{"a":1}'
"""The canonical JSON of `{"a": 1}`: sorted, compact, no spaces."""


class Cart(BaseModel):
    """A resource with a version column, like a row would have."""

    id: int
    items: list[str]
    version: int


# --- Entity tags ---------------------------------------------------------


def test_a_version_becomes_the_entity_tag() -> None:
    """A version already identifies the version, so it is not hashed."""
    # Assert
    assert etag_of(7) == '"7"'
    assert etag_of("v7") == '"v7"'
    assert etag_of(UUID(int=1)) == '"00000000-0000-0000-0000-000000000001"'


def test_a_datetime_version_renders_as_iso() -> None:
    """An `updated_at` column is a version too."""
    # Arrange
    updated_at = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)

    # Assert
    assert etag_of(updated_at) == '"2026-08-20T10:00:00+00:00"'


def test_a_representation_is_hashed() -> None:
    """Bytes and JSON data become a SHA-256 of what they hold."""
    # Assert
    assert etag_of(b"hello") == f'"{_sha256(b"hello")}"'
    assert etag_of({"a": 1}) == f'"{_sha256(_JSON_A)}"'


def test_a_pydantic_model_is_hashed_through_json_mode() -> None:
    """A model renders its own UUIDs and datetimes, then hashes as JSON."""
    # Arrange
    cart = Cart(id=1, items=["apple"], version=3)
    expected = json.dumps(
        cart.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()

    # Assert
    assert etag_of(cart) == f'"{_sha256(expected)}"'


def test_the_serialization_is_canonical() -> None:
    """Two dicts built in different orders are one representation.

    Key order is insertion order in Python, so without sorting the same
    resource would carry two entity tags depending on how it was built.
    """
    # Assert
    assert etag_of({"a": 1, "b": 2}) == etag_of({"b": 2, "a": 1})


def test_a_weak_tag_carries_its_marker() -> None:
    """`W/` is what tells a client this tag is not byte for byte."""
    # Assert
    assert etag_of(7, weak=True) == 'W/"7"'


def test_a_version_that_cannot_be_a_tag_is_refused() -> None:
    """An entity tag carries no quote, so a version holding one cannot pass."""
    # Act / Assert
    with pytest.raises(ValueError, match="entity tag"):
        etag_of('has "quotes"')


def test_a_bool_is_refused() -> None:
    """`True` is a mistake worth naming, not the entity tag `"True"`."""
    # Act / Assert
    with pytest.raises(TypeError, match="not a bool"):
        etag_of(value=True)


def test_something_that_is_not_json_is_refused() -> None:
    """A tag has to come from data, not from a repr."""
    # Act / Assert
    with pytest.raises(TypeError, match="JSON data"):
        etag_of(object())


def _sha256(payload: bytes) -> str:
    """Return the digest the entity tag should carry."""
    import hashlib  # noqa: PLC0415

    return hashlib.sha256(payload).hexdigest()


# --- The write path ------------------------------------------------------


def _cart_app(*components: Any, version: int = 3) -> FastAPI:  # noqa: ANN401
    """Build an app whose PUT is guarded by a precondition."""
    state = {"version": version}
    micro = Grelmicro(uses=[ErrorResponses(), *components])
    app = FastAPI()

    @app.get("/carts/1")
    async def read() -> Cart:
        return Cart(id=1, items=["apple"], version=state["version"])

    @app.put("/carts/1")
    async def replace() -> Cart:
        check_precondition(state["version"])
        state["version"] += 1
        return Cart(id=1, items=["pear"], version=state["version"])

    @app.put("/carts/2")
    async def create() -> Cart:
        # Nothing stored yet, which is what `If-None-Match: *` asks about.
        check_precondition(None)
        return Cart(id=2, items=[], version=1)

    micro.install(app)
    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Serve an app carrying the component."""
    with TestClient(_cart_app(ConditionalRequests())) as test_client:
        yield test_client


def test_a_matching_precondition_lets_the_write_through(
    client: TestClient,
) -> None:
    """The client holds the current version, so the write is safe."""
    # Act
    response = client.put("/carts/1", headers={"If-Match": '"3"'})

    # Assert
    assert response.status_code == HTTP_200_OK
    assert response.json()["version"] == NEXT_VERSION


def test_a_stale_precondition_is_refused(client: TestClient) -> None:
    """Another writer landed, so this write would lose their change."""
    # Act
    response = client.put("/carts/1", headers={"If-Match": '"2"'})

    # Assert
    assert response.status_code == HTTP_412_PRECONDITION_FAILED
    assert response.json()["type"].endswith("#precondition-failed")
    assert response.headers["content-type"] == "application/problem+json"


def test_a_weak_tag_never_matches_a_write(client: TestClient) -> None:
    """`If-Match` takes strong comparison, so `W/` cannot authorize a write."""
    # Act
    response = client.put("/carts/1", headers={"If-Match": 'W/"3"'})

    # Assert
    assert response.status_code == HTTP_412_PRECONDITION_FAILED


def test_the_wildcard_matches_a_resource_that_exists(
    client: TestClient,
) -> None:
    """`If-Match: *` asks only that there is something to update."""
    # Act
    response = client.put("/carts/1", headers={"If-Match": "*"})

    # Assert
    assert response.status_code == HTTP_200_OK


def test_a_write_without_a_precondition_is_told_to_send_one(
    client: TestClient,
) -> None:
    """`428` is the status that exists for the lost update problem."""
    # Act
    response = client.put("/carts/1")

    # Assert
    assert response.status_code == HTTP_428_PRECONDITION_REQUIRED
    assert response.json()["type"].endswith("#precondition-required")


def test_create_if_absent_passes_when_nothing_is_there(
    client: TestClient,
) -> None:
    """`If-None-Match: *` is how a client creates without overwriting."""
    # Act
    response = client.put("/carts/2", headers={"If-None-Match": "*"})

    # Assert
    assert response.status_code == HTTP_200_OK


def test_create_if_absent_is_refused_when_it_exists(
    client: TestClient,
) -> None:
    """The resource is there, so the create would overwrite it."""
    # Act
    response = client.put("/carts/1", headers={"If-None-Match": "*"})

    # Assert
    assert response.status_code == HTTP_412_PRECONDITION_FAILED


def test_the_middleware_can_refuse_before_the_handler_runs() -> None:
    """`require_precondition` turns away an unconditional write at the edge."""
    # Arrange
    app = _cart_app(ConditionalRequests(require_precondition=("PUT",)))

    # Act
    with TestClient(app) as client:
        response = client.put("/carts/1")

    # Assert
    assert response.status_code == HTTP_428_PRECONDITION_REQUIRED


def test_enforcement_leaves_reads_alone() -> None:
    """A read changes nothing, so there is nothing to lose an update on."""
    # Arrange
    app = _cart_app(
        ConditionalRequests(require_precondition=("PUT", "PATCH", "DELETE"))
    )

    # Act
    with TestClient(app) as client:
        response = client.get("/carts/1")

    # Assert
    assert response.status_code == HTTP_200_OK


def test_enforcement_still_allows_a_create() -> None:
    """`If-None-Match: *` is a precondition too, and it is how a client creates."""
    # Arrange
    app = _cart_app(ConditionalRequests(require_precondition=("PUT",)))

    # Act
    with TestClient(app) as client:
        response = client.put("/carts/2", headers={"If-None-Match": "*"})

    # Assert
    assert response.status_code == HTTP_200_OK


def test_an_excluded_path_is_left_alone() -> None:
    """A path the middleware skips binds nothing, so the guard says so."""
    # Arrange
    app = _cart_app(ConditionalRequests(exclude=("/carts/*",)))

    # Act / Assert
    with (
        TestClient(app, raise_server_exceptions=True) as client,
        pytest.raises(OutOfContextError, match="ConditionalRequests"),
    ):
        client.put("/carts/1", headers={"If-Match": '"3"'})


def test_the_guard_needs_the_component() -> None:
    """Without the middleware there is no request to read."""
    # Arrange
    app = _cart_app()

    # Act / Assert
    with (
        TestClient(app, raise_server_exceptions=True) as client,
        pytest.raises(OutOfContextError, match="check_precondition"),
    ):
        client.put("/carts/1", headers={"If-Match": '"3"'})


def test_the_error_is_raisable_by_the_application() -> None:
    """A conditional UPDATE that changed no row answers the same `412`."""
    # Arrange
    micro = Grelmicro(uses=[ErrorResponses(), ConditionalRequests()])
    app = FastAPI()

    @app.put("/carts/1")
    async def replace() -> dict[str, int]:
        raise PreconditionFailedError

    micro.install(app)

    # Act
    with TestClient(app) as client:
        response = client.put("/carts/1")

    # Assert
    assert response.status_code == HTTP_412_PRECONDITION_FAILED
    assert response.json()["type"].endswith("#precondition-failed")


# --- The read path -------------------------------------------------------


def test_a_read_carries_an_entity_tag(client: TestClient) -> None:
    """The client needs a tag before it can send one back."""
    # Act
    response = client.get("/carts/1")

    # Assert
    assert response.headers["etag"] == etag_of(response.content)


def test_a_read_that_already_holds_it_gets_304(client: TestClient) -> None:
    """The body is the one the client has, so it is not sent again."""
    # Arrange
    etag = client.get("/carts/1").headers["etag"]

    # Act
    response = client.get("/carts/1", headers={"If-None-Match": etag})

    # Assert
    assert response.status_code == HTTP_304_NOT_MODIFIED
    assert response.content == b""
    assert response.headers["etag"] == etag
    assert "content-length" not in response.headers


def test_a_weak_tag_still_answers_304(client: TestClient) -> None:
    """`If-None-Match` takes weak comparison, so `W/` is the same tag."""
    # Arrange
    etag = client.get("/carts/1").headers["etag"]

    # Act
    response = client.get("/carts/1", headers={"If-None-Match": f"W/{etag}"})

    # Assert
    assert response.status_code == HTTP_304_NOT_MODIFIED


def test_a_stale_tag_gets_the_body(client: TestClient) -> None:
    """The representation moved on, so the client gets the new one."""
    # Act
    response = client.get("/carts/1", headers={"If-None-Match": '"gone"'})

    # Assert
    assert response.status_code == HTTP_200_OK
    assert response.json()["version"] == VERSION


def test_a_write_never_answers_304(client: TestClient) -> None:
    """A `304` says nothing changed, which a write cannot claim."""
    # Arrange
    etag = etag_of(
        Cart(id=1, items=["pear"], version=4).model_dump_json().encode()
    )

    # Act
    response = client.put(
        "/carts/1", headers={"If-Match": '"3"', "If-None-Match": etag}
    )

    # Assert
    assert response.status_code == HTTP_200_OK


def test_a_handlers_own_entity_tag_wins() -> None:
    """A handler that knows the version tags better than the bytes do."""
    # Arrange
    micro = Grelmicro(uses=[ConditionalRequests()])
    app = FastAPI()

    @app.get("/carts/1")
    async def read() -> Any:  # noqa: ANN401
        from fastapi.responses import JSONResponse  # noqa: PLC0415

        return JSONResponse({"version": 3}, headers={"ETag": '"3"'})

    micro.install(app)

    # Act
    with TestClient(app) as client:
        response = client.get("/carts/1")

    # Assert
    assert response.headers["etag"] == '"3"'


def test_a_large_response_is_not_held_in_memory() -> None:
    """Past `max_body_size` the response streams on, with no entity tag."""
    # Arrange
    micro = Grelmicro(uses=[ConditionalRequests(max_body_size=16)])
    app = FastAPI()

    @app.get("/big")
    async def big() -> dict[str, str]:
        return {"body": "x" * 64}

    micro.install(app)

    # Act
    with TestClient(app) as client:
        response = client.get("/big")

    # Assert
    assert response.status_code == HTTP_200_OK
    assert "etag" not in response.headers
    assert response.json()["body"] == "x" * 64


def test_etag_responses_false_leaves_the_response_alone() -> None:
    """A service that tags its own responses keeps the middleware quiet."""
    # Arrange
    app = _cart_app(ConditionalRequests(etag_responses=False))

    # Act
    with TestClient(app) as client:
        response = client.get("/carts/1")
        write = client.put("/carts/1", headers={"If-Match": '"3"'})

    # Assert
    assert "etag" not in response.headers
    # The preconditions are still bound, which is the other half of the job.
    assert write.status_code == HTTP_200_OK


def test_an_optional_precondition_lets_an_unconditional_write_through() -> None:
    """`require=False` is how a route allows a write with no `If-Match`."""
    # Arrange
    micro = Grelmicro(uses=[ErrorResponses(), ConditionalRequests()])
    app = FastAPI()

    @app.put("/carts/1")
    async def replace() -> dict[str, int]:
        check_precondition(VERSION, require=False)
        return {"version": NEXT_VERSION}

    micro.install(app)

    # Act
    with TestClient(app) as client:
        response = client.put("/carts/1")

    # Assert
    assert response.status_code == HTTP_200_OK


def test_a_weak_current_tag_never_satisfies_a_write() -> None:
    """A weak tag says "equivalent", which is not enough to overwrite."""
    # Arrange
    micro = Grelmicro(uses=[ErrorResponses(), ConditionalRequests()])
    app = FastAPI()

    @app.put("/carts/1")
    async def replace() -> dict[str, int]:
        # A weak tag is a tag, so it goes through the explicit door.
        check_precondition(etag=etag_of(VERSION, weak=True))
        return {"version": NEXT_VERSION}

    micro.install(app)

    # Act
    with TestClient(app) as client:
        response = client.put("/carts/1", headers={"If-Match": 'W/"3"'})
        strong = client.put("/carts/1", headers={"If-Match": '"3"'})

    # Assert
    assert response.status_code == HTTP_412_PRECONDITION_FAILED
    assert strong.status_code == HTTP_412_PRECONDITION_FAILED


# --- Driven as pure ASGI -------------------------------------------------


async def _drive(
    app: Any,  # noqa: ANN401
    *,
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> list[dict[str, Any]]:
    """Run one request through a pure-ASGI app and return what it sent."""
    scope = {
        "type": "http",
        "method": method,
        "path": "/thing",
        "headers": headers or [],
        "query_string": b"",
    }
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


async def test_a_streamed_response_past_the_limit_flows_on() -> None:
    """Buffering stops, the held chunks go out, and the rest streams."""

    # Arrange
    async def streaming(
        scope: Any,  # noqa: ANN401, ARG001
        receive: Any,  # noqa: ANN401, ARG001
        send: Any,  # noqa: ANN401
    ) -> None:
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        for _ in range(4):
            await send(
                {
                    "type": "http.response.body",
                    "body": b"x" * 8,
                    "more_body": True,
                }
            )
        await send({"type": "http.response.body", "body": b""})

    middleware = ConditionalRequestsMiddleware(streaming, max_body_size=16)

    # Act
    sent = await _drive(middleware)

    # Assert
    start = sent[0]
    assert start["status"] == HTTP_200_OK
    assert not [name for name, _ in start["headers"] if name == b"etag"]
    body = b"".join(message.get("body", b"") for message in sent[1:])
    assert body == b"x" * 32


async def test_a_response_torn_off_mid_body_is_forwarded_untagged() -> None:
    """No complete representation means nothing to hash and nothing to compare."""

    # Arrange
    async def torn(
        scope: Any,  # noqa: ANN401, ARG001
        receive: Any,  # noqa: ANN401, ARG001
        send: Any,  # noqa: ANN401
    ) -> None:
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"half",
                "more_body": True,
            }
        )

    middleware = ConditionalRequestsMiddleware(torn)

    # Act
    sent = await _drive(middleware)

    # Assert
    assert sent[0]["type"] == "http.response.start"
    assert not [name for name, _ in sent[0]["headers"] if name == b"etag"]
    assert sent[1]["body"] == b"half"
    assert sent[1]["more_body"] is True


async def test_a_non_http_scope_passes_through() -> None:
    """A websocket carries no entity tag, and no precondition to bind."""
    # Arrange
    seen: list[str] = []

    async def app(
        scope: Any,  # noqa: ANN401
        receive: Any,  # noqa: ANN401, ARG001
        send: Any,  # noqa: ANN401, ARG001
    ) -> None:
        seen.append(scope["type"])

    middleware = ConditionalRequestsMiddleware(app)

    # Act
    await middleware({"type": "lifespan"}, _nothing, _nothing)

    # Assert
    assert seen == ["lifespan"]


async def _nothing(*args: Any) -> Any:  # noqa: ANN401, ARG001
    """Stand in for a receive or send that is never called."""
    return None


def test_if_match_on_a_resource_that_is_not_there_is_refused() -> None:
    """No resource means no tag of it, so the client's cannot be current."""
    # Arrange
    micro = Grelmicro(uses=[ErrorResponses(), ConditionalRequests()])
    app = FastAPI()

    @app.put("/carts/9")
    async def create() -> dict[str, int]:
        check_precondition(None)
        return {"version": 1}

    micro.install(app)

    # Act
    with TestClient(app) as client:
        response = client.put("/carts/9", headers={"If-Match": '"3"'})

    # Assert
    assert response.status_code == HTTP_412_PRECONDITION_FAILED


async def test_a_response_with_trailers_keeps_its_order() -> None:
    """Trailers follow the body, so this cannot hold the body back."""

    # Arrange
    async def with_trailers(
        scope: Any,  # noqa: ANN401, ARG001
        receive: Any,  # noqa: ANN401, ARG001
        send: Any,  # noqa: ANN401
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [],
                "trailers": True,
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})
        await send({"type": "http.response.trailers", "headers": []})

    middleware = ConditionalRequestsMiddleware(with_trailers)

    # Act
    sent = await _drive(middleware)

    # Assert
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
        "http.response.trailers",
    ]
    assert not [name for name, _ in sent[0]["headers"] if name == b"etag"]


async def test_a_message_the_shaper_does_not_know_flushes_first() -> None:
    """An extension message goes out behind whatever is still held."""

    # Arrange
    async def pathsend(
        scope: Any,  # noqa: ANN401, ARG001
        receive: Any,  # noqa: ANN401, ARG001
        send: Any,  # noqa: ANN401
    ) -> None:
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        # The `pathsend` extension hands the body to the server, so nothing
        # here ever sees bytes to hash.
        await send({"type": "http.response.pathsend", "path": "/var/thing"})

    middleware = ConditionalRequestsMiddleware(pathsend)

    # Act
    sent = await _drive(middleware)

    # Assert
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.pathsend",
    ]
    assert not [name for name, _ in sent[0]["headers"] if name == b"etag"]


def test_the_guard_takes_a_representation_too() -> None:
    """No version column means handing over the resource itself."""
    # Arrange
    micro = Grelmicro(uses=[ErrorResponses(), ConditionalRequests()])
    app = FastAPI()
    cart = Cart(id=1, items=["apple"], version=VERSION)

    @app.put("/carts/1")
    async def replace() -> dict[str, int]:
        check_precondition(cart)
        return {"version": NEXT_VERSION}

    micro.install(app)

    # Act
    with TestClient(app) as client:
        current = client.put("/carts/1", headers={"If-Match": etag_of(cart)})
        stale = client.put("/carts/1", headers={"If-Match": '"3"'})

    # Assert
    assert current.status_code == HTTP_200_OK
    assert stale.status_code == HTTP_412_PRECONDITION_FAILED


def test_the_guard_takes_an_entity_tag_that_is_already_one() -> None:
    """A tag from a store or an upstream service goes through `etag=`."""
    # Arrange
    micro = Grelmicro(uses=[ErrorResponses(), ConditionalRequests()])
    app = FastAPI()

    @app.put("/carts/1")
    async def replace() -> dict[str, int]:
        check_precondition(etag='"from-upstream"')
        return {"version": NEXT_VERSION}

    micro.install(app)

    # Act
    with TestClient(app) as client:
        response = client.put(
            "/carts/1", headers={"If-Match": '"from-upstream"'}
        )

    # Assert
    assert response.status_code == HTTP_200_OK


def test_the_guard_refuses_both_doors_at_once() -> None:
    """One says the version, the other says the tag. Two is a mistake."""
    # Act / Assert
    with pytest.raises(TypeError, match="not both"):
        check_precondition(3, etag='"3"')


def test_the_guard_refuses_neither_door() -> None:
    """A bare call would silently mean the resource is not there."""
    # Act / Assert
    with pytest.raises(TypeError, match="what identifies"):
        check_precondition()


def test_an_entity_tag_passed_as_a_version_says_where_it_goes() -> None:
    """The quotes give it away, and the message names the other door."""
    # Act / Assert
    with pytest.raises(ValueError, match="check_precondition"):
        etag_of('"3"')


# --- The read guard ------------------------------------------------------


def _read_app(**options: Any) -> tuple[FastAPI, list[int]]:  # noqa: ANN401
    """Build a read route that tags from a version and counts its work."""
    loads: list[int] = []
    micro = Grelmicro(uses=[ErrorResponses(), ConditionalRequests(**options)])
    app = FastAPI()

    @app.get("/carts/1")
    async def read() -> Cart:
        if check_freshness(VERSION):
            # The client holds this version, so the load never happens.
            raise HTTPException(status_code=HTTP_304_NOT_MODIFIED)
        loads.append(1)
        return Cart(id=1, items=["apple"], version=VERSION)

    micro.install(app)
    return app, loads


def test_a_read_tags_from_its_version() -> None:
    """No `Response` in the signature, no header string, still an `ETag`."""
    # Arrange
    app, loads = _read_app()

    # Act
    with TestClient(app) as client:
        response = client.get("/carts/1")

    # Assert
    assert response.headers["etag"] == etag_of(VERSION)
    assert loads == [1]


def test_a_fresh_read_skips_the_work() -> None:
    """The client already holds it, so the handler never builds a body."""
    # Arrange
    app, loads = _read_app()

    # Act
    with TestClient(app) as client:
        response = client.get(
            "/carts/1", headers={"If-None-Match": etag_of(VERSION)}
        )

    # Assert
    assert response.status_code == HTTP_304_NOT_MODIFIED
    assert response.content == b""
    assert loads == []


def test_a_recorded_tag_answers_304_even_when_the_handler_ignores_it() -> None:
    """The return value is a shortcut, not the mechanism."""
    # Arrange
    micro = Grelmicro(uses=[ConditionalRequests()])
    app = FastAPI()

    @app.get("/carts/1")
    async def read() -> Cart:
        check_freshness(VERSION)
        return Cart(id=1, items=["apple"], version=VERSION)

    micro.install(app)

    # Act
    with TestClient(app) as client:
        response = client.get(
            "/carts/1", headers={"If-None-Match": etag_of(VERSION)}
        )

    # Assert
    assert response.status_code == HTTP_304_NOT_MODIFIED
    assert response.headers["etag"] == etag_of(VERSION)


def test_a_recorded_tag_beats_the_body_hash() -> None:
    """A version tags better than the bytes, and costs nothing to record."""
    # Arrange
    app, _loads = _read_app()

    # Act
    with TestClient(app) as client:
        response = client.get("/carts/1")

    # Assert
    assert response.headers["etag"] == etag_of(VERSION)
    assert response.headers["etag"] != etag_of(response.content)


def test_the_read_guard_needs_the_component() -> None:
    """Without the middleware there is no request to answer for."""
    # Arrange
    app = FastAPI()

    @app.get("/carts/1")
    async def read() -> dict[str, int]:
        check_freshness(VERSION)
        return {"version": VERSION}

    Grelmicro().install(app)

    # Act / Assert
    with (
        TestClient(app, raise_server_exceptions=True) as client,
        pytest.raises(OutOfContextError, match="check_freshness"),
    ):
        client.get("/carts/1")


# --- Without ErrorResponses ----------------------------------------------


def test_the_component_answers_without_error_responses() -> None:
    """Registering it is the opt-in. No `500`, whatever else is wired."""
    # Arrange
    micro = Grelmicro(uses=[ConditionalRequests()])
    app = FastAPI()

    @app.put("/carts/1")
    async def replace() -> dict[str, int]:
        check_precondition(VERSION)
        return {"version": NEXT_VERSION}

    micro.install(app)

    # Act
    with TestClient(app) as client:
        stale = client.put("/carts/1", headers={"If-Match": '"1"'})
        unconditional = client.put("/carts/1")

    # Assert
    assert stale.status_code == HTTP_412_PRECONDITION_FAILED
    assert unconditional.status_code == HTTP_428_PRECONDITION_REQUIRED
    # RFC 9457 is the default format, which an error body always needs.
    assert stale.headers["content-type"] == "application/problem+json"
    assert stale.json()["type"].endswith("#precondition-failed")


def test_a_handler_registered_first_still_wins() -> None:
    """The component answers what nothing else already answers."""
    # Arrange
    micro = Grelmicro(uses=[ConditionalRequests()])
    app = FastAPI()

    @app.put("/carts/1")
    async def replace() -> dict[str, int]:
        check_precondition(VERSION)
        return {"version": NEXT_VERSION}

    async def mine(request: Any, exc: Exception) -> Any:  # noqa: ANN401, ARG001
        from fastapi.responses import JSONResponse  # noqa: PLC0415

        return JSONResponse({"mine": True}, status_code=418)

    app.add_exception_handler(PreconditionFailedError, mine)
    micro.install(app)

    # Act
    with TestClient(app) as client:
        response = client.put("/carts/1", headers={"If-Match": '"1"'})

    # Assert
    assert response.json() == {"mine": True}


# --- OpenAPI -------------------------------------------------------------


def _documented_app(**options: Any) -> FastAPI:  # noqa: ANN401
    """Build an app with one read, one write, and one create."""
    micro = Grelmicro(uses=[ConditionalRequests(**options)])
    app = FastAPI()

    @app.get("/carts/1")
    async def read() -> dict[str, int]:
        return {"version": VERSION}

    @app.patch("/carts/1")
    async def update() -> dict[str, int]:
        return {"version": NEXT_VERSION}

    @app.post("/carts")
    async def create() -> dict[str, int]:
        return {"version": FIRST_VERSION}

    @app.get("/metrics")
    async def metrics() -> dict[str, int]:
        return {"up": 1}

    micro.install(app)
    return app


def _parameters(operation: dict[str, Any]) -> list[str]:
    """Return the header parameters an operation declares."""
    return [
        parameter["name"]
        for parameter in operation.get("parameters", ())
        if parameter["in"] == "header"
    ]


def test_a_read_documents_the_header_a_client_sends_back() -> None:
    """Swagger shows the field, so the header is reachable from the UI."""
    # Arrange
    app = _documented_app()

    # Act
    operation = app.openapi()["paths"]["/carts/1"]["get"]

    # Assert
    assert _parameters(operation) == ["If-None-Match"]
    assert "304" in operation["responses"]


def test_a_write_documents_the_precondition_and_its_refusals() -> None:
    """A client built from the schema knows both ways it can be refused."""
    # Arrange
    app = _documented_app()

    # Act
    operation = app.openapi()["paths"]["/carts/1"]["patch"]

    # Assert
    assert _parameters(operation) == ["If-Match"]
    assert "412" in operation["responses"]
    assert "428" in operation["responses"]


def test_the_header_is_optional_until_it_is_enforced() -> None:
    """`required` in the schema is what the service actually refuses."""
    # Arrange
    optional = _documented_app()
    enforced = _documented_app(require_precondition=("PATCH",))

    # Act
    loose = optional.openapi()["paths"]["/carts/1"]["patch"]["parameters"]
    strict = enforced.openapi()["paths"]["/carts/1"]["patch"]["parameters"]

    # Assert
    assert [p["required"] for p in loose if p["in"] == "header"] == [False]
    assert [p["required"] for p in strict if p["in"] == "header"] == [True]


def test_a_create_carries_no_precondition() -> None:
    """`POST` has nothing to match against, so nothing is added to it."""
    # Arrange
    app = _documented_app()

    # Act
    operation = app.openapi()["paths"]["/carts"]["post"]

    # Assert
    assert _parameters(operation) == []
    assert "412" not in operation["responses"]


def test_an_excluded_path_is_left_out_of_the_schema_too() -> None:
    """What the middleware skips, the schema does not promise."""
    # Arrange
    app = _documented_app(exclude=("/metrics",))

    # Act
    schema = app.openapi()["paths"]

    # Assert
    assert _parameters(schema["/metrics"]["get"]) == []
    assert _parameters(schema["/carts/1"]["get"]) == ["If-None-Match"]


def test_openapi_false_leaves_the_conditional_schema_alone() -> None:
    """A service that publishes its own schema keeps it untouched."""
    # Arrange
    app = _documented_app(openapi=False)

    # Act
    schema = app.openapi()["paths"]

    # Assert
    assert _parameters(schema["/carts/1"]["get"]) == []
    assert "412" not in schema["/carts/1"]["patch"]["responses"]


def test_the_documented_refusal_follows_the_registered_format() -> None:
    """The schema publishes the body the app actually answers with."""
    # Arrange
    micro = Grelmicro(uses=[ErrorResponses.tmf(), ConditionalRequests()])
    app = FastAPI()

    @app.patch("/carts/1")
    async def update() -> dict[str, int]:
        return {"version": NEXT_VERSION}

    micro.install(app)

    # Act
    responses = app.openapi()["paths"]["/carts/1"]["patch"]["responses"]

    # Assert
    assert "application/json" in responses["412"]["content"]
    assert responses["412"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("TMFError")


def test_documenting_needs_the_middleware() -> None:
    """Nothing to describe without it, and saying so beats a silent no-op."""
    # Arrange
    app = FastAPI()

    # Act / Assert
    with pytest.raises(TypeError, match="no ConditionalRequestsMiddleware"):
        document_conditional_requests(app)


def test_documenting_needs_a_fastapi_app() -> None:
    """Only FastAPI builds an OpenAPI schema to annotate."""
    # Act / Assert
    with pytest.raises(TypeError, match="needs a FastAPI app"):
        document_conditional_requests(Starlette())  # ty: ignore[invalid-argument-type]


def test_an_app_with_nothing_to_document_is_left_alone() -> None:
    """No read and no write means no header to describe."""
    # Arrange
    micro = Grelmicro(uses=[ConditionalRequests()])
    app = FastAPI()

    @app.post("/carts")
    async def create() -> dict[str, int]:
        return {"version": FIRST_VERSION}

    micro.install(app)

    # Act
    schema = app.openapi()

    # Assert
    assert "parameters" not in schema["paths"]["/carts"]["post"]
    assert "components" not in schema


def test_include_selects_the_paths_it_names() -> None:
    """Empty means every path, and naming one narrows it to that one."""
    # Arrange
    app = _documented_app(include=("/carts/*",))

    # Act
    with TestClient(app) as client:
        inside = client.get("/carts/1")
        outside = client.get("/metrics")

    # Assert
    assert "etag" in inside.headers
    assert "etag" not in outside.headers


def test_the_schema_documents_only_what_is_included() -> None:
    """What the middleware skips, the schema does not promise."""
    # Arrange
    app = _documented_app(include=("/carts/*",))

    # Act
    schema = app.openapi()["paths"]

    # Assert
    assert _parameters(schema["/carts/1"]["get"]) == ["If-None-Match"]
    assert _parameters(schema["/metrics"]["get"]) == []


# --- Injected, the FastAPI way --------------------------------------------


def test_the_guards_can_be_injected() -> None:
    """A handler declares one word, and FastAPI hands it the guards."""
    # Arrange
    micro = Grelmicro(uses=[ErrorResponses(), ConditionalRequests()])
    app = FastAPI()

    @app.get("/carts/{cart_id}")
    async def read(cart_id: int, conditional: Conditional) -> dict[str, int]:  # noqa: ARG001
        conditional.fresh(VERSION)
        return {"version": VERSION}

    @app.patch("/carts/{cart_id}")
    async def update(
        cart_id: int,  # noqa: ARG001
        conditional: Conditional,
    ) -> dict[str, int]:
        conditional.check(VERSION)
        return {"version": NEXT_VERSION}

    micro.install(app)

    # Act
    with TestClient(app) as client:
        read_response = client.get("/carts/1")
        current = client.patch("/carts/1", headers={"If-Match": '"3"'})
        stale = client.patch("/carts/1", headers={"If-Match": '"2"'})
        unconditional = client.patch("/carts/1")

    # Assert
    assert read_response.headers["etag"] == etag_of(VERSION)
    assert current.status_code == HTTP_200_OK
    assert stale.status_code == HTTP_412_PRECONDITION_FAILED
    assert unconditional.status_code == HTTP_428_PRECONDITION_REQUIRED


def test_the_injected_guard_declares_what_the_client_sends() -> None:
    """The dependency is not a parameter, the headers it reads are."""
    # Arrange
    micro = Grelmicro(uses=[ConditionalRequests()])
    app = FastAPI()

    @app.patch("/carts/{cart_id}")
    async def update(
        cart_id: int,  # noqa: ARG001
        conditional: Conditional,
    ) -> dict[str, int]:
        conditional.check(VERSION)
        return {"version": NEXT_VERSION}

    micro.install(app)

    # Act
    operation = app.openapi()["paths"]["/carts/{cart_id}"]["patch"]

    # Assert
    assert [p["name"] for p in operation["parameters"]] == [
        "cart_id",
        "If-Match",
        "If-None-Match",
    ]


def test_both_forms_are_the_same_answer() -> None:
    """Injected or imported, it is one implementation underneath."""
    # Arrange
    micro = Grelmicro(uses=[ErrorResponses(), ConditionalRequests()])
    app = FastAPI()

    @app.patch("/injected")
    async def injected(conditional: Conditional) -> dict[str, int]:
        conditional.check(VERSION)
        return {"version": NEXT_VERSION}

    @app.patch("/imported")
    async def imported() -> dict[str, int]:
        check_precondition(VERSION)
        return {"version": NEXT_VERSION}

    micro.install(app)

    # Act
    with TestClient(app) as client:
        first = client.patch("/injected", headers={"If-Match": '"2"'})
        second = client.patch("/imported", headers={"If-Match": '"2"'})

    # Assert
    assert first.status_code == second.status_code
    assert first.json()["type"] == second.json()["type"]


def test_a_declared_requirement_reaches_the_schema() -> None:
    """What the service refuses without is what the schema calls required."""
    # Arrange
    micro = Grelmicro(uses=[ErrorResponses(), ConditionalRequests()])
    app = FastAPI()

    @app.patch("/loose/{cart_id}")
    async def loose(
        cart_id: int,  # noqa: ARG001
        conditional: Conditional,
    ) -> dict[str, int]:
        conditional.check(VERSION, require=False)
        return {"version": NEXT_VERSION}

    @app.patch("/strict/{cart_id}")
    async def strict(
        cart_id: int,  # noqa: ARG001
        conditional: ConditionalRequired,
    ) -> dict[str, int]:
        conditional.check(VERSION)
        return {"version": NEXT_VERSION}

    micro.install(app)

    # Act
    paths = app.openapi()["paths"]
    loose_headers = _headers(paths["/loose/{cart_id}"]["patch"])
    strict_headers = _headers(paths["/strict/{cart_id}"]["patch"])

    # Assert
    assert loose_headers["If-Match"] is False
    assert strict_headers["If-Match"] is True


def test_a_declared_requirement_answers_428_not_422() -> None:
    """The status is the one RFC 6585 added, not a validation error."""
    # Arrange
    micro = Grelmicro(uses=[ErrorResponses(), ConditionalRequests()])
    app = FastAPI()

    @app.patch("/carts/{cart_id}")
    async def update(
        cart_id: int,  # noqa: ARG001
        conditional: ConditionalRequired,
    ) -> dict[str, int]:
        conditional.check(VERSION)
        return {"version": NEXT_VERSION}

    micro.install(app)

    # Act
    with TestClient(app) as client:
        missing = client.patch("/carts/1")
        stale = client.patch("/carts/1", headers={"If-Match": '"2"'})
        current = client.patch("/carts/1", headers={"If-Match": '"3"'})

    # Assert
    assert missing.status_code == HTTP_428_PRECONDITION_REQUIRED
    assert missing.json()["type"].endswith("#precondition-required")
    assert stale.status_code == HTTP_412_PRECONDITION_FAILED
    assert current.status_code == HTTP_200_OK


def test_the_injected_guard_declares_both_headers() -> None:
    """Swagger offers the fields on the operation that reads them."""
    # Arrange
    micro = Grelmicro(uses=[ConditionalRequests()])
    app = FastAPI()

    @app.get("/carts/{cart_id}")
    async def read(
        cart_id: int,  # noqa: ARG001
        conditional: Conditional,
    ) -> dict[str, int]:
        conditional.fresh(VERSION)
        return {"version": VERSION}

    micro.install(app)

    # Act
    headers = _headers(app.openapi()["paths"]["/carts/{cart_id}"]["get"])

    # Assert
    assert set(headers) == {"If-Match", "If-None-Match"}


def _headers(operation: dict[str, Any]) -> dict[str, bool]:
    """Return the header parameters of an operation, and whether required."""
    return {
        parameter["name"]: parameter["required"]
        for parameter in operation.get("parameters", ())
        if parameter["in"] == "header"
    }


# --- Values that fight back ----------------------------------------------


class _Unbound:
    """A lazy proxy that raises the moment anything reads its class."""

    @property
    def __class__(self) -> type:
        msg = "unbound proxy"
        raise RuntimeError(msg)


class _NoName(type):
    """A metaclass whose `__name__` raises, so naming the type runs code."""

    @property
    def __name__(cls) -> str:
        msg = "no name for you"
        raise RuntimeError(msg)


class _Unnameable(metaclass=_NoName):
    """An instance whose type refuses to be named."""


class _RaisingDump:
    """A model-like object whose `model_dump` raises when read."""

    @property
    def model_dump(self) -> object:
        msg = "not bound yet"
        raise RuntimeError(msg)


@pytest.mark.parametrize(
    "build",
    [_Unbound, _Unnameable, _RaisingDump],
    ids=["unbound-proxy", "unnameable-type", "raising-model-dump"],
)
def test_a_value_that_raises_still_gets_the_argument_error(
    build: type[object],
) -> None:
    """A caller's object never raises out of `etag_of`.

    `isinstance` reads `__class__`, naming a type reads `__name__`, and
    serializing reads `model_dump`, all of which are caller code. Whatever
    they raise, the answer is the argument error the value was going to
    get.
    """
    # Built here rather than in the parameters: reading one to name a test
    # case is the very thing these raise from.
    value = build()

    # Act / Assert
    with pytest.raises(TypeError, match="etag_of"):
        etag_of(value)


def test_a_real_interrupt_is_never_swallowed_reading_an_attribute() -> None:
    """Guarding caller code must not turn `Ctrl-C` into an argument error."""

    # Arrange
    class Interrupting:
        @property
        def model_dump(self) -> object:
            raise KeyboardInterrupt

    # Act / Assert
    with pytest.raises(KeyboardInterrupt):
        etag_of(Interrupting())


def test_a_real_interrupt_is_never_swallowed_serializing() -> None:
    """The same, on the way through the serializer."""

    # Arrange
    class Interrupting(dict[str, int]):
        def items(self) -> Any:  # noqa: ANN401
            raise KeyboardInterrupt

    # Act / Assert
    with pytest.raises(KeyboardInterrupt):
        etag_of(Interrupting(a=1))


async def test_a_streamed_response_is_never_held_back() -> None:
    """An event stream reaches the client as it is produced, not at the end.

    Buffering to hash a body is only safe while the body arrives in one
    piece. A response that arrives in chunks is one the app is streaming,
    and holding it turns a live stream into a single message nobody reads
    until the stream ends.
    """
    # Arrange
    produced: list[str] = []

    async def stream(
        scope: Any,  # noqa: ANN401, ARG001
        receive: Any,  # noqa: ANN401, ARG001
        send: Any,  # noqa: ANN401
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        for index in range(3):
            produced.append(f"chunk-{index}")
            await send(
                {
                    "type": "http.response.body",
                    "body": f"data: {index}\n\n".encode(),
                    "more_body": True,
                }
            )
        await send({"type": "http.response.body", "body": b""})

    middleware = ConditionalRequestsMiddleware(stream)

    # Act
    sent = await _drive(middleware)

    # Assert
    bodies = [
        message["body"]
        for message in sent
        if message["type"] == "http.response.body"
    ]
    # One message per chunk, in order, rather than one at the end.
    assert bodies[:3] == [b"data: 0\n\n", b"data: 1\n\n", b"data: 2\n\n"]
    assert not [name for name, _ in sent[0]["headers"] if name == b"etag"]


def test_a_bodyless_status_carries_no_generated_tag() -> None:
    """A `204` has no representation, so hashing its empty body says nothing."""
    # Arrange
    micro = Grelmicro(uses=[ConditionalRequests()])
    app = FastAPI()

    @app.get("/one", status_code=HTTP_204_NO_CONTENT)
    async def one() -> None:
        return None

    @app.get("/two", status_code=HTTP_204_NO_CONTENT)
    async def two() -> None:
        return None

    micro.install(app)

    # Act
    with TestClient(app) as client:
        first = client.get("/one")
        second = client.get("/two")

    # Assert
    assert first.status_code == HTTP_204_NO_CONTENT
    assert "etag" not in first.headers
    assert "etag" not in second.headers


def test_a_tag_cannot_hold_the_separator() -> None:
    """A comma separates two tags in one header, so no tag may carry one."""
    # Act / Assert
    with pytest.raises(ValueError, match="commas"):
        etag_of("v1,rev2")


def test_the_injected_guard_answers_without_the_component() -> None:
    """It declares the headers, so it reads them whether or not one is wired."""
    # Arrange
    micro = Grelmicro(uses=[ErrorResponses()])  # no ConditionalRequests
    app = FastAPI()

    @app.patch("/carts/1")
    async def update(conditional: Conditional) -> dict[str, int]:
        conditional.check(VERSION)
        return {"version": NEXT_VERSION}

    micro.install(app)

    # Act
    with TestClient(app) as client:
        current = client.patch("/carts/1", headers={"If-Match": '"3"'})
        stale = client.patch("/carts/1", headers={"If-Match": '"2"'})
        missing = client.patch("/carts/1")

    # Assert
    assert current.status_code == HTTP_200_OK
    assert stale.status_code == HTTP_412_PRECONDITION_FAILED
    assert missing.status_code == HTTP_428_PRECONDITION_REQUIRED


async def test_a_response_that_never_sends_a_body_is_still_forwarded() -> None:
    """Start and nothing else: there is no representation to tag."""

    # Arrange
    async def headers_only(
        scope: Any,  # noqa: ANN401, ARG001
        receive: Any,  # noqa: ANN401, ARG001
        send: Any,  # noqa: ANN401
    ) -> None:
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )

    middleware = ConditionalRequestsMiddleware(headers_only)

    # Act
    sent = await _drive(middleware)

    # Assert
    assert [message["type"] for message in sent] == ["http.response.start"]
    assert not [name for name, _ in sent[0]["headers"] if name == b"etag"]


async def test_a_streamed_response_still_carries_a_recorded_tag() -> None:
    """A version the handler recorded needs no body to be known.

    The stream is not held, and the tag still reaches the client, so the
    next request can send it back.
    """

    # Arrange
    async def stream(
        scope: Any,  # noqa: ANN401, ARG001
        receive: Any,  # noqa: ANN401, ARG001
        send: Any,  # noqa: ANN401
    ) -> None:
        check_freshness(VERSION)
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send(
            {"type": "http.response.body", "body": b"a", "more_body": True}
        )
        await send({"type": "http.response.body", "body": b""})

    middleware = ConditionalRequestsMiddleware(stream)

    # Act
    sent = await _drive(middleware)

    # Assert
    assert dict(sent[0]["headers"])[b"etag"] == etag_of(VERSION).encode()


async def test_a_streamed_response_still_answers_304() -> None:
    """Ignoring what `check_freshness` returned costs the work, not the answer.

    The tag is known before the first byte, so the stream is swapped for a
    `304` and what the handler goes on producing reaches nobody.
    """

    # Arrange
    async def stream(
        scope: Any,  # noqa: ANN401, ARG001
        receive: Any,  # noqa: ANN401, ARG001
        send: Any,  # noqa: ANN401
    ) -> None:
        check_freshness(VERSION)
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        for _ in range(2):
            await send(
                {
                    "type": "http.response.body",
                    "body": b"data: x\n\n",
                    "more_body": True,
                }
            )
        await send({"type": "http.response.body", "body": b""})

    middleware = ConditionalRequestsMiddleware(stream)

    # Act
    sent = await _drive(
        middleware,
        headers=[(b"if-none-match", etag_of(VERSION).encode())],
    )

    # Assert
    assert sent[0]["status"] == HTTP_304_NOT_MODIFIED
    assert sum(len(message.get("body", b"")) for message in sent) == 0


def test_a_failure_that_recorded_a_version_is_not_answered_304() -> None:
    """A resource that is gone or refused is not one the client still holds.

    The version was read, then the load failed. Answering `304` would
    leave the client serving a cached copy of something it can no longer
    have.
    """
    # Arrange
    micro = Grelmicro(uses=[ConditionalRequests()])
    app = FastAPI()

    @app.get("/gone")
    async def gone() -> dict[str, int]:
        check_freshness(VERSION)
        raise HTTPException(status_code=HTTP_404_NOT_FOUND)

    micro.install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/gone", headers={"If-None-Match": etag_of(VERSION)}
        )

    # Assert
    assert response.status_code == HTTP_404_NOT_FOUND
    assert "etag" not in response.headers


def test_a_handlers_own_tag_still_answers_304() -> None:
    """The best-behaved handler is not the one that loses the shortcut."""
    # Arrange
    micro = Grelmicro(uses=[ConditionalRequests()])
    app = FastAPI()

    @app.get("/carts/1")
    async def read() -> Any:  # noqa: ANN401
        from fastapi.responses import JSONResponse  # noqa: PLC0415

        return JSONResponse({"version": VERSION}, headers={"ETag": '"v7"'})

    micro.install(app)

    # Act
    with TestClient(app) as client:
        matching = client.get("/carts/1", headers={"If-None-Match": '"v7"'})
        stale = client.get("/carts/1", headers={"If-None-Match": '"v6"'})

    # Assert
    assert matching.status_code == HTTP_304_NOT_MODIFIED
    assert matching.headers["etag"] == '"v7"'
    assert stale.status_code == HTTP_200_OK
    assert stale.headers["etag"] == '"v7"'


async def test_a_background_task_never_delays_the_response() -> None:
    """The decision is made when the body completes, not when the app returns.

    A framework runs background tasks inside the call this wraps, so a
    response held until they finish is a response the client waits for.
    """
    # Arrange
    started: list[float] = []

    async def app(
        scope: Any,  # noqa: ANN401, ARG001
        receive: Any,  # noqa: ANN401, ARG001
        send: Any,  # noqa: ANN401
    ) -> None:
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send({"type": "http.response.body", "body": b"{}"})
        # What a background task does inside the same call.
        await anyio.sleep(0.2)

    middleware = ConditionalRequestsMiddleware(app)
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/thing",
        "headers": [],
        "query_string": b"",
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            started.append(anyio.current_time())

    # Act
    begin = anyio.current_time()
    await middleware(scope, receive, send)  # ty: ignore[invalid-argument-type]

    # Assert
    assert started[0] - begin < BACKGROUND_WORK


def test_a_router_prefix_selects_the_router_root() -> None:
    """`"/payments/*"` covers `POST /payments`, the create route it names."""
    # Assert
    assert selects("/payments", include=("/payments/*",), exclude=())
    assert selects("/payments/1", include=("/payments/*",), exclude=())
    assert not selects("/payments-eu", include=("/payments/*",), exclude=())
    assert not selects("/other", include=("/payments/*",), exclude=())
