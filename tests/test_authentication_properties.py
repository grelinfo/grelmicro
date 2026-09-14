"""Differential property tests for authentication.

Hypothesis builds random apps out of everything Starlette and FastAPI route
with: literal and converter segments, trailing slashes, included routers,
mounts, hosts, routers with a default of their own, opaque applications and
websocket routes. Every endpoint answers with its own id, so the framework
itself is the oracle: a request the middleware let through without a
credential says which endpoint served it.

`GRELMICRO_AUTH_EXAMPLES` raises the number of apps tried, for a deeper run
than the default suite affords.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.routing import APIRoute, APIWebSocketRoute
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Host, Mount, Router
from starlette.testclient import TestClient, WebSocketDenialResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from grelmicro import Grelmicro
from grelmicro.http import AuthenticatedRequests, ErrorResponses
from grelmicro.integrations.fastapi import Anonymous
from tests.test_authentication import verifier

pytestmark = [pytest.mark.timeout(600)]

EXAMPLES = int(os.environ.get("GRELMICRO_AUTH_EXAMPLES", "150"))
HTTP_401_UNAUTHORIZED = 401
HTTP_500_INTERNAL_SERVER_ERROR = 500
REQUESTS_PER_APP = 12

HOST = "api.example.com"
HOSTS = ("testserver", HOST)
LITERALS = ("a", "b", "me")
CONVERTERS = (None, "str", "int", "path")
VALUES: dict[str | None, tuple[str, ...]] = {
    None: ("x", "me", "a"),
    "str": ("x", "me"),
    "int": ("7", "x"),
    "path": ("x", "x/y", "me/a", ""),
}
EXTRA_PATHS = ("/", "/a", "/me", "/a/b", "/x/me", "/a/", "//a")
PARAMETER = re.compile(r"\{(\w+)(?::(\w+))?\}")


@dataclass(frozen=True)
class Leaf:
    """A route, or a websocket route, public or not."""

    template: str
    methods: tuple[str, ...]
    public: bool
    websocket: bool


@dataclass(frozen=True)
class Include:
    """A router included under a prefix, declared public or not."""

    prefix: str
    children: tuple[Leaf | Include, ...]
    public: bool


@dataclass(frozen=True)
class Mounted:
    """A mount or a host, over readable routes or an opaque application."""

    path: str
    children: tuple[Leaf | Mounted, ...] | None
    default: bool
    host: str | None


@dataclass(frozen=True)
class AppSpec:
    """A whole app: its routing nodes and its router's own behaviour."""

    nodes: tuple[Leaf | Include | Mounted, ...]
    redirect_slashes: bool
    default: bool


@dataclass
class Built:
    """What an app was built with: which ids are public, and its URLs."""

    public: set[int] = field(default_factory=set)
    urls: list[tuple[str, str | None, bool]] = field(default_factory=list)
    count: int = 0

    def endpoint(self, *, public: bool) -> int:
        """Return a new endpoint id, remembered as public or not."""
        self.count += 1
        if public:
            self.public.add(self.count)
        return self.count


def templates(
    name: str, *, min_size: int, max_size: int, trailing: bool
) -> st.SearchStrategy[str]:
    """Return route paths made of literal and converter segments."""

    @st.composite
    def build(draw: st.DrawFn) -> str:
        count = draw(st.integers(min_size, max_size))
        parts: list[str] = []
        for index in range(count):
            if draw(st.booleans()):
                parts.append(draw(st.sampled_from(LITERALS)))
                continue
            converter = draw(st.sampled_from(CONVERTERS))
            parameter = f"{name}{index}"
            parts.append(
                f"{{{parameter}:{converter}}}"
                if converter
                else f"{{{parameter}}}"
            )
        path = "/" + "/".join(parts)
        if trailing and count and draw(st.booleans()):
            path += "/"
        return path

    return build()


