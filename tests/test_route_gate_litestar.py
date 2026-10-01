"""Authentication decided per handler on Litestar, once its router matched it.

Litestar's router matches the handler, by path and by method, and the gate
of that handler admits or refuses the request. A request without a
credential is routed when a handler declares `Anonymous()`, and whatever
answers it before a gate did is answered `401`. The answering middleware
run around the handler the gate admitted.
"""

from __future__ import annotations

import json
import logging
import warnings
from typing import TYPE_CHECKING, Annotated, Any, cast

import pytest
from litestar import Litestar, asgi, get, post, websocket
from litestar import WebSocket as LitestarWebSocket
from litestar.background_tasks import BackgroundTask
from litestar.config.cors import CORSConfig
from litestar.exceptions import WebSocketDisconnect
from litestar.middleware import DefineMiddleware
from litestar.params import Parameter
from litestar.response import Stream
from litestar.testing import AsyncTestClient, TestClient
from starlette.responses import PlainTextResponse

from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.errors import MiddlewarePlacementWarning
from grelmicro.http import (
    AuthenticatedRequests,
    CachedResponses,
    ErrorResponses,
    IdempotentRequests,
    RateLimitedRequests,
    RateLimitMiddleware,
    RouteDeclaration,
)
from grelmicro.http._authentication import _PublicRoutes
from grelmicro.integrations.litestar import (
    Anonymous,
    Authenticated,
    route_declarations,
)
from grelmicro.resilience import RateLimiter
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter
from tests.test_authentication import bearer, token, verifier
from tests.test_authentication_cases import unasked

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from starlette.types import ASGIApp, Receive, Scope, Send

pytestmark = [pytest.mark.timeout(10)]

OK = 200
CREATED = 201
NO_CONTENT = 204
UNAUTHORIZED = 401
FORBIDDEN = 403
NOT_FOUND = 404
METHOD_NOT_ALLOWED = 405
TOO_MANY_REQUESTS = 429
SERVER_ERROR = 500
CLOSED = 1008
EVENTS = "grelmicro.security.events"
ORIGIN = "https://app.example"
ADDRESS = ("203.0.113.7", 5000)
"""The address every caller shares, as behind one NAT."""


@pytest.fixture
def events(
    caplog: pytest.LogCaptureFixture,
) -> Iterator[list[logging.LogRecord]]:
    """Return the security records written while the test runs."""
    caplog.set_level(logging.DEBUG, logger=EVENTS)
    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Keep()
    logger = logging.getLogger(EVENTS)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


class Calls:
    """Counts the requests each handler served."""

    def __init__(self) -> None:
        """Start at zero."""
        self.served: list[str] = []


