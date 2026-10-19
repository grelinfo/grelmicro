"""Each HTTP component describes itself in the OpenAPI schema it is handed.

The edit takes a schema dict and returns it, so FastAPI and Litestar hand
the schema they build to the same code and publish the same document.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

import pytest
from fastapi import FastAPI
from litestar import Litestar, WebSocket, delete, get, post, put, websocket
from litestar.exceptions import LitestarDeprecationWarning
from litestar.openapi import OpenAPIConfig, OpenAPIController
from litestar.openapi.plugins import SwaggerRenderPlugin
from litestar.openapi.spec import (
    Components,
    OpenAPIResponse,
    Operation,
    SecurityScheme,
)
from litestar.params import FromPath
from litestar.plugins import ReceiveRoutePlugin
from litestar.status_codes import HTTP_200_OK, HTTP_404_NOT_FOUND
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
from grelmicro.integrations.fastapi import Anonymous as FastAPIAnonymous
from grelmicro.integrations.fastapi import (
    Authenticated as FastAPIAuthenticated,
)
from grelmicro.integrations.fastapi import (
    ConditionalRequest,
    ConditionalRequired,
    route_declarations,
)
from grelmicro.integrations.litestar import Anonymous as LitestarAnonymous
from grelmicro.integrations.litestar import (
    Authenticated as LitestarAuthenticated,
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
        schema["info"]["description"] = "recorded"
        return schema

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Removing(_Recording):
    """A component removing the summary of every operation it is handed."""

    def _document_openapi(
        self,
        schema: dict[str, Any],
        *,
        routes: Iterable[RouteDeclaration] = (),
        errors: Any = None,  # noqa: ANN401
    ) -> dict[str, Any]:
        self.calls.append((list(routes), errors))
        for path_item in schema["paths"].values():
            for operation in path_item.values():
                operation.pop("summary", None)
        return schema


class _Extending(_Recording):
    """A component adding a member Litestar's spec objects have no field for."""

    def _document_openapi(
        self,
        schema: dict[str, Any],
        *,
        routes: Iterable[RouteDeclaration] = (),
        errors: Any = None,  # noqa: ANN401
    ) -> dict[str, Any]:
        self.calls.append((list(routes), errors))
        schema["info"]["x-audience"] = "internal"
        return schema


@dataclass
class _TaggedOperation(Operation):
    """An app's operation class, rendering a member of its own from its fields."""

    def to_schema(self) -> dict[str, Any]:
        rendered = super().to_schema()
        rendered["x-tags"] = [tag.upper() for tag in self.tags or []]
        return rendered


class _Passthrough:
    """A pure-ASGI middleware that does nothing."""

    def __init__(self, app: Any) -> None:  # noqa: ANN401
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        await self.app(scope, receive, send)


_MISSING: Any = object()
"""Marks a member the schema did not carry."""

_OPENAPI_DEFAULTS = frozenset(
    {"required", "deprecated", "allowEmptyValue", "allowReserved"}
)
"""Members OpenAPI defaults to `false`, which Litestar's objects render."""

_SCHEME = "AuthenticatedRequests"
"""Name the schema publishes the bearer token scheme under."""


def _grelmicro_parts(
    before: Any,  # noqa: ANN401
    after: Any,  # noqa: ANN401
    at: tuple[str, ...] = (),
) -> dict[tuple[str, ...], Any]:
    """Return each member `after` adds to or changes in `before`, by its path.

    A mapping the framework did not publish is read as an empty one, so a
    header added to a response reads the same whether or not the framework
    gave it a `headers` member of its own.
    """
    if before == after:
        return {}
    if isinstance(after, dict) and (
        before is _MISSING or isinstance(before, dict)
    ):
        known = {} if before is _MISSING else before
        found: dict[tuple[str, ...], Any] = {}
        for key, value in after.items():
            found |= _grelmicro_parts(
                known.get(key, _MISSING), value, (*at, key)
            )
        for key in known.keys() - after.keys():
            found[*at, key] = _MISSING
        return found
    return {at: after}


