"""A set of path patterns is a set, and a string is not one.

`exclude="/internal/*"` is a missing comma. It is a sequence of characters,
so the matcher walks it one at a time, and the single `*` matches every path
as a prefix: the middleware then acts on nothing, or on everything, with
nothing said. Every door that takes patterns refuses one.
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace
from typing import Any, cast

import pytest
from litestar import Litestar
from litestar import Request as LitestarRequest
from litestar import get as litestar_get
from litestar.testing import TestClient as LitestarTestClient
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount, Route, Router, compile_path

from grelmicro import _discovery, _paths
from grelmicro._paths import (
    _request_authority,
    matches,
    names_route,
    read_paths_with,
    route_path,
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
from grelmicro.integrations import _litestar_routes  # noqa: F401
from grelmicro.integrations._starlette_routes import _routing_app
from grelmicro.log import AccessLog, AccessLogMiddleware
from grelmicro.resilience import RateLimiter
from grelmicro.security import TrustedProxies


async def app(scope: object, receive: object, send: object) -> None:
    """Stand in for the app a middleware wraps."""


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


def test_routing_app_stops_at_a_wrapped_mount() -> None:
    """A mount behind a middleware is the routing node read."""
    # Arrange
    mount = Mount("/r", routes=[Route("/items", app)])

    # Act / Assert
    assert _routing_app(CORSMiddleware(mount, allow_origins=["*"])) is mount


def test_routing_app_reads_a_wrapped_bound_router_as_its_router() -> None:
    """A middleware around a router's bound `app` reads that router."""
    # Arrange
    router = Router(routes=[Route("/items", app)])

    # Act / Assert
    assert (
        _routing_app(CORSMiddleware(router.app, allow_origins=["*"])) is router
    )


def test_routing_app_middleware_loop_returns_the_loop() -> None:
    """A malformed middleware cycle terminates without inventing routes."""

    # Arrange
    class Loop:
        app: Any

    loop = Loop()
    loop.app = loop

    # Act / Assert
    assert _routing_app(loop) is loop


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


class _Router:
    """Stand in for an app whose router reads the path its own way."""


class _Unread:
    """Stand in for an app no integration gave a path reader."""


def _kind(cls: type) -> str:
    """Return the name `read_paths_with` names an app class by."""
    return f"{cls.__module__}.{cls.__qualname__}"


def _read_upper(app: object, scope: Any, path: str, root_path: str) -> str:  # noqa: ANN401, ARG001
    """Read the path in upper case, so a test sees this reader ran."""
    return path.upper()


@pytest.fixture
def readers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test path readers and routed keys of its own."""
    monkeypatch.setattr(_paths, "_PATH_READERS", {})
    monkeypatch.setattr(_paths, "_READERS_BY_CLASS", {})
    monkeypatch.setattr(_paths, "ROUTED_KEYS", ())


@pytest.mark.usefixtures("readers")
def test_a_path_no_integration_reads_is_read_by_the_asgi_rule() -> None:
    """An app with no path reader has its root path taken off at a segment boundary."""
    # Arrange
    app = _Unread()

    # Act
    under = route_path({"path": "/api/keys", "root_path": "/api", "app": app})
    beside = route_path({"path": "/apikeys", "root_path": "/api", "app": app})

    # Assert
    assert (under, beside) == ("/keys", "/apikeys")


@pytest.mark.usefixtures("readers")
def test_a_path_reader_reads_only_while_its_app_serves_the_request() -> None:
    """An outer app's reader leaves alone the path of an app mounted under it."""
    # Arrange
    read_paths_with(_kind(_Router), _read_upper)
    outer = {"path": "/api/keys", "root_path": "/api", "app": _Router()}
    mounted = {**outer, "app": _Unread()}

    # Act
    served = (route_path(outer), route_path(mounted))

    # Assert
    assert served == ("/API/KEYS", "/keys")


@pytest.mark.usefixtures("readers")
def test_a_subclass_reads_the_path_as_the_app_class_it_extends() -> None:
    """An app built on a class with a path reader reads through that reader."""

    # Arrange
    class Built(_Router):
        pass

    read_paths_with(_kind(_Router), _read_upper)

    # Act
    path = route_path({"path": "/keys", "app": Built()})

    # Assert
    assert path == "/KEYS"


