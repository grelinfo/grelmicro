"""Each HTTP component describes itself in the OpenAPI schema it is handed.

The edit takes a schema dict and returns it, so FastAPI and Litestar hand
the schema they build to the same code and publish the same document.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, Self

import pytest
from fastapi import FastAPI
from litestar import Litestar, delete, get, patch, post, put
from litestar.params import FromPath
from litestar.status_codes import HTTP_200_OK
from litestar.testing import TestClient as LitestarTestClient

from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.http import (
    AuthenticatedRequests,
    ConditionalRequests,
    ErrorResponses,
    IdempotentRequests,
    RateLimitedRequests,
    RouteDeclaration,
)
from grelmicro.integrations.fastapi import (
    ConditionalRequest,
    ConditionalRequired,
    route_declarations,
)
from grelmicro.resilience import RateLimiter
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter
from tests.test_authentication import verifier

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

pytestmark = [pytest.mark.timeout(10)]


def _limited(*, openapi: bool = True) -> RateLimitedRequests:
    """Return a rate limit that meters nothing, so a schema read is never refused."""
    limiter = RateLimiter.sliding_window(
        "api", limit=100, window=60, backend=MemoryRateLimiterAdapter()
    )
    return RateLimitedRequests(
        limiter, key=lambda _scope: None, openapi=openapi
    )


def _schema() -> dict[str, Any]:
    """Return a schema with a read, a create and an update."""
    ok = {"200": {"description": "OK"}}
    return {
        "openapi": "3.1.0",
        "info": {"title": "Carts", "version": "1"},
        "paths": {
            "/carts": {
                "get": {"responses": copy.deepcopy(ok)},
                "post": {"responses": copy.deepcopy(ok)},
            },
            "/carts/{cart_id}": {
                "put": {"responses": copy.deepcopy(ok)},
            },
        },
    }


def _headers(operation: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the header parameters of an operation, by name."""
    return {
        parameter["name"]: parameter
        for parameter in operation.get("parameters", ())
        if parameter["in"] == "header"
    }


def test_rate_limited_requests_document_openapi_adds_the_refusal_to_every_operation() -> (
    None
):
    """Every operation gains the `429`, and its `2xx` the budget fields."""
    # Arrange
    schema = _schema()

    # Act
    edited = _limited()._document_openapi(schema)

    # Assert
    assert edited is schema
    for path, method in (
        ("/carts", "get"),
        ("/carts", "post"),
        ("/carts/{cart_id}", "put"),
    ):
        responses = edited["paths"][path][method]["responses"]
        assert "Retry-After" in responses["429"]["headers"]
        assert "RateLimit" in responses["200"]["headers"]
    assert "ProblemDetail" in edited["components"]["schemas"]


def test_idempotent_requests_document_openapi_adds_the_key_to_covered_writes() -> (
    None
):
    """A `POST` gains the key header and the refusals, a `GET` nothing."""
    # Arrange
    schema = _schema()

    # Act
    edited = IdempotentRequests(require_key=True)._document_openapi(schema)

    # Assert
    create = edited["paths"]["/carts"]["post"]
    assert _headers(create)["Idempotency-Key"]["required"] is True
    assert {"400", "409"} <= set(create["responses"])
    assert "Idempotent-Replayed" in create["responses"]["200"]["headers"]
    assert "parameters" not in edited["paths"]["/carts"]["get"]


def test_conditional_requests_document_openapi_marks_a_declared_route_required() -> (
    None
):
    """A route declaring `precondition_required` gets `If-Match` required, `POST` included."""
    # Arrange
    schema = _schema()
    routes = [
        RouteDeclaration(
            "/carts",
            methods=frozenset({"POST"}),
            precondition_required=True,
        )
    ]

    # Act
    edited = ConditionalRequests()._document_openapi(schema, routes=routes)

    # Assert
    create = edited["paths"]["/carts"]["post"]
    assert _headers(create)["If-Match"]["required"] is True
    assert "If-None-Match: *" in _headers(create)["If-Match"]["description"]
    assert {"412", "428"} <= set(create["responses"])
    update = edited["paths"]["/carts/{cart_id}"]["put"]
    assert _headers(update)["If-Match"]["required"] is False
    read = edited["paths"]["/carts"]["get"]
    assert "If-None-Match" in _headers(read)
    assert "304" in read["responses"]