def _rendered(node: Any) -> Any:  # noqa: ANN401
    """Return `node` as Litestar renders it.

    Litestar's objects state OpenAPI's own `false` defaults, and its
    renderer drops every `null`, a `null` default included.
    """
    if isinstance(node, dict):
        return {
            key: _rendered(value)
            for key, value in node.items()
            if not (key in _OPENAPI_DEFAULTS and value is False)
            and value is not None
        }
    if isinstance(node, list):
        return [_rendered(item) for item in node]
    return node


def _components(*, openapi: bool = True) -> list[Any]:
    """Return rate limiting, idempotency and conditional requests."""
    return [
        Cache(MemoryCacheAdapter()),
        ErrorResponses(),
        _limited(openapi=openapi),
        IdempotentRequests(openapi=openapi),
        ConditionalRequests(require_precondition=("PUT",), openapi=openapi),
    ]


def _authenticated(*, openapi: bool = True) -> list[Any]:
    """Return authentication excluding the probe and the schema routes."""
    return [
        ErrorResponses(),
        AuthenticatedRequests(
            verifier(), exclude=("/livez", "/schema/*"), openapi=openapi
        ),
    ]


def _every_component(*, openapi: bool = True) -> list[Any]:
    """Return every component that describes itself, behind authentication."""
    return [
        *_components(openapi=openapi),
        *_authenticated(openapi=openapi)[1:],
    ]


def _fastapi_routes(app: FastAPI) -> None:
    """Declare the routes both twin apps serve on a FastAPI app."""

    @app.get("/items")
    async def items() -> dict[str, int]:
        return {"ok": 1}

    @app.post("/items")
    async def create() -> dict[str, int]:
        return {"ok": 1}

    @app.put("/items")
    async def replace() -> dict[str, int]:
        return {"ok": 1}

    @app.delete(
        "/orders", dependencies=[FastAPIAuthenticated(scopes=["orders:write"])]
    )
    async def cancel() -> dict[str, int]:
        return {"cancelled": 1}

    @app.get("/catalog", dependencies=[FastAPIAnonymous()])
    async def catalog() -> dict[str, bool]:
        return {"public": True}

    @app.get("/livez")
    async def probe() -> dict[str, bool]:
        return {"live": True}


def _litestar_routes() -> list[Any]:
    """Return the same routes as Litestar handlers."""

    @get("/items")
    async def items() -> dict[str, int]:
        return {"ok": 1}

    @post("/items", status_code=HTTP_200_OK)
    async def create() -> dict[str, int]:
        return {"ok": 1}

    @put("/items")
    async def replace() -> dict[str, int]:
        return {"ok": 1}

    @delete(
        "/orders",
        guards=[LitestarAuthenticated(scopes=["orders:write"])],
        status_code=HTTP_200_OK,
    )
    async def cancel() -> dict[str, int]:
        return {"cancelled": 1}

    @get("/catalog", opt=LitestarAnonymous())
    async def catalog() -> dict[str, bool]:
        return {"public": True}

    @get("/livez")
    async def probe() -> dict[str, bool]:
        return {"live": True}

    return [items, create, replace, cancel, catalog, probe]


def _late_route() -> Any:  # noqa: ANN401
    """Return a handler registered once the app is built."""

    @get("/late")
    async def late() -> dict[str, bool]:
        return {"late": True}

    return late


def _fastapi_parts(*, openapi: bool = True) -> dict[tuple[str, ...], Any]:
    """Return what grelmicro adds to the schema FastAPI builds."""
    bare = FastAPI()
    _fastapi_routes(bare)
    app = FastAPI()
    _fastapi_routes(app)
    Grelmicro(uses=_every_component(openapi=openapi)).install(app)
    return _grelmicro_parts(_rendered(bare.openapi()), _rendered(app.openapi()))


def _litestar_app(*, openapi: bool = True, **options: Any) -> Litestar:  # noqa: ANN401
    """Return the same app served by Litestar, with every component."""
    app = Litestar(route_handlers=_litestar_routes(), **options)
    Grelmicro(uses=_every_component(openapi=openapi)).install(app)
    return app


def _litestar_built(app: Litestar) -> dict[str, Any]:
    """Return the schema Litestar builds before the app starts."""
    return app.openapi_schema.to_schema()


def _litestar_started(app: Litestar) -> dict[str, Any]:
    """Return the schema Litestar builds once the app started."""
    with LitestarTestClient(app):
        return app.openapi_schema.to_schema()