class Passing:
    """Middleware of the app's own that passes every request on."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap `app`."""
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Pass the request on."""
        await self.app(scope, receive, send)


def installed(app: Litestar, *uses: Any, **options: Any) -> Litestar:  # noqa: ANN401
    """Install authentication and `uses` on `app`, and return it."""
    Grelmicro(
        uses=[
            ErrorResponses(),
            AuthenticatedRequests(verifier(), **options),
            *uses,
        ]
    ).install(app)
    return app


def catalog(calls: Calls, *uses: Any, **options: Any) -> Litestar:  # noqa: ANN401
    """Return an app with a public read beside protected and scoped writes."""

    @get("/items/{item_id:int}", opt=Anonymous())
    async def read(item_id: Annotated[int, Parameter()]) -> dict[str, int]:
        calls.served.append("read")
        return {"item": item_id}

    @post("/items/{item_id:int}", status_code=CREATED)
    async def write(item_id: Annotated[int, Parameter()]) -> dict[str, int]:
        calls.served.append("write")
        return {"item": item_id}

    @post(
        "/orders/{order_id:int}",
        guards=[Authenticated(scopes=["orders:write"])],
        status_code=CREATED,
    )
    async def order(order_id: Annotated[int, Parameter()]) -> dict[str, int]:
        calls.served.append("order")
        return {"order": order_id}

    return installed(Litestar([read, write, order]), *uses, **options)


def limited(limit: int = 3) -> RateLimitedRequests:
    """Return a rate limit of `limit` requests a minute for every caller at one address."""
    return RateLimitedRequests(
        RateLimiter.sliding_window(
            "burst", limit=limit, window=60, backend=MemoryRateLimiterAdapter()
        ),
        key=lambda scope: ADDRESS[0],  # noqa: ARG005
    )


def flooded(flood: int = 3, burst: int = 100) -> RateLimitedRequests:
    """Return a route limit beside a flood limit, both keyed by one address."""
    return RateLimitedRequests(
        RateLimiter.sliding_window(
            "burst", limit=burst, window=60, backend=MemoryRateLimiterAdapter()
        ),
        flood=RateLimiter.sliding_window(
            "flood", limit=flood, window=60, backend=MemoryRateLimiterAdapter()
        ),
        key=lambda scope: ADDRESS[0],  # noqa: ARG005
    )


def without_instance(content: bytes) -> dict[str, Any]:
    """Return a problem body without the path it names."""
    body = json.loads(content)
    body.pop("instance", None)
    return body


class TestExclude:
    """A path in `exclude` is matched from the app's root, as before routing."""

    def test_it_is_served_under_a_root_path(self) -> None:
        """A route whose path starts like the root path included."""

        @get("/api/orders")
        async def orders() -> str:
            return "orders"

        @get("/orders")
        async def other() -> str:
            return "other"  # pragma: no cover

        app = installed(
            Litestar([orders, other, *catalog_handlers()]),
            exclude=("/api/orders",),
        )
        with TestClient(app, root_path="/api") as client:
            served = client.get("/api/api/orders")
            refused = client.get("/api/orders")

        assert served.text == "orders"
        assert refused.status_code == UNAUTHORIZED


class TestPerMethod:
    """Each method of a route is decided by the handler that answers it."""

    def test_an_anonymous_read_keeps_the_writes_authenticated(self) -> None:
        """And the `OPTIONS` Litestar adds, which declares nothing."""
        calls = Calls()
        with TestClient(catalog(calls)) as client:
            read = client.get("/items/7")
            write = client.post("/items/7")
            options = client.options("/items/7")
            written = client.post("/items/7", headers=bearer(token()))

        assert read.json() == {"item": 7}
        assert write.status_code == options.status_code == UNAUTHORIZED
        assert written.status_code == CREATED
        assert calls.served == ["read", "write"]

    def test_a_guard_scope_is_refused_before_the_handler(self) -> None:
        """`403` naming the scope, and `401` without a credential."""
        calls = Calls()
        with TestClient(catalog(calls)) as client:
            missing = client.post("/orders/7", headers=bearer(token()))
            refused = client.post("/orders/7")
            granted = client.post(
                "/orders/7", headers=bearer(token(scope="orders:write"))
            )

        assert missing.status_code == FORBIDDEN
        assert missing.headers["www-authenticate"] == (
            'Bearer error="insufficient_scope", scope="orders:write"'
        )
        assert refused.status_code == UNAUTHORIZED
        assert refused.headers["www-authenticate"] == "Bearer"
        assert granted.status_code == CREATED
        assert calls.served == ["order"]

    def test_each_handler_is_declared_per_method(self) -> None:
        """What the gates read, listed for the app to check at startup."""
        declared = route_declarations(catalog(Calls()))

        assert {
            declaration
            for declaration in declared
            if not declaration.path.startswith("/schema")
        } == {
            RouteDeclaration(
                "/items/{item_id}", methods=frozenset({"GET"}), anonymous=True
            ),
            RouteDeclaration("/items/{item_id}", methods=frozenset({"POST"})),
            RouteDeclaration(
                "/items/{item_id}", methods=frozenset({"OPTIONS"})
            ),
            RouteDeclaration(
                "/orders/{order_id}",
                methods=frozenset({"POST"}),
                scopes=frozenset({"orders:write"}),
            ),
            RouteDeclaration(
                "/orders/{order_id}", methods=frozenset({"OPTIONS"})
            ),
        }