LEAVES = st.builds(
    Leaf,
    template=templates("p", min_size=0, max_size=3, trailing=True),
    methods=st.lists(
        st.sampled_from(("GET", "POST")), min_size=1, max_size=2, unique=True
    ).map(tuple),
    public=st.booleans(),
    websocket=st.booleans(),
)


def includes(depth: int = 0) -> st.SearchStrategy[Include]:
    """Return included routers, nested at most two deep."""
    children = LEAVES if depth >= 1 else st.one_of(LEAVES, includes(depth + 1))
    return st.builds(
        Include,
        prefix=templates(f"i{depth}_", min_size=1, max_size=2, trailing=False),
        children=st.lists(children, min_size=1, max_size=3).map(tuple),
        public=st.booleans(),
    )


def mounts(depth: int = 0) -> st.SearchStrategy[Mounted]:
    """Return mounts and hosts, nested at most two deep."""
    inner = LEAVES if depth >= 1 else st.one_of(LEAVES, mounts(depth + 1))
    return st.builds(
        Mounted,
        path=templates("p", min_size=0, max_size=2, trailing=False),
        children=st.one_of(
            st.none(), st.lists(inner, min_size=1, max_size=3).map(tuple)
        ),
        default=st.booleans(),
        host=st.one_of(st.none(), st.just(HOST)),
    )


APPS = st.builds(
    AppSpec,
    nodes=st.lists(
        st.one_of(LEAVES, includes(), mounts()), min_size=1, max_size=5
    ).map(tuple),
    redirect_slashes=st.booleans(),
    default=st.booleans(),
)


def http_endpoint(ident: int) -> Any:  # noqa: ANN401
    """Return an endpoint answering with its id."""

    async def endpoint(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse({"served": ident})

    return endpoint


def websocket_endpoint(ident: int) -> Any:  # noqa: ANN401
    """Return a websocket endpoint sending its id."""

    async def endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"served": ident})
        await websocket.close()

    return endpoint