def _litestar_served(app: Litestar) -> dict[str, Any]:
    """Return the schema a started Litestar app serves."""
    with LitestarTestClient(app) as client:
        return client.get("/schema/openapi.json").json()


def _litestar_parts(schema: dict[str, Any]) -> dict[tuple[str, ...], Any]:
    """Return what grelmicro adds to the schema Litestar builds."""
    bare = Litestar(route_handlers=_litestar_routes()).openapi_schema
    return _grelmicro_parts(_rendered(bare.to_schema()), _rendered(schema))


def _two_installs() -> list[Grelmicro]:
    """Return a conditional-requests app and an authenticating one, for one framework app."""
    return [
        Grelmicro(uses=[ConditionalRequests()]),
        Grelmicro(
            uses=[AuthenticatedRequests(verifier(), exclude=("/schema/*",))]
        ),
    ]


def _fastapi_installed_twice() -> dict[str, Any]:
    """Return the schema of a FastAPI app two `Grelmicro` apps installed on."""
    app = FastAPI()
    _fastapi_routes(app)
    for micro in _two_installs():
        micro.install(app)
    return app.openapi()


def _fastapi_installed(times: int) -> dict[str, Any]:
    """Return the schema of a FastAPI app one `Grelmicro` was installed on `times` times."""
    app = FastAPI()
    _fastapi_routes(app)
    micro = Grelmicro(uses=_every_component())
    for _ in range(times):
        micro.install(app)
    return app.openapi()


def _litestar_installed(times: int) -> dict[str, Any]:
    """Return the schema a Litestar app one `Grelmicro` was installed on `times` times serves."""
    app = Litestar(route_handlers=_litestar_routes())
    micro = Grelmicro(uses=_every_component())
    for _ in range(times):
        micro.install(app)
    with LitestarTestClient(app) as client:
        return client.get("/schema/openapi.json").json()


def _litestar_installed_twice() -> dict[str, Any]:
    """Return the schema a Litestar app two `Grelmicro` apps installed on serves."""
    app = Litestar(route_handlers=_litestar_routes())
    for micro in _two_installs():
        micro.install(app)
    with LitestarTestClient(app) as client:
        return client.get("/schema/openapi.json").json()


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
    assert schema["info"]["description"] == "recorded"
    [(routes, handed)] = recording.calls
    assert handed is errors
    assert (
        RouteDeclaration("/carts/{cart_id}", methods=frozenset({"PUT"}))
        in routes
    )


def test_litestar_install_hands_the_built_schema_to_each_component() -> None:
    """The schema Litestar builds reaches the edit once, with the routes and the format."""

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
    assert schema["info"]["description"] == "recorded"
    [(routes, handed)] = recording.calls
    assert handed is errors
    assert (
        RouteDeclaration("/carts/{cart_id}", methods=frozenset({"PUT"}))
        in routes
    )


@pytest.mark.parametrize("openapi", [True, False], ids=["on", "off"])
@pytest.mark.parametrize(
    "schema_of",
    [
        pytest.param(_litestar_built, id="built"),
        pytest.param(_litestar_started, id="started"),
        pytest.param(_litestar_served, id="served"),
    ],
)
def test_litestar_app_publishes_every_part_the_same_fastapi_app_publishes(
    schema_of: Callable[[Litestar], dict[str, Any]],
    openapi: bool,  # noqa: FBT001
) -> None:
    """Every path and component grelmicro adds reads the same on both."""
    # Arrange
    published = _fastapi_parts(openapi=openapi)

    # Act
    parts = _litestar_parts(schema_of(_litestar_app(openapi=openapi)))

    # Assert
    assert parts == published