@pytest.mark.usefixtures("readers")
def test_a_reader_given_later_replaces_the_rule_an_app_class_read_by() -> None:
    """An app class already read by the ASGI rule reads through a reader given later."""
    # Arrange
    scope = {"path": "/keys", "app": _Router()}
    before = route_path(scope)

    # Act
    read_paths_with(_kind(_Router), _read_upper)

    # Assert
    assert (before, route_path(scope)) == ("/keys", "/KEYS")


class _Entry:
    """Stand in for an integration's entry point, named after a framework's package."""

    def __init__(self, load: Any) -> None:  # noqa: ANN401
        """Answer for this test module's package, and run `load` when loaded."""
        self.name = __name__.partition(".")[0]
        self.load = load


def _entries(monkeypatch: pytest.MonkeyPatch, *entries: _Entry) -> list[type]:
    """List `entries` as the registered integrations, and return each class looked up."""
    looked: list[type] = []
    found = _discovery._class_entry

    def entry_points(*, group: str) -> tuple[_Entry, ...]:  # noqa: ARG001
        return entries

    def class_entry(kind: type) -> Any:  # noqa: ANN401
        looked.append(kind)
        return found(kind)

    monkeypatch.setattr(_discovery, "entry_points", entry_points)
    monkeypatch.setattr(_discovery, "_class_entry", class_entry)
    return looked


@pytest.mark.usefixtures("readers")
def test_an_app_class_no_integration_names_reads_by_the_asgi_rule_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A class with no integration for its framework is looked up on its first request only."""
    # Arrange
    looked = _entries(monkeypatch)
    scope = {"path": "/api/keys", "root_path": "/api", "app": _Unread()}

    # Act
    paths = (route_path(scope), route_path(scope))

    # Assert
    assert (paths, looked) == (("/keys", "/keys"), [_Unread])


@pytest.mark.usefixtures("readers")
def test_the_first_request_of_an_app_class_imports_its_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The integration of the class's framework gives the reader the request reads by."""
    # Arrange
    _entries(
        monkeypatch,
        _Entry(lambda: read_paths_with(_kind(_Router), _read_upper)),
    )

    # Act
    path = route_path({"path": "/keys", "app": _Router()})

    # Assert
    assert path == "/KEYS"


@pytest.mark.usefixtures("readers")
def test_an_integration_failing_to_import_raises_on_each_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What importing the integration raises propagates, and is raised again on the next request."""

    # Arrange
    def load() -> None:
        msg = "the framework is too old"
        raise ImportError(msg)

    _entries(monkeypatch, _Entry(load))
    scope = {"path": "/keys", "app": _Router()}

    # Act / Assert
    for _ in range(2):
        with pytest.raises(ImportError, match="too old"):
            route_path(scope)


def test_a_litestar_path_reads_by_litestar_before_any_integration_is_imported() -> (
    None
):
    """Middleware added by hand to a Litestar app reads the path as Litestar routes it."""
    # Arrange
    code = (
        "import sys\n"
        "from litestar import Litestar\n"
        "from grelmicro._paths import route_path\n"
        "imported = [m for m in sys.modules if m.startswith('grelmicro.integrations')]\n"
        "app = Litestar()\n"
        "scope = {'path': '/v1/api/x/', 'root_path': '/api', 'app': app}\n"
        "print(imported, route_path(scope))\n"
    )

    # Act
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, check=True, text=True
    )

    # Assert
    assert result.stdout.split() == ["[]", "/x"]


@pytest.mark.usefixtures("readers")
def test_each_key_a_router_writes_is_routed_once() -> None:
    """A key two path readers name is one key the gate keeps for the route."""
    # Act
    read_paths_with(_kind(_Router), _read_upper, writes=("handler",))
    read_paths_with(_kind(_Unread), _read_upper, writes=("handler", "endpoint"))

    # Assert
    assert _paths.ROUTED_KEYS == ("handler", "endpoint")


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
    app = Litestar()
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

    A real app always lists the mounts it owns, so the mount is a
    stand-in.
    """
    app = Litestar()
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