class TestRefusals:
    """What a request no handler admits is answered and recorded with."""

    def test_a_refusal_is_recorded_with_the_route_template(
        self, events: list[logging.LogRecord]
    ) -> None:
        """Under the root path the request arrived at, as before routing."""
        with TestClient(catalog(Calls()), root_path="/api") as client:
            client.post("/api/items/7")
            client.post("/api/orders/7", headers=bearer(token()))
        with TestClient(catalog(Calls()), root_path="/api") as client:
            client.get("/api/nowhere")

        assert [
            (record.__dict__["error.type"], record.__dict__.get("http.route"))
            for record in events
        ] == [
            ("authentication-required", "/api/items/{item_id}"),
            ("insufficient-scope", "/api/orders/{order_id}"),
            ("authentication-required", None),
        ]

    def test_an_app_with_no_public_handler_refuses_before_routing(
        self, events: list[logging.LogRecord]
    ) -> None:
        """Nothing is routed without a credential, and no route is named."""
        calls = Calls()

        @get("/private")
        async def private() -> str:
            calls.served.append("private")  # pragma: no cover
            return "private"  # pragma: no cover

        with TestClient(installed(Litestar([private]))) as client:
            refused = client.get("/nowhere")

        assert refused.status_code == UNAUTHORIZED
        assert [record.__dict__.get("http.route") for record in events] == [
            None
        ]

    @pytest.mark.parametrize(
        ("method", "path", "served_with_a_token"),
        [
            ("GET", "/nowhere", NOT_FOUND),
            ("PUT", "/items/7", METHOD_NOT_ALLOWED),
        ],
    )
    def test_no_handler_answers_with_the_401_a_protected_route_answers(
        self, method: str, path: str, served_with_a_token: int
    ) -> None:
        """Same status, headers and body, so route existence does not leak."""
        with TestClient(catalog(Calls())) as client:
            protected = client.post("/items/7")
            refused = client.request(method, path)
            with_token = client.request(method, path, headers=bearer(token()))

        assert refused.status_code == protected.status_code == UNAUTHORIZED
        assert without_instance(refused.content) == without_instance(
            protected.content
        )
        assert json.loads(refused.content)["instance"] == path
        assert {
            name: value
            for name, value in refused.headers.items()
            if name != "content-length"
        } == {
            name: value
            for name, value in protected.headers.items()
            if name != "content-length"
        }
        assert with_token.status_code == served_with_a_token

    def test_a_websocket_no_handler_answers_is_closed(self) -> None:
        """As a protected websocket handler closes it."""

        @websocket("/ws")
        async def chat(socket: LitestarWebSocket[Any, Any, Any]) -> None:
            await socket.accept()  # pragma: no cover
            await socket.close()  # pragma: no cover

        app = installed(Litestar([chat, *catalog_handlers()]))

        with TestClient(app) as client:
            closed = [
                pytest.raises(WebSocketDisconnect) for _ in ("/nowhere", "/ws")
            ]
            for path, raises in zip(("/nowhere", "/ws"), closed, strict=True):
                with raises as refused, client.websocket_connect(path):
                    pass  # pragma: no cover
                assert refused.value.code == CLOSED


def catalog_handlers() -> list[Any]:
    """Return a public handler, so the app routes requests without a credential."""

    @get("/public", opt=Anonymous())
    async def public() -> str:
        return "public"  # pragma: no cover

    return [public]


class CountingCache(MemoryCacheAdapter):
    """A memory cache counting every call to it."""

    calls = 0

    def __getattribute__(self, name: str) -> Any:  # noqa: ANN401
        """Count a public method looked up."""
        found = super().__getattribute__(name)
        if callable(found) and not name.startswith("_"):
            type(self).calls += 1
        return found