def test_fastapi_app_publishes_the_parts_the_litestar_app_is_compared_with() -> (
    None
):
    """The scheme, each requirement, each header and each refusal."""
    # Act
    published = _fastapi_parts()

    # Assert
    scheme = ("components", "securitySchemes", _SCHEME)
    assert {
        key[-1]: value for key, value in published.items() if key[:3] == scheme
    } == {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    assert published["paths", "/items", "get", "security"] == [{_SCHEME: []}]
    assert published["paths", "/orders", "delete", "security"] == [
        {_SCHEME: ["orders:write"]}
    ]
    assert published["paths", "/catalog", "get", "security"] == [
        {},
        {_SCHEME: []},
    ]
    assert ("paths", "/livez", "get", "security") not in published
    named = {key[:5] for key in published}
    assert ("paths", "/items", "get", "responses", "401") in named
    assert ("paths", "/items", "get", "responses", "429") in named
    assert ("paths", "/orders", "delete", "responses", "403") in named
    parameters = published["paths", "/items", "put", "parameters"]
    assert {"If-Match"} <= {parameter["name"] for parameter in parameters}
    assert any(parameter.get("required") for parameter in parameters)
    assert "Idempotency-Key" in {
        parameter["name"]
        for parameter in published["paths", "/items", "post", "parameters"]
    }


def test_fastapi_app_with_openapi_off_publishes_nothing() -> None:
    """`openapi=False` leaves the schema as the framework built it."""
    # Act
    published = _fastapi_parts(openapi=False)

    # Assert
    assert published == {}


def test_litestar_member_an_edit_removes_is_gone_from_the_schema_object() -> (
    None
):
    """A component removing a member removes it from `app.openapi_schema`."""

    # Arrange
    @get("/items")
    async def items() -> dict[str, int]:
        return {"ok": 1}

    app = Litestar(route_handlers=[items])
    Grelmicro(uses=[_Removing()]).install(app)

    # Act
    paths = app.openapi_schema.paths

    # Assert
    assert paths is not None
    operation = paths["/items"].get
    assert operation is not None
    assert operation.summary is None


def test_litestar_member_the_spec_cannot_hold_fails_install() -> None:
    """A member no Litestar spec object has a field for is refused, naming it."""
    # Arrange
    app = Litestar(route_handlers=_litestar_routes())

    # Act / Assert
    with pytest.raises(TypeError, match="x-audience"):
        Grelmicro(uses=[_Extending()]).install(app)


def test_litestar_schema_object_carries_the_refusals_as_spec_objects() -> None:
    """`app.openapi_schema` holds each refusal as Litestar's own response."""
    # Arrange
    app = _litestar_app()

    # Act
    paths = app.openapi_schema.paths

    # Assert
    assert paths is not None
    operation = paths["/items"].get
    assert operation is not None
    assert operation.responses is not None
    assert isinstance(operation.responses["401"], OpenAPIResponse)
    assert isinstance(operation.responses["429"], OpenAPIResponse)


def test_litestar_change_to_the_schema_object_reaches_the_served_schema() -> (
    None
):
    """The served document is rendered from `app.openapi_schema`."""
    # Arrange
    app = _litestar_app()

    # Act
    app.openapi_schema.info.title = "Mutated"
    with LitestarTestClient(app) as client:
        served = client.get("/schema/openapi.json").json()

    # Assert
    assert served["info"]["title"] == "Mutated"
    assert served["paths"]["/items"]["get"]["security"] == [{_SCHEME: []}]
    assert served == app.openapi_schema.to_schema()


def test_litestar_openapi_plugin_is_found_by_its_name() -> None:
    """Litestar's plugin registry still finds the plugin that builds the schema."""
    # Arrange
    app = _litestar_app()

    # Act
    plugin: Any = app.plugins.get("OpenAPIPlugin")

    # Assert
    assert plugin.provide_openapi() is app.openapi_schema


def test_litestar_openapi_controller_serves_the_described_schema() -> None:
    """An app serving its schema from a controller serves the same document."""
    # Arrange
    with pytest.warns(LitestarDeprecationWarning, match="openapi_controller"):
        config = OpenAPIConfig(
            title="Items", version="1", openapi_controller=OpenAPIController
        )
    app = _litestar_app(openapi_config=config)

    # Act
    with LitestarTestClient(app) as client:
        served = client.get("/schema/openapi.json").json()

    # Assert
    assert served["paths"]["/items"]["get"]["security"] == [{_SCHEME: []}]
    assert served == app.openapi_schema.to_schema()


def test_litestar_app_keeps_its_own_render_plugins_and_components() -> None:
    """Its own render plugin, scheme and root requirement stay as declared."""
    # Arrange
    own = SecurityScheme(
        type="apiKey", name="X-Key", security_scheme_in="header"
    )
    ours = SecurityScheme(type="http", scheme="basic")
    config = OpenAPIConfig(
        title="Items",
        version="1",
        security=[{"key": []}],
        render_plugins=[SwaggerRenderPlugin()],
        components=Components(security_schemes={"key": own, _SCHEME: ours}),
    )
    app = _litestar_app(openapi_config=config)

    # Act
    with LitestarTestClient(app) as client:
        page = client.get("/schema/swagger")
        served = client.get("/schema/openapi.json").json()

    # Assert
    assert page.status_code == HTTP_200_OK
    assert app.openapi_config is config
    assert "ProblemDetail" in served["components"]["schemas"]
    assert served["components"]["securitySchemes"] == {
        "key": {"type": "apiKey", "name": "X-Key", "in": "header"},
        _SCHEME: {"type": "http", "scheme": "basic"},
    }
    assert served["paths"]["/items"]["get"]["security"] == [
        {"key": [], _SCHEME: []}
    ]


def test_litestar_handler_on_two_paths_is_secured_per_path() -> None:
    """An excluded path names nothing, and the covered one requires the scheme."""

    # Arrange
    @get(["/livez", "/readyz"])
    async def probe() -> dict[str, bool]:
        return {"live": True}

    app = Litestar(route_handlers=[probe])
    Grelmicro(uses=_authenticated()).install(app)

    # Act
    paths = app.openapi_schema.paths

    # Assert
    assert paths is not None
    livez, readyz = paths["/livez"].get, paths["/readyz"].get
    assert livez is not None
    assert readyz is not None
    assert livez.security is None
    assert readyz.security == [{_SCHEME: []}]
    assert readyz.responses is not None
    assert "401" in readyz.responses
    assert livez.responses is not None
    assert "401" not in livez.responses


@pytest.mark.parametrize("started", [False, True], ids=["before", "after"])
def test_litestar_route_registered_after_install_is_described(
    started: bool,  # noqa: FBT001
) -> None:
    """A late route rebuilds the schema, and the rebuilt one is described too."""
    # Arrange
    app = _litestar_app()
    app.openapi_schema.to_schema()
    if not started:
        app.register(_late_route())

    # Act
    with LitestarTestClient(app) as client:
        if started:
            app.register(_late_route())
        served = client.get("/schema/openapi.json").json()

    # Assert
    for schema in (served, app.openapi_schema.to_schema()):
        assert schema["paths"]["/late"]["get"]["security"] == [{_SCHEME: []}]
        assert schema["paths"]["/items"]["get"]["security"] == [{_SCHEME: []}]
        assert _SCHEME in schema["components"]["securitySchemes"]


def test_litestar_header_joins_the_parameters_litestar_declares() -> None:
    """A path parameter Litestar declares stays beside the header a component adds."""

    # Arrange
    @put("/carts/{cart_id:int}")
    async def replace(cart_id: FromPath[int]) -> dict[str, int]:
        return {"cart": cart_id}

    app = Litestar(route_handlers=[replace])
    Grelmicro(uses=_components()).install(app)

    # Act
    paths = app.openapi_schema.paths

    # Assert
    assert paths is not None
    operation = paths["/carts/{cart_id}"].put
    assert operation is not None
    assert operation.parameters is not None
    assert [
        getattr(parameter, "name", None) for parameter in operation.parameters
    ] == [
        "cart_id",
        "If-Match",
    ]


def test_litestar_websocket_registered_after_install_keeps_the_schema() -> None:
    """A route the schema does not list leaves the described schema as it is."""

    # Arrange
    @websocket("/feed")
    async def feed(socket: WebSocket) -> None:
        await socket.close()

    app = _litestar_app()
    built = app.openapi_schema

    # Act
    app.register(feed)

    # Assert
    assert app.openapi_schema is built
    assert built.to_schema()["paths"]["/items"]["get"]["security"] == [
        {_SCHEME: []}
    ]


@pytest.mark.parametrize(
    ("move", "missing"),
    [
        pytest.param(
            lambda plugins: delattr(plugins, "receive_route"),
            r"PluginRegistry\.receive_route,",
            id="absent",
        ),
        pytest.param(
            lambda plugins: setattr(plugins, "receive_route", []),
            r"PluginRegistry\.receive_route as a tuple",
            id="list",
        ),
        pytest.param(
            lambda plugins: setattr(
                plugins, "receive_route", ReceiveRoutePlugin()
            ),
            r"PluginRegistry\.receive_route as a tuple",
            id="one plugin",
        ),
        pytest.param(
            lambda plugins: setattr(plugins, "receive_route", (object(),)),
            r"PluginRegistry\.receive_route holding ReceiveRoutePlugin alone",
            id="not a plugin",
        ),
    ],
)
def test_litestar_registry_without_route_plugins_fails_install(
    move: Callable[[Any], None],
    missing: str,
) -> None:
    """A Litestar release that moved its route plugins is refused, naming the part."""
    # Arrange
    app = Litestar(route_handlers=_litestar_routes())
    move(app.plugins)

    # Act / Assert
    with pytest.raises(RuntimeError, match=missing):
        Grelmicro(uses=_authenticated()).install(app)


@pytest.mark.parametrize(
    "schema_of",
    [
        pytest.param(_fastapi_installed_twice, id="fastapi"),
        pytest.param(_litestar_installed_twice, id="litestar"),
    ],
)
def test_second_install_on_one_app_describes_its_own_components(
    schema_of: Callable[[], dict[str, Any]],
) -> None:
    """Each `Grelmicro` installed on the app describes the components it carries."""
    # Act
    operation = schema_of()["paths"]["/items"]["get"]

    # Assert
    assert operation["security"] == [{_SCHEME: []}]
    assert "If-None-Match" in {
        parameter["name"] for parameter in operation["parameters"]
    }


@pytest.mark.parametrize(
    "installed",
    [
        pytest.param(_fastapi_installed, id="fastapi"),
        pytest.param(_litestar_installed, id="litestar"),
    ],
)
def test_same_install_twice_on_one_app_describes_each_part_once(
    installed: Callable[[int], dict[str, Any]],
) -> None:
    """One scheme, one requirement per operation, and no repeated header or refusal."""
    # Arrange
    once = installed(1)

    # Act
    twice = installed(2)

    # Assert
    assert twice == once
    assert list(twice["components"]["securitySchemes"]) == [_SCHEME]
    for path_item in twice["paths"].values():
        for operation in path_item.values():
            security = operation.get("security", [])
            assert len(security) == len(
                {tuple(sorted(alternative)) for alternative in security}
            )
            names = [
                (parameter["in"], parameter["name"].lower())
                for parameter in operation.get("parameters", [])
            ]
            assert len(names) == len(set(names))
    assert twice["paths"]["/items"]["get"]["security"] == [{_SCHEME: []}]
    assert twice["paths"]["/catalog"]["get"]["security"] == [{}, {_SCHEME: []}]


def test_litestar_app_publishing_no_schema_installs_without_one() -> None:
    """`openapi_config=None` is installed, and still publishes nothing."""
    # Arrange
    app = Litestar(route_handlers=_litestar_routes(), openapi_config=None)
    plugins = app.plugins.receive_route

    # Act
    Grelmicro(uses=_every_component()).install(app)

    # Assert
    assert app.plugins.receive_route == plugins
    with LitestarTestClient(app) as client:
        assert client.get("/schema/openapi.json").status_code == (
            HTTP_404_NOT_FOUND
        )


def test_litestar_operation_class_of_the_app_renders_beside_the_description() -> (
    None
):
    """An `operation_class` reading its own fields keeps its members and gains ours."""

    # Arrange
    @get("/items", operation_class=_TaggedOperation, tags=["Items"])
    async def items() -> dict[str, int]:
        return {"ok": 1}

    app = Litestar(route_handlers=[items])
    Grelmicro(uses=_authenticated()).install(app)

    # Act
    with LitestarTestClient(app) as client:
        served = client.get("/schema/openapi.json").json()

    # Assert
    operation = served["paths"]["/items"]["get"]
    assert operation["x-tags"] == ["ITEMS"]
    assert operation["security"] == [{_SCHEME: []}]
    assert "401" in operation["responses"]
