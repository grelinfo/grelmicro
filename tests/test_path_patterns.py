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
from litestar import Litestar
from litestar import Request as LitestarRequest
from litestar import get as litestar_get
from litestar.testing import TestClient as LitestarTestClient
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.exceptions import ExceptionMiddleware
from starlette.routing import Mount, Route, Router, compile_path

from grelmicro._paths import (
    _request_authority,
    _routing_app,
    _transparent_routing_source,
    matches,
    names_route,
    route_path,
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
from grelmicro.idempotency import Idempotency
from grelmicro.log import AccessLog, AccessLogMiddleware
from grelmicro.resilience import RateLimiter
from grelmicro.security import TrustedProxies


async def app(scope: object, receive: object, send: object) -> None:
    """Stand in for the app a middleware wraps."""


def test_route_walker_reads_nothing_through_a_wrapper() -> None:
    """A wrapper it cannot see into hides its routes."""
    # Arrange
    wrapped = SimpleNamespace(app=Router())

    # Act / Assert
    assert walk_routes(wrapped) == []


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


def test_transparent_routing_source_stops_at_what_it_cannot_unwrap() -> None:
    """Nothing, and an exception router wrapping nothing, are read as they are."""
    # Arrange
    broken_exception = ExceptionMiddleware(cast("Any", None))

    # Act / Assert
    assert _transparent_routing_source(None) is None
    assert _transparent_routing_source(broken_exception) is broken_exception


def test_route_walker_reads_through_plumbing_and_a_bound_router() -> None:
    """An exception router is looked through, and a wrapped bound router is read."""
    # Arrange
    router = Router(routes=[Route("/items", app)])

    # Act
    unwrapped = _transparent_routing_source(ExceptionMiddleware(router))
    walked = walk_routes(
        SimpleNamespace(app=router.app), unwrap_middleware=True
    )

    # Assert
    assert unwrapped is router
    assert [(prefix, found.path) for prefix, found, _ in walked] == [
        ("", "/items")
    ]


def test_route_walker_stops_at_a_mounted_router_with_middleware() -> None:
    """Routes behind middleware of their own are not read from above it."""
    # Arrange
    guarded = Router(
        routes=[Route("/items", app)],
        middleware=[
            Middleware(CORSMiddleware, allow_origins=["https://example.test"])
        ],
    )
    root = Router(routes=[Mount("/private", app=guarded)])

    # Act / Assert
    assert walk_routes(root) == []


def test_route_walker_reads_nothing_through_a_cycle_of_wrappers() -> None:
    """Two wrappers delegating to each other hide no route and end the walk."""
    # Arrange
    first = SimpleNamespace()
    second = SimpleNamespace(app=first)
    first.app = second

    # Act / Assert
    assert walk_routes(first, unwrap_middleware=True) == []


def test_route_walker_stops_at_a_mount_whose_app_is_wrapped() -> None:
    """A middleware wrapping the mounted app guards every route behind it."""
    # Arrange
    wrapped = CORSMiddleware(
        Router(routes=[Route("/items", app)]),
        allow_origins=["https://example.test"],
    )

    # Act / Assert
    assert walk_routes(Mount("/private", app=wrapped)) == []


def test_route_walker_stops_at_a_mounted_app_with_middleware_of_its_own() -> (
    None
):
    """Middleware the mounted app declares guards every route behind it."""
    # Arrange
    guarded = Starlette(
        routes=[Route("/items", app)],
        middleware=[
            Middleware(CORSMiddleware, allow_origins=["https://example.test"])
        ],
    )

    # Act / Assert
    assert walk_routes(Mount("/private", app=guarded)) == []


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


def test_routing_app_middleware_loop_returns_the_loop() -> None:
    """A malformed middleware cycle terminates without inventing routes."""

    # Arrange
    class Loop:
        app: Any

    loop = Loop()
    loop.app = loop

    # Act / Assert
    assert _routing_app(loop) is loop


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

    # Assert
    assert repeated_paths == ["/one/items", "/two/items"]
    assert cyclic_paths.count("/charge") == 1


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


@pytest.mark.parametrize(
    ("path", "root_path", "expected"),
    [
        ("/api/keys", "/api", "/keys"),
        ("/apikeys", "/api", "/apikeys"),
        ("/api", "/api", ""),
        ("/api/x", "/api/", "/api/x"),
        ("/api/", "/api/", ""),
        ("/other/x", "/api", "/other/x"),
        ("/x", "", "/x"),
    ],
)
def test_the_route_path_is_read_as_starlette_routes_it(
    path: str, root_path: str, expected: str
) -> None:
    """A root path is taken off whole, exactly as given, as Starlette does."""
    assert route_path({"path": path, "root_path": root_path}) == expected


@pytest.mark.parametrize(
    ("path", "root_path", "expected"),
    [
        ("/api/x/", "/api", "/x"),
        ("/api//x", "/api", "/x"),
        ("/v1/api/x", "/api", "/x"),
        ("/api", "/api", "/"),
        ("/livez/", "", "/livez"),
        ("//orders//7", "", "/orders/7"),
    ],
)
def test_the_route_path_is_read_as_litestar_routes_it(
    path: str, root_path: str, expected: str
) -> None:
    """Litestar takes the root path off where it first appears, and normalizes either way."""
    app = object()
    scope = {
        "path": path,
        "root_path": root_path,
        "app": app,
        "litestar_app": app,
    }

    assert route_path(scope) == expected


def test_a_path_litestar_routed_is_read_as_its_router_wrote_it() -> None:
    """Behind routing, the root path is already off, so it is not taken off again."""
    seen: list[str] = []

    @litestar_get("/files/v1/livez")
    async def livez(request: LitestarRequest[Any, Any, Any]) -> str:
        seen.append(route_path(cast("Any", request.scope)))
        return "live"

    with LitestarTestClient(Litestar([livez]), root_path="/v1") as client:
        client.get("/v1/files/v1/livez")

    assert seen == ["/files/v1/livez"]


def test_a_litestar_mount_its_router_does_not_list_is_refused() -> None:
    """Without the mount's own path, the path it serves is unknown.

    A real app always lists the mounts it owns, so the app and the mount
    are stand-ins.
    """
    app = SimpleNamespace(asgi_router=SimpleNamespace(_mount_routes={}))
    scope = {
        "path": "/livez/",
        "app": app,
        "litestar_app": app,
        "route_handler": SimpleNamespace(is_mount=True, owner=app),
    }

    with pytest.raises(RuntimeError, match="mount"):
        route_path(scope)


def test_an_app_mounted_under_litestar_is_read_by_its_own_router() -> None:
    """Litestar's key stays in the scope, and the app that set `app` routes."""
    scope = {
        "path": "/public/",
        "root_path": "/sub",
        "app": object(),
        "litestar_app": object(),
    }

    assert route_path(scope) == "/public/"