def tenant_key(scope: Scope, key: str) -> str:
    """Key an idempotent request by its caller, refusing one with none."""
    user = scope.get("user")
    if user is None or not user.is_authenticated:
        msg = "idempotency needs an authenticated caller"
        raise PermissionError(msg)
    return json.dumps(["tenant-v1", str(user.identity), key])


class TestNothingIsSpentOnARefusal:
    """A request a handler refuses reaches none of the answering middleware."""

    @pytest.mark.usefixtures("clock")
    async def test_the_rate_limit_of_an_address_is_not_spent(self) -> None:
        """Callers behind one address keep their budget."""
        calls = Calls()
        app = catalog(calls, limited())
        async with AsyncTestClient(app) as client:
            refused = [
                (await client.post("/items/7")).status_code for _ in range(5)
            ]
            unrouted = [
                (await client.get("/nowhere")).status_code for _ in range(5)
            ]
            answered = await client.get("/items/7")

        assert refused == unrouted == [UNAUTHORIZED] * 5
        assert answered.status_code == OK
        assert answered.headers["ratelimit"] == '"burst";r=2;t=20'

    def test_the_cache_is_never_asked(self) -> None:
        """No backend call for a refused or an unrouted request."""
        backend = CountingCache()
        app = catalog(
            Calls(), Cache(backend), CachedResponses(include={"/items/*": 60})
        )
        with TestClient(app) as client:
            backend.calls = 0
            statuses = [
                client.post("/items/7").status_code,
                client.get("/nowhere").status_code,
                client.options("/items/7").status_code,
            ]

        assert statuses == [UNAUTHORIZED] * 3
        assert backend.calls == 0

    @pytest.mark.parametrize("key_maker", [None, tenant_key])
    def test_an_idempotent_write_stores_nothing(self, key_maker: Any) -> None:  # noqa: ANN401
        """Its key stays free for the retry with a token, which runs."""
        calls = Calls()
        backend = CountingCache()
        idempotent = (
            IdempotentRequests(key_maker=key_maker)
            if key_maker
            else IdempotentRequests()
        )
        app = catalog(calls, Cache(backend), idempotent)
        with TestClient(app) as client:
            backend.calls = 0
            refused = client.post(
                "/items/7", headers={"Idempotency-Key": "k-1"}
            )
            stored = backend.calls
            retried = client.post(
                "/items/7",
                headers={"Idempotency-Key": "k-1", **bearer(token())},
            )

        assert refused.status_code == UNAUTHORIZED
        assert stored == 0
        assert retried.status_code == CREATED
        assert calls.served == ["write"]

    def test_a_mounted_app_never_runs_for_a_refused_request(self) -> None:
        """Neither its middleware nor whichever app it dispatches to."""
        seen: list[str] = []

        class Audit:
            def __init__(self, app: ASGIApp) -> None:
                self.app = app

            async def __call__(
                self, scope: Scope, receive: Receive, send: Send
            ) -> None:
                seen.append(scope["path"])
                await self.app(scope, receive, send)

        async def answer(scope: Scope, receive: Receive, send: Send) -> None:
            await PlainTextResponse("admin")(scope, receive, send)

        admin = Audit(answer)

        async def dispatch(scope: Scope, receive: Receive, send: Send) -> None:
            target = admin if (b"x-v", b"2") in scope["headers"] else answer
            await target(scope, receive, send)

        app = installed(
            Litestar(
                [
                    *catalog_handlers(),
                    asgi("/admin", is_mount=True, copy_scope=False)(admin),
                    asgi("/v", is_mount=True, copy_scope=False)(dispatch),
                ]
            )
        )
        with TestClient(app) as client:
            refused = [
                client.post("/admin/users").status_code,
                client.get("/v/users", headers={"x-v": "2"}).status_code,
                client.get("/v/users").status_code,
            ]
            answered = client.get("/admin/users", headers=bearer(token()))

        assert refused == [UNAUTHORIZED] * 3
        assert answered.text == "admin"
        assert seen == ["/users/"]