def test_conditional_requests_document_openapi_reads_a_declaration_by_its_schema_path() -> (
    None
):
    """A converter in the declared path still names the schema's path."""
    # Arrange
    schema = _schema()
    routes = [
        RouteDeclaration(
            "/carts/{cart_id:int}",
            methods=frozenset({"PUT"}),
            precondition_required=True,
        )
    ]

    # Act
    edited = ConditionalRequests()._document_openapi(schema, routes=routes)

    # Assert
    update = edited["paths"]["/carts/{cart_id}"]["put"]
    assert _headers(update)["If-Match"]["required"] is True


def test_conditional_requests_document_openapi_twice_changes_nothing() -> None:
    """A second edit keeps a required `If-Match` and its note as they are."""
    # Arrange
    component = ConditionalRequests()
    routes = [
        RouteDeclaration(
            "/carts/{cart_id}",
            methods=frozenset({"PUT"}),
            precondition_required=True,
        )
    ]
    once = component._document_openapi(_schema(), routes=routes)
    expected = copy.deepcopy(once)

    # Act
    twice = component._document_openapi(once, routes=routes)

    # Assert
    assert twice == expected


def test_conditional_requests_document_openapi_marks_a_required_post() -> None:
    """`require_precondition` naming `POST` describes the `428` it answers there."""
    # Arrange
    schema = _schema()

    # Act
    edited = ConditionalRequests(
        require_precondition=("POST",)
    )._document_openapi(schema)

    # Assert
    create = edited["paths"]["/carts"]["post"]
    assert _headers(create)["If-Match"]["required"] is True
    assert "428" in create["responses"]


def test_authenticated_requests_document_openapi_reads_the_declarations() -> (
    None
):
    """An anonymous route lists the scheme as optional, a scoped one names its scopes."""
    # Arrange
    schema = _schema()
    routes = [
        RouteDeclaration("/carts", methods=frozenset({"GET"}), anonymous=True),
        RouteDeclaration(
            "/carts/{cart_id}",
            methods=frozenset({"PUT"}),
            scopes=frozenset({"carts:write", "carts:admin"}),
        ),
    ]

    # Act
    edited = AuthenticatedRequests(verifier())._document_openapi(
        schema, routes=routes
    )

    # Assert
    paths = edited["paths"]
    [scheme] = edited["components"]["securitySchemes"]
    assert paths["/carts"]["get"]["security"] == [{}, {scheme: []}]
    assert paths["/carts"]["post"]["security"] == [{scheme: []}]
    assert paths["/carts/{cart_id}"]["put"]["security"] == [
        {scheme: ["carts:admin", "carts:write"]}
    ]


def test_authenticated_requests_document_openapi_twice_changes_nothing() -> (
    None
):
    """A second edit keeps an anonymous route's optional scheme as it is."""
    # Arrange
    component = AuthenticatedRequests(verifier())
    routes = [
        RouteDeclaration("/carts", methods=frozenset({"GET"}), anonymous=True)
    ]
    once = component._document_openapi(_schema(), routes=routes)
    expected = copy.deepcopy(once)

    # Act
    twice = component._document_openapi(once, routes=routes)

    # Assert
    assert twice == expected


def test_idempotent_requests_document_openapi_follows_the_error_format() -> (
    None
):
    """The refusals point at the body the registered format answers with."""
    # Arrange
    schema = _schema()

    # Act
    edited = IdempotentRequests()._document_openapi(
        schema, errors=ErrorResponses.tmf()
    )

    # Assert
    content = edited["paths"]["/carts"]["post"]["responses"]["409"]["content"]
    assert content == {
        "application/json": {
            "schema": {"$ref": "#/components/schemas/TMFError"}
        }
    }


@pytest.mark.parametrize(
    "component",
    [
        pytest.param(lambda: _limited(openapi=False), id="rate-limit"),
        pytest.param(
            lambda: IdempotentRequests(openapi=False), id="idempotency"
        ),
        pytest.param(
            lambda: ConditionalRequests(openapi=False), id="conditional"
        ),
        pytest.param(
            lambda: AuthenticatedRequests(verifier(), openapi=False),
            id="authentication",
        ),
    ],
)
def test_component_document_openapi_with_openapi_off_leaves_the_schema_untouched(
    component: Callable[[], Any],
) -> None:
    """`openapi=False` returns the schema as it was handed."""
    # Arrange
    schema = _schema()
    routes = [
        RouteDeclaration(
            "/carts", methods=frozenset({"POST"}), precondition_required=True
        )
    ]

    # Act
    edited = component()._document_openapi(schema, routes=routes)

    # Assert
    assert edited is schema
    assert edited == _schema()