def opaque(ident: int) -> Any:  # noqa: ANN401
    """Return an application answering anything with its id."""

    async def application(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        if scope["type"] == "http":
            await JSONResponse({"served": ident})(scope, receive, send)
            return
        await websocket_endpoint(ident)(WebSocket(scope, receive, send))

    return application


def route_of(
    leaf: Leaf, built: Built, prefix: str, host: str | None
) -> BaseRoute:
    """Return a FastAPI route object for a leaf, remembering its URL."""
    ident = built.endpoint(public=leaf.public)
    built.urls.append((prefix + leaf.template, host, leaf.websocket))
    dependencies = [Anonymous()] if leaf.public else []
    if leaf.websocket:
        return APIWebSocketRoute(
            leaf.template, websocket_endpoint(ident), dependencies=dependencies
        )
    return APIRoute(
        leaf.template,
        http_endpoint(ident),
        methods=list(leaf.methods),
        dependencies=dependencies,
    )


def apply(
    target: FastAPI | APIRouter,
    nodes: tuple[Leaf | Include | Mounted, ...],
    built: Built,
    *,
    prefix: str,
    public_above: bool,
) -> None:
    """Declare nodes on an app or a router, the way an app would."""
    for node in nodes:
        if isinstance(node, Leaf):
            ident = built.endpoint(public=node.public or public_above)
            built.urls.append((prefix + node.template, None, node.websocket))
            dependencies = [Anonymous()] if node.public else []
            if node.websocket:
                target.add_api_websocket_route(
                    node.template,
                    websocket_endpoint(ident),
                    dependencies=dependencies,
                )
            else:
                target.add_api_route(
                    node.template,
                    http_endpoint(ident),
                    methods=list(node.methods),
                    dependencies=dependencies,
                )
        elif isinstance(node, Include):
            router = APIRouter()
            apply(
                router,
                node.children,
                built,
                prefix=prefix + node.prefix,
                public_above=public_above or node.public,
            )
            target.include_router(
                router,
                prefix=node.prefix,
                dependencies=[Anonymous()] if node.public else [],
            )
        else:
            target.routes.append(mounted(node, built, prefix, None))


def mounted(
    node: Mounted, built: Built, prefix: str, host: str | None
) -> BaseRoute:
    """Return a mount or a host over its routes or an opaque application."""
    under = prefix if node.host else prefix + node.path.rstrip("/")
    reached = node.host or host
    if node.children is None:
        application: Any = opaque(built.endpoint(public=False))
        built.urls.append((under + "/{rest:path}", reached, False))
    else:
        router = Router(
            default=opaque(built.endpoint(public=False))
            if node.default
            else None
        )
        for child in node.children:
            router.routes.append(
                route_of(child, built, under, reached)
                if isinstance(child, Leaf)
                else mounted(child, built, under, reached)
            )
        application = router
    if node.host:
        return Host(node.host, app=application)
    return Mount(node.path.rstrip("/"), app=application)


def build(spec: AppSpec) -> tuple[FastAPI, Built]:
    """Build and install an app from its spec."""
    built = Built()
    app = FastAPI(redirect_slashes=spec.redirect_slashes)
    apply(app, spec.nodes, built, prefix="", public_above=False)
    if spec.default:
        app.router.default = opaque(built.endpoint(public=False))
    Grelmicro(
        uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
    ).install(app)
    return app, built


def filled(data: st.DataObject, template: str) -> str:
    """Return a URL of a template, with values drawn for its parameters."""
    return PARAMETER.sub(
        lambda match: data.draw(st.sampled_from(VALUES[match.group(2)])),
        template,
    )


def request_of(data: st.DataObject, built: Built) -> tuple[str, str, bool]:
    """Draw a URL, a host and whether it is a websocket handshake."""
    if built.urls and data.draw(st.integers(0, 3)):
        template, host, websocket = data.draw(st.sampled_from(built.urls))
        path = filled(data, template) or "/"
        if path != "/" and data.draw(st.booleans()):
            path = path.rstrip("/") if path.endswith("/") else f"{path}/"
    else:
        path = data.draw(st.sampled_from(EXTRA_PATHS))
        host, websocket = None, data.draw(st.booleans())
    if data.draw(st.integers(0, 5)) == 0:
        path = "/" + path
    if data.draw(st.integers(0, 7)) == 0:
        # A URL decoding to a trailing newline, which Starlette's `$` accepts.
        path += "%0A"
    return path, host or data.draw(st.sampled_from(HOSTS)), websocket


def served_by(response: Any) -> int | None:  # noqa: ANN401
    """Return the endpoint id a response came from, if it came from one."""
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("served") if isinstance(body, dict) else None


@given(spec=APPS, data=st.data())
@settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_a_request_without_a_credential_reaches_only_a_public_endpoint(
    spec: AppSpec, data: st.DataObject
) -> None:
    """Whatever the app routes with, the middleware opens only what was declared."""
    app, built = build(spec)
    client = TestClient(app, raise_server_exceptions=False)
    for _ in range(REQUESTS_PER_APP):
        path, host, websocket = request_of(data, built)
        if websocket:
            try:
                with client.websocket_connect(f"ws://{host}{path}") as socket:
                    served = served_by_message(socket.receive_json())
            except (WebSocketDenialResponse, WebSocketDisconnect):
                continue
            assert served is None or served in built.public, (
                "websocket",
                host,
                path,
                served,
            )
            continue
        method = data.draw(st.sampled_from(("GET", "POST")))
        response = client.request(
            method, f"http://{host}{path}", follow_redirects=False
        )
        assert response.status_code != HTTP_500_INTERNAL_SERVER_ERROR, (
            method,
            host,
            path,
            response.text,
        )
        if response.status_code == HTTP_401_UNAUTHORIZED:
            continue
        served = served_by(response)
        assert served is None or served in built.public, (
            method,
            host,
            path,
            response.status_code,
            served,
        )


def served_by_message(message: Any) -> int | None:  # noqa: ANN401
    """Return the endpoint id a websocket message came from."""
    return message.get("served") if isinstance(message, dict) else None