class TestAnAnonymousHandler:
    """A request without a credential it admits runs the answering middleware."""

    def test_it_is_rate_limited(self) -> None:
        """By address, since nobody is authenticated."""
        app = catalog(Calls(), limited())
        with TestClient(app) as client:
            statuses = [client.get("/items/7").status_code for _ in range(4)]

        assert statuses == [OK, OK, OK, TOO_MANY_REQUESTS]

    def test_its_refusal_names_the_path_the_request_arrived_at(self) -> None:
        """Under a root path, as before routing."""
        app = catalog(Calls(), limited(limit=1))
        with TestClient(app, root_path="/svc") as client:
            client.get("/svc/items/7")
            refused = client.get("/svc/items/7")

        assert refused.status_code == TOO_MANY_REQUESTS
        assert refused.json()["instance"] == "/svc/items/7"

    def test_a_read_is_cached_on_a_miss_and_served_on_a_hit(self) -> None:
        """The handler runs once."""
        calls = Calls()
        app = catalog(
            calls,
            Cache(MemoryCacheAdapter()),
            CachedResponses(include={"/items/*": 60}),
        )
        with TestClient(app) as client:
            bodies = [client.get("/items/7").json() for _ in range(2)]

        assert bodies == [{"item": 7}] * 2
        assert calls.served == ["read"]

    def test_a_write_retried_with_its_key_is_replayed(self) -> None:
        """The handler runs once, and the retry says it was replayed."""
        calls = Calls()

        @post("/signups", opt=Anonymous(), status_code=CREATED)
        async def signup() -> dict[str, bool]:
            calls.served.append("signup")
            return {"signed": True}

        app = installed(
            Litestar([signup]),
            Cache(MemoryCacheAdapter()),
            IdempotentRequests(),
        )
        with TestClient(app) as client:
            first, retried = (
                client.post("/signups", headers={"Idempotency-Key": "k-1"})
                for _ in range(2)
            )

            assert first.status_code == retried.status_code == CREATED
            assert retried.headers["idempotent-replayed"] == "true"
        assert calls.served == ["signup"]

    @pytest.mark.parametrize("middleware", [[], [Passing]])
    def test_a_write_that_raises_stores_nothing(
        self, middleware: list[Any]
    ) -> None:
        """With or without app middleware, the retry runs the handler again."""
        calls = Calls()

        @post("/signups", opt=Anonymous(), status_code=CREATED)
        async def signup() -> dict[str, bool]:
            calls.served.append("signup")
            msg = "kaboom"
            raise RuntimeError(msg)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", MiddlewarePlacementWarning)
            app = installed(
                Litestar([signup], middleware=middleware),
                Cache(MemoryCacheAdapter()),
                IdempotentRequests(),
            )
        with TestClient(app, raise_server_exceptions=False) as client:
            first, retried = (
                client.post("/signups", headers={"Idempotency-Key": "k-1"})
                for _ in range(2)
            )

            assert first.status_code == retried.status_code == SERVER_ERROR
            assert "idempotent-replayed" not in retried.headers
        assert calls.served == ["signup", "signup"]

    @pytest.mark.usefixtures("clock")
    async def test_a_streamed_body_and_a_background_task_run(self) -> None:
        """Through the rate limit, which runs around them."""
        ran: list[str] = []

        async def chunks() -> AsyncIterator[bytes]:
            yield b"a"
            yield b"b"

        @get("/stream", opt=Anonymous())
        async def stream() -> Stream:
            return Stream(
                chunks(), background=BackgroundTask(ran.append, "after")
            )

        app = installed(Litestar([stream]), limited())
        async with AsyncTestClient(app) as client:
            response = await client.get("/stream")

        assert response.text == "ab"
        assert response.headers["ratelimit"] == '"burst";r=2;t=20'
        assert ran == ["after"]

    @pytest.mark.usefixtures("clock")
    async def test_the_app_middleware_run_inside_them(self) -> None:
        """Litestar's own stack for the handler, once per request."""
        seen: list[str | None] = []

        class Reading(Passing):
            async def __call__(
                self, scope: Scope, receive: Receive, send: Send
            ) -> None:
                seen.append(scope.get("route_handler").name)  # type: ignore[union-attr]  # ty: ignore[unresolved-attribute]
                await self.app(scope, receive, send)

        @get("/public", opt=Anonymous(), name="public")
        async def public() -> str:
            return "public"

        with pytest.warns(MiddlewarePlacementWarning):
            app = installed(
                Litestar(
                    [public],
                    middleware=[DefineMiddleware(cast("Any", Reading))],
                ),
                limited(),
            )
        async with AsyncTestClient(app) as client:
            response = await client.get("/public")

        assert response.headers["ratelimit"] == '"burst";r=2;t=20'
        assert seen == ["public"]

    def test_a_websocket_is_accepted(self) -> None:
        """Its handshake reaches the handler."""

        @websocket("/ws", opt=Anonymous())
        async def chat(socket: LitestarWebSocket[Any, Any, Any]) -> None:
            await socket.accept()
            await socket.send_text("hello")
            await socket.close()

        app = installed(Litestar([chat]), limited())
        with TestClient(app) as client, client.websocket_connect("/ws") as ws:
            assert ws.receive_text() == "hello"