def test_route_declarations_conditional_required_declares_precondition_required() -> (
    None
):
    """A route injecting `ConditionalRequired` declares it, one injecting `Conditional` does not."""
    # Arrange
    app = FastAPI()

    @app.post("/carts")
    async def create(conditional: ConditionalRequired) -> dict[str, bool]:
        return {"guarded": isinstance(conditional, ConditionalRequest)}

    @app.get("/carts")
    async def read() -> dict[str, bool]:
        return {"read": True}

    # Act
    declarations = {
        next(iter(declaration.methods or ())): declaration
        for declaration in route_declarations(app)
    }

    # Assert
    assert declarations["POST"].precondition_required is True
    assert declarations["GET"].precondition_required is False


def test_conditional_requests_fastapi_conditional_required_post_marks_if_match_required() -> (
    None
):
    """A `POST` declaring `ConditionalRequired` answers `428`, and its schema says so."""
    # Arrange
    app = FastAPI()

    @app.post("/carts")
    async def create(conditional: ConditionalRequired) -> dict[str, bool]:
        return {"guarded": isinstance(conditional, ConditionalRequest)}

    Grelmicro(uses=[ConditionalRequests()]).install(app)

    # Act
    operation = app.openapi()["paths"]["/carts"]["post"]

    # Assert
    assert _headers(operation)["If-Match"]["required"] is True
    assert {"412", "428"} <= set(operation["responses"])


class _Recording:
    """A component asking for no middleware of its own, recording each edit."""

    kind = "recording"

    def __init__(self) -> None:
        self.calls: list[tuple[list[RouteDeclaration], Any]] = []

    @property
    def name(self) -> str:
        return "default"

    def asgi_middleware(self) -> tuple[type[Any], dict[str, Any]]:
        return _Passthrough, {}

    def _document_openapi(
        self,
        schema: dict[str, Any],
        *,
        routes: Iterable[RouteDeclaration] = (),
        errors: Any = None,  # noqa: ANN401
    ) -> dict[str, Any]:
        self.calls.append((list(routes), errors))
        schema["x-recorded"] = True
        return schema

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Passthrough:
    """A pure-ASGI middleware that does nothing."""

    def __init__(self, app: Any) -> None:  # noqa: ANN401
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        await self.app(scope, receive, send)


def test_fastapi_install_hands_the_built_schema_to_each_component() -> None:
    """The schema FastAPI builds reaches the edit once, with the routes and the format."""
    # Arrange
    recording = _Recording()
    errors = ErrorResponses()
    app = FastAPI()
    Grelmicro(uses=[errors, recording]).install(app)

    @app.put("/carts/{cart_id}")
    async def update(cart_id: int) -> dict[str, int]:
        return {"cart": cart_id}

    # Act
    schema = app.openapi()
    app.openapi()

    # Assert
    assert schema["x-recorded"] is True
    [(routes, handed)] = recording.calls
    assert handed is errors
    assert (
        RouteDeclaration("/carts/{cart_id}", methods=frozenset({"PUT"}))
        in routes
    )


def test_litestar_install_hands_the_built_schema_to_each_component() -> None:
    """The schema Litestar builds reaches the edit on startup, with the routes and the format."""

    # Arrange
    @put("/carts/{cart_id:int}")
    async def update(cart_id: FromPath[int]) -> dict[str, int]:
        return {"cart": cart_id}

    recording = _Recording()
    errors = ErrorResponses()
    app = Litestar(route_handlers=[update])
    Grelmicro(uses=[errors, recording]).install(app)

    # Act
    with LitestarTestClient(app) as client:
        schema = client.get("/schema/openapi.json").json()

    # Assert
    assert schema["x-recorded"] is True
    [(routes, handed)] = recording.calls
    assert handed is errors
    assert (
        RouteDeclaration("/carts/{cart_id}", methods=frozenset({"PUT"}))
        in routes
    )


def _components(*, openapi: bool = True) -> list[Any]:
    """Return rate limiting, idempotency and conditional requests."""
    return [
        Cache(MemoryCacheAdapter()),
        ErrorResponses(),
        _limited(openapi=openapi),
        IdempotentRequests(openapi=openapi),
        ConditionalRequests(require_precondition=("PUT",), openapi=openapi),
    ]


