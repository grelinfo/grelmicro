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
from fastapi import APIRouter, Depends, FastAPI
from starlette.authentication import AuthenticationBackend
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.routing import Mount, Route, Router, compile_path

from grelmicro._paths import (
    _is_starlette_routing_app,
    _middleware_boundaries,
    _nested_routing_app,
    _request_authority,
    _RouteTopologyState,
    _routing_root,
    _same_routing_root,
    _TopologyWatch,
    _transparent_routing_source,
    _watch_topology_node,
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
from grelmicro.integrations.fastapi import CachedResponse
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
    assert _middleware_boundaries(wrapped) == {("", True, None)}
    assert _middleware_boundaries(builtin_exception) == set()
    assert _middleware_boundaries(custom_exception) == {("", True, None)}
    assert _middleware_boundaries(protected_exception) == {("", True, None)}
    assert walk_routes(wrapped) == []
    assert _middleware_boundaries(root) == {("/api", True, None)}
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
    # Act / Assert
    assert not _is_starlette_routing_app(None)
    assert _is_starlette_routing_app(mounted)
    assert _nested_routing_app(recursive) is None
    assert _transparent_routing_source(None) is None
    broken_exception = ExceptionMiddleware(cast("Any", None))
    assert _transparent_routing_source(broken_exception) is broken_exception


def test_routing_root_distinguishes_mount_coordinates_from_wrapped_sources() -> (
    None
):
    """Wrapped app entry points share a root; a mount is a different root."""
    router = Router(routes=[Route("/x", app)])
    mounted = Mount("/api", app=router)
    web = FastAPI()

    assert _routing_root(router.app) is router
    assert _routing_root(mounted) is mounted
    assert _same_routing_root(web, web.router.app)
    assert not _same_routing_root(web, router)


def test_topology_generation_collects_router_declared_dependencies() -> None:
    """Router-level dependencies participate in the guarded full snapshot."""
    router = APIRouter(dependencies=[Depends(app)])

    snapshot = _RouteTopologyState(router)

    assert not snapshot.changed()
    assert snapshot.value


def test_topology_generation_detects_same_length_replacement_and_reorder() -> (
    None
):
    """Ordered route identities catch mutations that preserve list length."""
    first = Route("/first", app, methods=["GET"])
    second = Route("/second", app, methods=["GET"])
    router = Router(routes=[first, second])
    snapshot = _RouteTopologyState(router)

    router.routes[0] = Route("/replacement", app, methods=["GET"])
    assert snapshot.changed()

    snapshot.rebuild()
    router.routes.reverse()
    assert snapshot.changed()


def test_topology_generation_detects_policy_mutation_on_existing_nodes() -> (
    None
):
    """Dependency, method, authentication, and middleware edits invalidate."""

    class Backend(AuthenticationBackend):
        async def authenticate(self, conn: Any) -> None:  # noqa: ANN401, ARG002
            return None

    web = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @web.get("/x")
    async def x() -> None:
        return None

    route = cast("Any", web.routes[0])
    snapshot = _RouteTopologyState(web)

    route.methods.add("POST")
    assert snapshot.changed()

    snapshot.rebuild()
    dependency = SimpleNamespace(
        call=app,
        dependencies=[],
        dependency_overrides_provider=None,
    )
    route.dependant.dependencies.append(dependency)  # codespell:ignore
    assert snapshot.changed()

    snapshot.rebuild()
    dependency.call = x
    assert snapshot.changed()

    snapshot.rebuild()
    route.app = AuthenticationMiddleware(route.app, backend=Backend())
    assert snapshot.changed()

    snapshot.rebuild()
    web.user_middleware.append(
        Middleware(CORSMiddleware, allow_origins=["https://example.test"])
    )
    assert snapshot.changed()


def test_topology_generation_watches_nothing_without_an_application() -> None:
    """An entry point with no application holds a signature that never moves."""
    snapshot = _RouteTopologyState(None)

    assert not any(snapshot.value)
    assert not snapshot.changed()


def test_route_topology_reads_a_bound_router_as_its_router() -> None:
    """A bound `Router.app` describes the router it belongs to, not a wrapper."""
    router = Router(routes=[Route("/x", app)])

    assert (
        _RouteTopologyState(router.app).value
        == _RouteTopologyState(router).value
    )


def test_snapshot_walk_watches_the_nodes_whose_later_edits_matter() -> None:
    """A snapshot walk handed a watch registers what a later edit can change."""

    class Backend(AuthenticationBackend):
        async def authenticate(self, conn: Any) -> None:  # noqa: ANN401, ARG002
            return None

    child = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @child.get("/x")
    async def x() -> None:
        return None

    web = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(app)],
    )
    web.mount("/sub", child)
    web.mount("/other", Router())
    guarded = AuthenticationMiddleware(web, backend=Backend())

    watch = _TopologyWatch()
    _watch_topology_node(guarded, frozenset(), watch=watch)

    # The wrapper, so replacing the application it delegates to is seen.
    before = watch.signature()
    guarded.app = Router()
    assert watch.signature() != before

    # The mounted route, so swapping the application behind it is seen.
    before = watch.signature()
    cast("Any", web.routes[-1]).app = Router()
    assert watch.signature() != before

    # The leaf route, so widening the methods it answers is seen.
    before = watch.signature()
    cast("Any", child.routes[0]).methods.add("POST")
    assert watch.signature() != before

    # The router, so replacing a route in place is seen.
    before = watch.signature()
    child.router.routes[0] = Route("/replacement", app)
    assert watch.signature() != before

    # The override provider each holder and dependency resolved to.
    before = watch.signature()
    web.dependency_overrides[app] = x
    assert watch.signature() != before