class TestRegisteredLater:
    """A handler registered after install is gated as it lands."""

    @pytest.mark.parametrize("started", [False, True])
    def test_it_is_refused_without_a_credential(self, *, started: bool) -> None:
        """Before the app starts, which then starts, and once it serves."""
        calls = Calls()

        @get("/admin")
        async def admin() -> str:
            calls.served.append("admin")
            return "admin"

        @get("/open", opt=Anonymous())
        async def opened() -> str:
            return "open"

        app = installed(Litestar([admin]))
        if not started:
            app.register(opened)
        with TestClient(app) as client:
            if started:
                app.register(opened)
            refused = client.get("/admin")
            answered = client.get("/admin", headers=bearer(token()))
            public = client.get("/open")

        assert refused.status_code == UNAUTHORIZED
        assert answered.text == "admin"
        assert public.text == "open"
        assert calls.served == ["admin"]


class TestFloodLimit:
    """The flood limit runs before routing, with a budget of its own."""

    def test_a_flood_no_route_answers_is_refused(self) -> None:
        """With a token, once the flood budget is spent."""
        app = catalog(Calls(), flooded())
        with TestClient(app) as client:
            statuses = [
                client.get("/nowhere", headers=bearer(token())).status_code
                for _ in range(4)
            ]

        assert statuses == [NOT_FOUND] * 3 + [TOO_MANY_REQUESTS]

    def test_a_flood_without_a_token_is_refused(self) -> None:
        """An app with a public route routes it, so it spends the flood budget."""
        app = catalog(Calls(), flooded())
        with TestClient(app) as client:
            statuses = [client.get("/nowhere").status_code for _ in range(4)]

        assert statuses == [UNAUTHORIZED] * 3 + [TOO_MANY_REQUESTS]

    def test_a_request_a_route_refuses_spends_the_flood_budget_only(
        self,
    ) -> None:
        """The route's bucket is left whole, the flood one is not."""
        calls = Calls()
        app = catalog(calls, flooded(flood=4, burst=2))
        with TestClient(app) as client:
            refused = [
                client.post("/orders/1", headers=bearer(token())).status_code
                for _ in range(3)
            ]
            answered = client.post("/items/7", headers=bearer(token()))
            turned_away = client.post("/items/7", headers=bearer(token()))

        assert refused == [FORBIDDEN] * 3
        assert answered.status_code == CREATED
        assert answered.headers["ratelimit"].startswith('"burst";r=1;')
        assert turned_away.status_code == TOO_MANY_REQUESTS
        assert calls.served == ["write"]

    def test_each_answer_states_the_budget_that_metered_it(self) -> None:
        """A served request states the route's, a flood refusal the flood one."""
        app = catalog(Calls(), flooded(flood=1))
        with TestClient(app) as client:
            answered = client.get("/items/7")
            turned_away = client.get("/items/7")

        assert answered.headers["ratelimit-policy"] == '"burst";q=100;w=60'
        assert turned_away.status_code == TOO_MANY_REQUESTS
        assert turned_away.headers["ratelimit-policy"] == '"flood";q=1;w=60'


