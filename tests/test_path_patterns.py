"""A set of path patterns is a set, and a string is not one.

`exclude="/internal/*"` is a missing comma. It is a sequence of characters,
so the matcher walks it one at a time, and the single `*` matches every path
as a prefix: the middleware then acts on nothing, or on everything, with
nothing said. Every door that takes patterns refuses one.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI
from starlette.authentication import AuthenticationBackend
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.routing import Mount, Route, Router, compile_path

from grelmicro._paths import (
    _dependency_topology,
    _is_starlette_routing_app,
    _middleware_boundaries,
    _nested_routing_app,
    _route_topology,
    _transparent_routing_source,
    matches,
    names_route,
    walk_routes,
)
from grelmicro.errors import SettingsValidationError
from grelmicro.http import (
    CachedResponses,
    ConditionalRequests,
    ConditionalRequestsMiddleware,
    IdempotencyMiddleware,
    IdempotentRequests,
    RateLimitedRequests,
)
from grelmicro.http._idempotency import _authentication_paths
from grelmicro.idempotency import Idempotency
from grelmicro.log import AccessLog, AccessLogMiddleware
from grelmicro.resilience import RateLimiter
from grelmicro.security import TrustedProxies


async def app(scope: object, receive: object, send: object) -> None:
    """Stand in for the app a middleware wraps."""


def test_route_walkers_treat_wrappers_and_middleware_as_boundaries() -> None:
    """Boundary discovery terminates on wrappers, includes, and cycles."""

    class CustomExceptionMiddleware(ExceptionMiddleware):
        """A user wrapper must not inherit the built-in routing exemption."""

    class Backend(AuthenticationBackend):
        async def authenticate(self, conn: Any) -> None:  # noqa: ANN401, ARG002
            return None

    # Arrange
    wrapped = SimpleNamespace(app=Router())
    builtin_exception = ExceptionMiddleware(Router())
    custom_exception = CustomExceptionMiddleware(Router())
    protected_exception = ExceptionMiddleware(
        AuthenticationMiddleware(Router(), backend=Backend())
    )
    included = Router(
        middleware=[
            Middleware(CORSMiddleware, allow_origins=["https://example.test"])
        ]
    )
    inclusion = SimpleNamespace(
        original_router=included,
        include_context=SimpleNamespace(prefix="/api"),
    )
    root = SimpleNamespace(routes=[inclusion])
    loop = SimpleNamespace()
    loop.routes = [
        SimpleNamespace(
            original_router=None,
            routes=[],
            path="/loop",
            app=loop,
        )
    ]

    # Act / Assert
    assert _middleware_boundaries(wrapped) == {("", True)}
    assert _middleware_boundaries(builtin_exception) == set()
    assert _middleware_boundaries(custom_exception) == {("", True)}
    assert _middleware_boundaries(protected_exception) == {("", True)}
    assert walk_routes(wrapped) == []
    assert _middleware_boundaries(root) == {("/api", True)}
    assert _middleware_boundaries(loop) == set()


def test_direct_mount_and_bound_router_keep_their_routing_context() -> None:
    """Direct routing nodes retain prefixes and configured boundaries."""
    # Arrange
    route = Route("/items", app)
    router = Router(routes=[route])
    mounted = Mount("/api", app=router)
    protected = Mount(
        "/private",
        app=Router(
            routes=[route],
            middleware=[
                Middleware(
                    CORSMiddleware,
                    allow_origins=["https://example.test"],
                )
            ],
        ),
    )

    # Act / Assert
    assert _middleware_boundaries(mounted) == set()
    assert [
        (prefix, found.path) for prefix, found, _ in walk_routes(mounted)
    ] == [("/api", "/items")]
    assert [
        (prefix, found.path) for prefix, found, _ in walk_routes(router.app)
    ] == [("", "/items")]
    assert [
        (prefix, found.path) for prefix, found, _ in walk_routes(route)
    ] == [("", "/items")]
    assert walk_routes(protected) == []


def test_routing_shape_helpers_handle_mounts_and_broken_endpoints() -> None:
    """Optional route discovery recognizes mounts and rejects unusable leaves."""
    # Arrange
    mounted = Mount("/api", app=Router())
    recursive = SimpleNamespace(routes=None)
    recursive.app = recursive
    dependency = SimpleNamespace(call=None, dependencies=[])
    dependency.dependencies.append(dependency)

    # Act / Assert
    assert not _is_starlette_routing_app(None)
    assert _is_starlette_routing_app(mounted)
    assert _nested_routing_app(recursive) is None
    assert _route_topology(None) == ("none",)
    assert _dependency_topology(dependency)[2] == (("cycle", id(dependency)),)
    assert _transparent_routing_source(None) is None
    broken_exception = ExceptionMiddleware(cast("Any", None))
    assert _transparent_routing_source(broken_exception) is broken_exception


def test_direct_route_authentication_flattens_nested_router_boundaries() -> (
    None
):
    """A direct Route keeps protected leaf routers exact and public ones open."""

    class Backend(AuthenticationBackend):
        async def authenticate(self, conn: Any) -> None:  # noqa: ANN401, ARG002
            return None

    protected = Router(
        routes=[
            Route(
                "/private",
                app,
                middleware=[
                    Middleware(
                        AuthenticationMiddleware,
                        backend=Backend(),
                    )
                ],
            )
        ]
    )
    public = Router(routes=[Route("/public", app)])

    assert _authentication_paths(Route("/private", protected)) == {
        ("/private", False)
    }
    assert _authentication_paths(Route("/public", public)) == set()


def test_route_walker_stops_cycles_per_path_not_globally() -> None:
    """Cycles stop while repeated mounts retain both distinct prefixes."""
    # Arrange
    child = Router(routes=[Route("/items", app)])
    repeated = Router(
        routes=[Mount("/one", app=child), Mount("/two", app=child)]
    )
    cyclic = FastAPI()

    @cyclic.post("/charge")
    async def charge() -> dict[str, bool]:
        return {"charged": True}

    cyclic.mount("/v1", cyclic)

    # Act
    repeated_paths = [
        f"{prefix}{route.path}"
        for prefix, route, _contexts in walk_routes(repeated)
    ]
    cyclic_paths = [
        f"{prefix}{route.path}"
        for prefix, route, _contexts in walk_routes(cyclic)
    ]
    before = _route_topology(cyclic)
    cyclic.add_api_route("/later", charge, methods=["POST"])
    after = _route_topology(cyclic)

    # Assert
    assert repeated_paths == ["/one/items", "/two/items"]
    assert cyclic_paths.count("/charge") == 1
    assert before != after


def components() -> list[tuple[str, Any]]:
    """Return every component that takes path patterns, with its name."""
    return [
        ("AccessLog", AccessLog),
        ("IdempotentRequests", IdempotentRequests),
        ("ConditionalRequests", ConditionalRequests),
        ("CachedResponses", CachedResponses),
        ("RateLimitedRequests", _rate_limited),
    ]


def _rate_limited(**kwargs: Any) -> RateLimitedRequests:  # noqa: ANN401
    """Build a `RateLimitedRequests` with the one limiter it requires."""
    return RateLimitedRequests(
        RateLimiter.sliding_window("sweep", limit=10, window=60),
        trusted=TrustedProxies(["10.0.0.0/8"]),
        **kwargs,
    )


@pytest.mark.parametrize(("name", "component"), components())
@pytest.mark.parametrize("field", ["include", "exclude"])
def test_a_component_refuses_a_bare_string(
    name: str,  # noqa: ARG001
    component: Any,  # noqa: ANN401
    field: str,
) -> None:
    """The component says so where the mistake is written.

    A component field is a setting, so it refuses with the one error
    every component raises for a bad value. The middleware under it is
    hand-wired ASGI, where a wrong argument type is a `TypeError`.
    """
    mistake = cast("Any", {field: "/internal/*"})

    with pytest.raises(SettingsValidationError, match="is a string"):
        component(**mistake)


@pytest.mark.parametrize("field", ["include", "exclude"])
def test_the_conditional_middleware_refuses_a_bare_string(
    field: str,
) -> None:
    """The middleware is public too, and wired by hand as often as not."""
    mistake = cast("Any", {field: "/internal/*"})

    with pytest.raises(TypeError, match="is a string"):
        ConditionalRequestsMiddleware(app, **mistake)


@pytest.mark.parametrize("field", ["include", "exclude"])
def test_the_idempotency_middleware_refuses_a_bare_string(
    field: str,
) -> None:
    """The same, for the one that replays a stored response."""
    mistake = cast("Any", {field: "/internal/*"})

    with pytest.raises(TypeError, match="is a string"):
        IdempotencyMiddleware(app, idempotency=Idempotency("test"), **mistake)


@pytest.mark.parametrize("field", ["include", "exclude", "quiet"])
def test_the_access_log_middleware_refuses_a_bare_string(
    field: str,
) -> None:
    """And the one that writes a record for every request."""
    mistake = cast("Any", {field: "/internal/*"})

    with pytest.raises(TypeError, match="is a string"):
        AccessLogMiddleware(app, **mistake)


def test_a_tuple_of_patterns_is_taken_as_it_is() -> None:
    """The shape that was always meant still works, list or tuple."""
    assert AccessLog(exclude=("/internal/*",))
    assert AccessLogMiddleware(app, exclude=cast("Any", ["/internal/*"]))


@pytest.mark.parametrize(
    ("pattern", "template", "url"),
    [
        pytest.param(
            "/users/me/*",
            "/users/{uid}/settings",
            "/users/me/settings",
            id="prefix-past-a-parameter",
        ),
        pytest.param(
            "/users/me/*", "/users/{uid}", "/users/me", id="prefix-is-the-path"
        ),
        pytest.param(
            "/products/ho*",
            "/products/{pid}",
            "/products/hot",
            id="prefix-cuts-into-a-parameter",
        ),
        pytest.param(
            "/products/co*",
            "/products/cold",
            "/products/cold",
            id="prefix-cuts-into-a-literal",
        ),
        pytest.param(
            "/products/*",
            "/products/{pid}",
            "/products/x",
            id="prefix-names-the-router",
        ),
        pytest.param(
            "/orders/*", "/users/{uid}", "/orders/1", id="another-router"
        ),
        pytest.param(
            "/rest/*", "/rest/{rest:path}", "/rest/a/b", id="a-path-converter"
        ),
        pytest.param(
            "/rest/a/b/*",
            "/rest/{rest:path}",
            "/rest/a/b/c",
            id="past-a-path-converter",
        ),
        pytest.param(
            "/users/*", "/products/{pid}", "/users/x", id="names-nothing"
        ),
        pytest.param(
            # Diverges before the last segment, so the walk stops there
            # rather than at the segment the prefix cuts into.
            "/api/v2/x*",
            "/api/v1/{pid}",
            "/api/v2/xy",
            id="diverges-early",
        ),
        pytest.param(
            "/users/me", "/users/{uid}", "/users/me", id="exact-names-a-url"
        ),
        pytest.param(
            "/products", "/products", "/products", id="exact-literal-route"
        ),
        pytest.param(
            # Written as the route was declared, which selects only a
            # request for that literal path. A client sends a URL, not a
            # template, so this is a mistake the check should surface.
            "/users/{uid}",
            "/users/{uid}",
            "/users/{uid}",
            id="exact-is-the-template",
        ),
    ],
)
def test_names_route_agrees_with_what_runs(
    pattern: str, template: str, url: str
) -> None:
    """Every spelling, because a guard written for one leaves the rest open.

    `names_route` decides whether the response cache refuses a pattern
    naming a gated read, and whether the endpoint table reports one, so
    it has to answer exactly what the middleware answers at request
    time: does this pattern select a request this route serves.
    """
    # Arrange
    regex, _, _ = compile_path(template)

    # Act
    named = names_route(pattern, template, regex)
    runs = matches(url, (pattern,)) and bool(regex.fullmatch(url))

    # Assert
    assert named is runs