def test_route_topology_tracks_dependency_override_identities() -> None:
    """Override keys, values, and effective calls invalidate route policy."""
    web = FastAPI()
    marker = CachedResponse()

    @web.get("/x", dependencies=[marker])
    async def x() -> None:
        return None

    async def first() -> None:
        return None

    async def second() -> None:
        return None

    async def unrelated() -> None:
        return None

    original = _RouteTopologyState(web).value
    web.dependency_overrides[marker.dependency] = first
    first_override = _RouteTopologyState(web).value
    web.dependency_overrides[marker.dependency] = second
    second_override = _RouteTopologyState(web).value
    web.dependency_overrides[unrelated] = first
    unrelated_override = _RouteTopologyState(web).value
    web.dependency_overrides.clear()

    assert first_override != original
    assert second_override != first_override
    assert unrelated_override != second_override
    assert _RouteTopologyState(web).value == original


def test_included_topology_inherits_an_empty_contexts_override_provider() -> (
    None
):
    """Provider identity and mappings count without include dependencies."""
    web = FastAPI()
    router = FastAPI().router
    marker = CachedResponse()

    @router.get("/x", dependencies=[marker])
    async def x() -> None:
        return None

    web.include_router(router)
    inclusion = web.routes[-1]
    context = cast("Any", inclusion).include_context
    assert context.dependencies == []

    original = _RouteTopologyState(web).value
    replacement = SimpleNamespace(dependency_overrides={})
    context.dependency_overrides_provider = replacement
    provider_changed = _RouteTopologyState(web).value
    replacement.dependency_overrides[marker.dependency] = app
    mapping_changed = _RouteTopologyState(web).value
    replacement.dependency_overrides.clear()

    assert provider_changed != original
    assert mapping_changed != provider_changed
    assert _RouteTopologyState(web).value == provider_changed