def _fastapi_schema(*, openapi: bool = True) -> dict[str, Any]:
    """Return the schema of a FastAPI app serving a read, a create and a write."""
    app = FastAPI()

    @app.get("/carts/{cart_id}")
    async def read(cart_id: int) -> dict[str, int]:
        return {"cart": cart_id}

    @app.post("/carts")
    async def create() -> dict[str, int]:
        return {"cart": 1}

    @app.put("/carts/{cart_id}")
    async def replace(cart_id: int) -> dict[str, int]:
        return {"cart": cart_id}

    @app.patch("/carts/{cart_id}")
    async def update(cart_id: int) -> dict[str, int]:
        return {"cart": cart_id}

    @app.delete("/carts/{cart_id}")
    async def remove(cart_id: int) -> dict[str, int]:
        return {"cart": cart_id}

    Grelmicro(uses=_components(openapi=openapi)).install(app)
    return app.openapi()


def _litestar_schema(*, openapi: bool = True) -> dict[str, Any]:
    """Return the schema of the same app served by Litestar."""

    @get("/carts/{cart_id:int}")
    async def read(cart_id: FromPath[int]) -> dict[str, int]:
        return {"cart": cart_id}

    @post("/carts", status_code=HTTP_200_OK)
    async def create() -> dict[str, int]:
        return {"cart": 1}

    @put("/carts/{cart_id:int}")
    async def replace(cart_id: FromPath[int]) -> dict[str, int]:
        return {"cart": cart_id}

    @patch("/carts/{cart_id:int}")
    async def update(cart_id: FromPath[int]) -> dict[str, int]:
        return {"cart": cart_id}

    @delete("/carts/{cart_id:int}", status_code=HTTP_200_OK)
    async def remove(cart_id: FromPath[int]) -> dict[str, int]:
        return {"cart": cart_id}

    app = Litestar(route_handlers=[read, create, replace, update, remove])
    Grelmicro(uses=_components(openapi=openapi)).install(app)
    with LitestarTestClient(app) as client:
        return client.get("/schema/openapi.json").json()


_OPERATIONS = (
    ("/carts/{cart_id}", "get"),
    ("/carts", "post"),
    ("/carts/{cart_id}", "put"),
    ("/carts/{cart_id}", "patch"),
    ("/carts/{cart_id}", "delete"),
)
"""The operations both apps publish."""

_DESCRIBED_HEADERS = ("Idempotency-Key", "If-Match", "If-None-Match")
"""The request headers the components describe."""

_DESCRIBED_STATUSES = ("304", "409", "412", "428", "429")
"""The responses the components describe."""


def _described(schema: dict[str, Any]) -> dict[tuple[str, str], Any]:
    """Return what the components describe on each operation."""
    described = {}
    for path, method in _OPERATIONS:
        operation = schema["paths"][path][method]
        headers = _headers(operation)
        responses = operation["responses"]
        described[path, method] = {
            "headers": {
                name: headers[name]
                for name in _DESCRIBED_HEADERS
                if name in headers
            },
            "responses": {
                status: responses[status]
                for status in _DESCRIBED_STATUSES
                if status in responses
            },
            "rate limit": responses["200"].get("headers", {}).get("RateLimit"),
            "replayed": responses["200"]
            .get("headers", {})
            .get("Idempotent-Replayed"),
        }
    return described


def test_litestar_app_publishes_what_the_same_fastapi_app_publishes() -> None:
    """The `429`, the `Idempotency-Key` and the preconditions read the same on both."""
    # Arrange
    fastapi_schema = _fastapi_schema()

    # Act
    litestar_schema = _litestar_schema()

    # Assert
    published = _described(fastapi_schema)
    assert _described(litestar_schema) == published
    assert published["/carts/{cart_id}", "put"]["headers"]["If-Match"][
        "required"
    ]
    assert "Idempotency-Key" in published["/carts", "post"]["headers"]
    assert "429" in published["/carts/{cart_id}", "get"]["responses"]


@pytest.mark.parametrize(
    "schema_of",
    [
        pytest.param(_fastapi_schema, id="fastapi"),
        pytest.param(_litestar_schema, id="litestar"),
    ],
)
def test_component_openapi_off_publishes_nothing_on_either_framework(
    schema_of: Callable[..., dict[str, Any]],
) -> None:
    """`openapi=False` leaves the schema as the framework built it."""
    # Act
    described = _described(schema_of(openapi=False))

    # Assert
    assert all(
        entry
        == {
            "headers": {},
            "responses": {},
            "rate limit": None,
            "replayed": None,
        }
        for entry in described.values()
    )