class TestAuthenticationInTheApp:
    """Authentication passed to `Litestar(middleware=[...])` runs behind the router."""

    def test_it_reads_the_declaration_of_the_handler_it_serves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Public, protected and scoped handlers answer as they do in front."""
        monkeypatch.setattr(_PublicRoutes, "matches", unasked)
        calls = Calls()
        component = AuthenticatedRequests(verifier())
        middleware, options = component.asgi_middleware()

        @get("/public", opt=Anonymous())
        async def public() -> str:
            calls.served.append("public")
            return "public"

        @get("/private")
        async def private() -> str:
            calls.served.append("private")
            return "private"

        @post("/orders", guards=[Authenticated(scopes=["orders:write"])])
        async def order() -> str:
            calls.served.append("order")  # pragma: no cover
            return "order"  # pragma: no cover

        app = Litestar(
            [public, private, order],
            middleware=[
                DefineMiddleware(cast("Any", Passing)),
                DefineMiddleware(middleware, **options),
            ],
        )
        Grelmicro(uses=[ErrorResponses(), component]).install(app)
        with TestClient(app) as client:
            statuses = [
                client.get("/public").status_code,
                client.get("/private").status_code,
                client.get("/private", headers=bearer(token())).status_code,
                client.post("/orders", headers=bearer(token())).status_code,
                client.post("/orders").status_code,
            ]

        assert statuses == [OK, UNAUTHORIZED, OK, FORBIDDEN, UNAUTHORIZED]
        assert calls.served == ["public", "private"]

    @pytest.mark.usefixtures("clock")
    async def test_the_answering_middleware_run_once_it_admitted_the_request(
        self,
    ) -> None:
        """A refused request spends nothing, an admitted one is limited."""
        component = AuthenticatedRequests(verifier())
        middleware, options = component.asgi_middleware()

        @get("/public", opt=Anonymous())
        async def public() -> str:
            return "public"

        @get("/private")
        async def private() -> str:
            return "private"

        app = Litestar(
            [public, private],
            middleware=[DefineMiddleware(middleware, **options)],
        )
        Grelmicro(uses=[ErrorResponses(), component, limited()]).install(app)
        async with AsyncTestClient(app) as client:
            refused = [
                (await client.get("/private")).status_code for _ in range(3)
            ]
            answered = await client.get("/private", headers=bearer(token()))
            public_read = await client.get("/public")

        assert refused == [UNAUTHORIZED] * 3
        assert answered.headers["ratelimit"] == '"burst";r=2;t=20'
        assert public_read.headers["ratelimit"] == '"burst";r=1;t=40'

    def test_a_flood_limit_passed_to_the_app_fails_install(self) -> None:
        """It runs behind the router, where no unrouted request reaches it."""

        @get("/orders")
        async def orders() -> str:
            return "orders"  # pragma: no cover

        flood = RateLimiter.sliding_window("flood", limit=600, window=60)
        app = Litestar(
            [orders],
            middleware=[
                DefineMiddleware(
                    cast("Any", RateLimitMiddleware),
                    limiters=[
                        RateLimiter.sliding_window(
                            "burst", limit=100, window=60
                        )
                    ],
                    flood=flood,
                    key=lambda scope: ADDRESS[0],  # noqa: ARG005
                )
            ],
        )

        with pytest.raises(TypeError, match="flood="):
            Grelmicro(uses=[ErrorResponses()]).install(app)

    def test_a_flood_option_of_another_middleware_installs(self) -> None:
        """Only a grelmicro rate limit is a flood limit."""

        class Shield(Passing):
            def __init__(self, app: ASGIApp, *, flood: bool) -> None:
                super().__init__(app)
                self.flood = flood

        @get("/orders")
        async def orders() -> str:
            return "orders"

        app = Litestar(
            [orders],
            middleware=[DefineMiddleware(cast("Any", Shield), flood=True)],
        )
        Grelmicro(uses=[ErrorResponses()]).install(app)

        with TestClient(app) as client:
            assert client.get("/orders").status_code == OK

    def test_a_flood_limit_fails_install(self) -> None:
        """No point before routing has authenticated the request."""
        component = AuthenticatedRequests(verifier())
        middleware, options = component.asgi_middleware()

        @get("/private")
        async def private() -> str:
            return "private"  # pragma: no cover

        app = Litestar(
            [private], middleware=[DefineMiddleware(middleware, **options)]
        )

        with pytest.raises(TypeError, match="flood="):
            Grelmicro(uses=[ErrorResponses(), component, flooded()]).install(
                app
            )


def test_a_litestar_router_without_the_seam_fails_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Litestar release that moved `handle_routing` stops `install` loudly."""
    app = Litestar(catalog_handlers())
    monkeypatch.delattr(type(app.asgi_router), "handle_routing")

    with pytest.raises(RuntimeError, match="has no handle_routing"):
        installed(app)