def test_included_topology_ignores_shadowed_child_override_providers() -> None:
    """Only the outer include provider can change its effective topology."""
    marker = CachedResponse()
    child = FastAPI()

    @child.get("/x", dependencies=[marker])
    async def x() -> None:
        return None

    parent = FastAPI()
    parent.include_router(child.router)
    original = _RouteTopologyState(parent).value

    async def child_override() -> None:
        return None

    child.dependency_overrides[marker.dependency] = child_override
    child_changed = _RouteTopologyState(parent).value

    async def parent_override() -> None:
        return None

    parent.dependency_overrides[marker.dependency] = parent_override

    assert child_changed == original
    assert _RouteTopologyState(parent).value != original


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        pytest.param(
            {
                "scheme": "HTTP",
                "headers": [(b"host", b"Example.COM.:080")],
                "server": ("wrong.example", 9000),
            },
            "example.com.",
            id="host-default-port",
        ),
        pytest.param(
            {
                "scheme": b"https",
                "headers": [("Host", "[2001:0DB8::1]:443")],
            },
            "[2001:db8::1]",
            id="ipv6-host",
        ),
        pytest.param(
            {
                "scheme": "http",
                "headers": [(b"x-forwarded-host", b"untrusted.example")],
                "server": ("Example.COM", 8080),
            },
            "example.com:8080",
            id="server-fallback",
        ),
        pytest.param(
            {
                "scheme": "http",
                "headers": [],
                "server": (b"2001:0DB8::1", b"8080"),
            },
            "[2001:db8::1]:8080",
            id="ipv6-server-bytes",
        ),
        pytest.param(
            {
                "scheme": "http",
                "headers": [],
                "server": ("127.0.0.1",),
            },
            "127.0.0.1",
            id="server-without-port",
        ),
        pytest.param(
            {
                "scheme": "http",
                "headers": [],
                "server": ("Example.COM", 70000),
            },
            "example.com:70000",
            id="invalid-server-port-preserved",
        ),
        pytest.param(
            {
                "scheme": "http",
                "headers": [],
                "server": ("Example.COM", b"invalid"),
            },
            "example.com:invalid",
            id="invalid-server-port-bytes-preserved",
        ),
        pytest.param(
            {
                "scheme": "http",
                "headers": [(b"host", b"Example.COM:8080")],
            },
            "example.com:8080",
            id="host-non-default-port",
        ),
        pytest.param(
            {
                "scheme": "http",
                "headers": [(b"host", b"2001:0DB8::1")],
            },
            "[2001:db8::1]",
            id="bare-ipv6-host",
        ),
        pytest.param(
            {
                "scheme": "http",
                "headers": [(b"host", b"Example.COM:invalid")],
            },
            "example.com:invalid",
            id="invalid-port-preserved",
        ),
        pytest.param(
            {
                "scheme": "http",
                "headers": [(b"host", b"Example.COM:70000")],
            },
            "example.com:70000",
            id="out-of-range-port-preserved",
        ),
        pytest.param(
            {
                "scheme": "http",
                "headers": [(b"host", b"[2001:DB8::1]:invalid")],
            },
            "[2001:db8::1]:invalid",
            id="invalid-ipv6-port-preserved",
        ),
        pytest.param(
            {
                "scheme": "http",
                "headers": [(b"host", b"[2001:DB8::1]suffix")],
            },
            "[2001:db8::1]suffix",
            id="invalid-ipv6-suffix-preserved",
        ),
        pytest.param(
            {
                "scheme": "http",
                "headers": [(b"host", b"[2001:DB8::1")],
            },
            "[2001:db8::1",
            id="unclosed-ipv6-preserved",
        ),
        pytest.param(
            {
                "scheme": "http",
                "headers": [(b"host", b" ")],
            },
            "",
            id="empty-host",
        ),
        pytest.param(
            {
                "scheme": "http",
                "headers": [(b"host", b":80")],
            },
            ":80",
            id="missing-host-preserved",
        ),
        pytest.param(
            {"scheme": "http", "headers": []},
            "",
            id="no-authority",
        ),
    ],
)
def test_request_authority_is_canonical_and_uses_server_fallback(
    scope: dict[str, Any],
    expected: str,
) -> None:
    """Authority follows Host routing and normalizes equivalent spellings."""
    assert _request_authority(scope) == expected


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
        ("/private", False, None)
    }
    assert _authentication_paths(Route("/public", public)) == set()


def test_authentication_around_a_single_route_keeps_its_methods() -> None:
    """Authentication over one route protects that path for its methods only."""

    class Backend(AuthenticationBackend):
        async def authenticate(self, conn: Any) -> None:  # noqa: ANN401, ARG002
            return None

    route = Route("/private", app, methods=["GET"])
    guarded = AuthenticationMiddleware(route, backend=Backend())

    assert _authentication_paths(guarded) == {
        ("/private", False, frozenset({"GET", "HEAD"}))
    }


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
    before = _RouteTopologyState(cyclic).value
    cyclic.add_api_route("/later", charge, methods=["POST"])
    after = _RouteTopologyState(cyclic).value

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
            "/api/*",
            "/api-admin",
            "/api-admin",
            id="slash-prefix-does-not-name-sibling",
        ),
        pytest.param(
            "/api-ad*",
            "/api-admin",
            "/api-admin",
            id="partial-final-segment-still-names-sibling",
        ),
        pytest.param(
            "/api-ad*",
            "/api-other",
            "/api-admin",
            id="partial-final-segment-names-nothing",
        ),
        pytest.param(
            "/orders/1/items/*",
            "/orders/{oid}",
            "/orders/1/items/x",
            id="prefix-runs-past-what-the-template-declares",
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
            "/other/a/b/*",
            "/rest/{rest:path}",
            "/other/a/b/c",
            id="path-converter-validates-preceding-segments",
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