def test_a_cors_preflight_is_answered_before_authentication() -> None:
    """Litestar's CORS middleware answers it, whatever the URL."""

    @get("/private")
    async def private() -> str:
        return "private"  # pragma: no cover

    app = installed(
        Litestar(
            [private, *catalog_handlers()],
            cors_config=CORSConfig(allow_origins=[ORIGIN]),
        )
    )
    preflight = {"origin": ORIGIN, "access-control-request-method": "GET"}
    with TestClient(app) as client:
        answered = [
            client.options(path, headers=preflight)
            for path in ("/private", "/public", "/nowhere")
        ]

    assert [response.status_code for response in answered] == [NO_CONTENT] * 3
    assert {
        response.headers["access-control-allow-origin"] for response in answered
    } == {ORIGIN}


def test_an_installed_app_under_an_anonymous_mount_hides_its_routes() -> None:
    """Its own unmatched URL and wrong method answer the `401` a protected route does."""

    @get("/pub", opt=Anonymous())
    async def public() -> str:
        return "public"

    @get("/prot")
    async def private() -> str:
        return "private"

    inner = Litestar([public, private])
    Grelmicro(
        uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
    ).install(inner)

    inner_app = cast("ASGIApp", inner)

    async def forward(scope: Scope, receive: Receive, send: Send) -> None:
        await inner_app(scope, receive, send)

    mount = asgi("/in", is_mount=True, copy_scope=False, opt=Anonymous())
    outer = Litestar([mount(forward)])
    Grelmicro(
        uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
    ).install(outer)
    with TestClient(outer) as client:
        statuses = [
            client.get("/in/nope").status_code,
            client.post("/in/prot").status_code,
            client.get("/in/prot").status_code,
            client.get("/in/pub").status_code,
        ]

    assert statuses == [UNAUTHORIZED, UNAUTHORIZED, UNAUTHORIZED, OK]


def test_authentication_on_a_handler_serves_its_anonymous_route() -> None:
    """A handler's own `middleware=` runs after the gate admitted the request."""
    component = AuthenticatedRequests(verifier())
    middleware, options = component.asgi_middleware()
    on_handler = [DefineMiddleware(middleware, **options)]

    @get("/public", opt=Anonymous(), middleware=on_handler)
    async def public() -> str:
        return "public"

    @get("/private", middleware=on_handler)
    async def private() -> str:
        return "private"

    app = Litestar([public, private])
    Grelmicro(uses=[ErrorResponses(), component]).install(app)
    with TestClient(app) as client:
        statuses = [
            client.get("/public").status_code,
            client.get("/private").status_code,
            client.get("/private", headers=bearer(token())).status_code,
            client.get("/nowhere").status_code,
        ]

    assert statuses == [OK, UNAUTHORIZED, OK, UNAUTHORIZED]
