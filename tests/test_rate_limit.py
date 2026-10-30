"""Tests for rate limit decisions at the HTTP edge."""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

import anyio
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from grelmicro import Grelmicro
from grelmicro.clock import VirtualClock
from grelmicro.http import (
    ErrorResponses,
    RateLimitedRequests,
    RateLimitedRequestsConfig,
    RateLimitMiddleware,
)
from grelmicro.http._ratelimit import _has_less_left, _joined
from grelmicro.integrations.fastapi import RateLimited
from grelmicro.resilience import RateLimiter
from grelmicro.resilience.errors import RateLimitExceededError
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter
from grelmicro.resilience.ratelimiter.sliding_window import (
    SlidingWindowConfig,
)
from grelmicro.security import ClientAddressMiddleware, TrustedProxies
from tests._logs import records_of

if TYPE_CHECKING:
    from collections.abc import MutableMapping

pytestmark = [pytest.mark.timeout(5)]

HTTP_200_OK = 200
HTTP_429_TOO_MANY_REQUESTS = 429
CALLER = ("203.0.113.7", 5000)
OTHER_CALLER = ("198.51.100.9", 5000)
BURST = 2
WINDOW = 60
DAY = 86400
PROXIES = ("10.0.0.0/8",)
TWO_METERS = 2
COST = 3


def _limiter(
    name: str, limit: int, window: int | timedelta = WINDOW
) -> RateLimiter:
    """Return a limiter over a backend of its own."""
    return RateLimiter.sliding_window(
        name, limit=limit, window=window, backend=MemoryRateLimiterAdapter()
    )


def _app(
    *limiters: RateLimiter,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
    cost: int = 1,
    max_wait: float = 0.0,
    legacy_headers: bool = False,
    errors: ErrorResponses | None = None,
) -> FastAPI:
    """Return an app metering every request through the component."""
    micro = Grelmicro(
        uses=[
            errors or ErrorResponses(),
            RateLimitedRequests(
                *limiters,
                trusted=TrustedProxies(list(PROXIES)),
                include=include,
                exclude=exclude,
                cost=cost,
                max_wait=max_wait,
                legacy_headers=legacy_headers,
            ),
        ]
    )
    app = FastAPI()
    micro.install(app)

    @app.get("/read")
    async def read() -> dict[str, int]:
        return {"read": 1}

    @app.get("/livez")
    async def livez() -> dict[str, int]:
        return {"live": 1}

    return app


def _starlette(
    *limiters: RateLimiter,
    **options: Any,  # noqa: ANN401
) -> Starlette:
    """Return an app metering every request through a hand-added middleware."""

    async def read(_request: Request) -> Response:
        return Response(b"read")

    async def echo(request: Request) -> Response:
        return Response(await request.body())

    app = Starlette(
        routes=[
            Route("/read", read),
            Route("/livez", read),
            Route("/echo", echo, methods=["POST"]),
        ]
    )
    if "key" not in options:
        options["trusted"] = TrustedProxies(list(PROXIES))
    app.add_middleware(
        RateLimitMiddleware,
        limiters=list(limiters) or [_limiter("api", 10)],
        **options,
    )
    return app


def _slow_bucket(name: str = "api") -> RateLimiter:
    """Return a one-token bucket that refills one token every half second."""
    return RateLimiter.token_bucket(
        name, capacity=1, refill_rate=2, backend=MemoryRateLimiterAdapter()
    )


async def _answer_while_it_refills(
    app: Starlette, clock: VirtualClock
) -> httpx.Response:
    """Spend the one token, then answer a request a second of refill reaches."""
    transport = httpx.ASGITransport(app=app, client=CALLER)
    answers: list[httpx.Response] = []
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        await client.get("/read")

        async def ask() -> None:
            answers.append(await client.get("/read"))

        async with anyio.create_task_group() as group:
            group.start_soon(ask)
            await anyio.wait_all_tasks_blocked()
            await clock.advance(1.0)
    return answers[0]


# --- What a caller is told ---


def test_an_allowed_request_says_what_is_left() -> None:
    """A client learns its budget from the answer it already asked for."""
    # Arrange
    client = TestClient(_app(_limiter("api", 10)), client=CALLER)

    # Act
    with client:
        response = client.get("/read")

    # Assert
    assert response.status_code == HTTP_200_OK
    assert re.fullmatch(r'"api";r=9;t=\d+', response.headers["ratelimit"])
    assert response.headers["ratelimit-policy"] == '"api";q=10;w=60'


def test_a_refused_request_says_when_to_come_back() -> None:
    """`Retry-After` is what a client acts on, and the quota is why."""
    # Arrange
    client = TestClient(_app(_limiter("api", 1)), client=CALLER)

    # Act
    with client:
        client.get("/read")
        response = client.get("/read")

    # Assert
    assert response.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert response.headers["retry-after"]
    assert response.headers["ratelimit"].startswith('"api";r=0')
    assert response.json()["type"].endswith("#rate-limit-exceeded")
    assert response.headers["content-type"] == "application/problem+json"


def test_every_limiter_is_stated() -> None:
    """A burst limit beside a daily one is two policies, and says so."""
    # Arrange
    app = _app(_limiter("burst", BURST), _limiter("daily", 1000, DAY))
    client = TestClient(app, client=CALLER)

    # Act
    with client:
        response = client.get("/read")

    # Assert
    assert re.fullmatch(
        r'"burst";r=1;t=\d+, "daily";r=999;t=\d+',
        response.headers["ratelimit"],
    )
    assert (
        response.headers["ratelimit-policy"]
        == '"burst";q=2;w=60, "daily";q=1000;w=86400'
    )


def test_the_first_limiter_to_refuse_answers() -> None:
    """A request passes all of them or is turned away by one."""
    # Arrange
    app = _app(_limiter("burst", 1), _limiter("daily", 1000, DAY))
    client = TestClient(app, client=CALLER)

    # Act
    with client:
        client.get("/read")
        response = client.get("/read")

    # Assert
    assert response.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert "daily" not in response.headers["ratelimit"]


def test_a_token_bucket_states_no_policy() -> None:
    """Its reset is the wait for one token, not the edge of a window."""
    # Arrange
    bucket = RateLimiter.token_bucket(
        "api",
        capacity=10,
        refill_rate=1,
        backend=MemoryRateLimiterAdapter(),
    )
    client = TestClient(_app(bucket), client=CALLER)

    # Act
    with client:
        response = client.get("/read")

    # Assert
    assert response.headers["ratelimit"].startswith('"api";r=9')
    assert "ratelimit-policy" not in response.headers


def test_the_superseded_fields_are_sent_when_asked_for() -> None:
    """A client that reads only those still learns its budget."""
    # Arrange
    app = _app(_limiter("api", 10), legacy_headers=True)
    client = TestClient(app, client=CALLER)

    # Act
    with client:
        response = client.get("/read")

    # Assert
    assert response.headers["x-ratelimit-limit"] == "10"
    assert response.headers["x-ratelimit-remaining"] == "9"
    assert response.headers["x-ratelimit-reset"].isdigit()


def test_the_superseded_fields_carry_the_limiter_closest_to_spent() -> None:
    """They hold one budget, and the one that refuses first is the honest one."""
    # Arrange
    app = _app(
        _limiter("daily", 1000, DAY), _limiter("burst", 3), legacy_headers=True
    )
    client = TestClient(app, client=CALLER)

    # Act
    with client:
        response = client.get("/read")

    # Assert
    assert response.headers["x-ratelimit-limit"] == "3"
    assert response.headers["x-ratelimit-remaining"] == "2"


@pytest.mark.parametrize(
    "max_wait", [0.0, 1.0], ids=["refusing at once", "waiting first"]
)
def test_a_request_spends_its_cost_of_every_limiter(max_wait: float) -> None:
    """A request priced above one token spends all of them."""
    # Arrange
    app = _app(
        _limiter("api", 10),
        _limiter("burst", 5),
        cost=COST,
        max_wait=max_wait,
    )
    client = TestClient(app, client=CALLER)

    # Act
    with client:
        response = client.get("/read")

    # Assert
    assert re.fullmatch(
        rf'"api";r={10 - COST};t=\d+, "burst";r={5 - COST};t=\d+',
        response.headers["ratelimit"],
    )


def test_a_refusal_answers_in_the_registered_format() -> None:
    """A client parses one error format, whichever component refused it."""
    # Arrange
    app = _app(_limiter("api", 1), errors=ErrorResponses.tmf())
    client = TestClient(app, client=CALLER)

    # Act
    with client:
        client.get("/read")
        response = client.get("/read")

    # Assert
    assert response.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert response.headers["content-type"] == "application/json"


# --- Who is metered ---


@pytest.mark.parametrize(
    "max_wait", [0.0, 0.05], ids=["refusing at once", "waiting first"]
)
def test_two_callers_are_two_buckets(max_wait: float) -> None:
    """One caller spending its budget must not spend everybody's."""
    # Arrange
    app = _app(_limiter("api", 1), max_wait=max_wait)

    # Act
    with TestClient(app, client=CALLER) as client:
        client.get("/read")
        spent = client.get("/read")
    with TestClient(app, client=OTHER_CALLER) as other:
        fresh = other.get("/read")

    # Assert
    assert spent.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert fresh.status_code == HTTP_200_OK


def test_a_trusted_proxy_is_believed() -> None:
    """Behind an ingress the peer is the ingress, and the header is the caller."""
    # Arrange
    app = _app(_limiter("api", 1))
    proxy = ("10.1.2.3", 5000)

    # Act
    with TestClient(app, client=proxy) as client:
        first = client.get("/read", headers={"X-Forwarded-For": "203.0.113.1"})
        other = client.get("/read", headers={"X-Forwarded-For": "203.0.113.2"})

    # Assert
    assert first.status_code == other.status_code == HTTP_200_OK


def test_an_untrusted_peer_keeps_its_own_bucket() -> None:
    """A caller writing its own forwarded header meters only itself."""
    # Arrange
    app = _app(_limiter("api", 1))

    # Act
    with TestClient(app, client=CALLER) as client:
        client.get("/read", headers={"X-Forwarded-For": "198.51.100.1"})
        response = client.get(
            "/read", headers={"X-Forwarded-For": "198.51.100.2"}
        )

    # Assert
    assert response.status_code == HTTP_429_TOO_MANY_REQUESTS


def test_a_key_builder_replaces_the_caller() -> None:
    """A service that meters by tenant says what a tenant is."""
    # Arrange
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(
                _limiter("api", 1),
                key=lambda scope: (
                    dict(scope["headers"]).get(b"x-tenant", b"none").decode()
                ),
            ),
        ]
    )
    app = FastAPI()
    micro.install(app)

    @app.get("/read")
    async def read() -> dict[str, int]:
        return {"read": 1}

    # Act
    with TestClient(app, client=CALLER) as client:
        client.get("/read", headers={"X-Tenant": "a"})
        same = client.get("/read", headers={"X-Tenant": "a"})
        other = client.get("/read", headers={"X-Tenant": "b"})

    # Assert
    assert same.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert other.status_code == HTTP_200_OK


def test_a_key_builder_that_returns_none_leaves_the_call_alone() -> None:
    """`None` is how a builder says this one is not metered."""
    # Arrange
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(_limiter("api", 1), key=lambda _scope: None),
        ]
    )
    app = FastAPI()
    micro.install(app)

    @app.get("/read")
    async def read() -> dict[str, int]:
        return {"read": 1}

    # Act
    with TestClient(app, client=CALLER) as client:
        client.get("/read")
        response = client.get("/read")

    # Assert
    assert response.status_code == HTTP_200_OK


def test_a_call_left_unmetered_still_reaches_the_app_whole() -> None:
    """A request the builder leaves alone keeps its body."""
    # Arrange
    app = _starlette(key=lambda _scope: None)

    # Act
    with TestClient(app, client=CALLER) as client:
        response = client.post("/echo", content=b"payload")

    # Assert
    assert response.content == b"payload"


def test_a_caller_that_cannot_be_read_is_let_through_once_said(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A peer the transport did not give is not a caller to meter."""
    # Arrange
    app = _app(_limiter("api", 1))

    # Act
    with (
        caplog.at_level(logging.WARNING, logger="grelmicro.http.ratelimit"),
        TestClient(app) as client,
    ):
        client.get("/read")
        response = client.get("/read")

    # Assert
    assert response.status_code == HTTP_200_OK
    assert len(records_of(caplog, "grelmicro.http.ratelimit")) == 1
    assert "transport peer" in caplog.text


def test_a_resolved_caller_is_never_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A caller that was metered is not a misconfiguration to log."""
    # Arrange
    app = _app(_limiter("api", 10))

    # Act
    with (
        caplog.at_level(logging.WARNING, logger="grelmicro.http.ratelimit"),
        TestClient(app, client=CALLER) as client,
    ):
        client.get("/read")

    # Assert
    assert records_of(caplog, "grelmicro.http.ratelimit") == []


def test_the_resolved_caller_is_left_where_the_next_reader_looks() -> None:
    """One walk of the forwarded header serves everything downstream."""
    # Arrange
    seen: list[str] = []

    async def handler(request: Any) -> Response:  # noqa: ANN401
        seen.append(request.scope["state"]["client_address"].key)
        return Response(b"ok")

    app = Starlette(routes=[Route("/read", handler)])
    Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(
                _limiter("api", 10), trusted=TrustedProxies(list(PROXIES))
            ),
        ]
    ).install(app)

    # Act
    with TestClient(app, client=CALLER) as client:
        client.get("/read")

    # Assert
    assert seen == [CALLER[0]]


# --- What is not metered ---


def test_a_caller_another_middleware_resolved_is_reused() -> None:
    """The forwarded header is walked once, whoever walks it."""
    # Arrange
    app = _app(_limiter("api", 1))
    app.add_middleware(
        ClientAddressMiddleware, trusted=TrustedProxies(list(PROXIES))
    )

    # Act
    with TestClient(app, client=CALLER) as client:
        client.get("/read")
        response = client.get("/read")

    # Assert
    assert response.status_code == HTTP_429_TOO_MANY_REQUESTS


def test_an_excluded_path_is_never_metered() -> None:
    """A probe polled every few seconds must not spend a caller's budget."""
    # Arrange
    app = _app(_limiter("api", 1), exclude=("/livez",))
    client = TestClient(app, client=CALLER)

    # Act
    with client:
        client.get("/livez")
        client.get("/livez")
        response = client.get("/read")

    # Assert
    assert response.status_code == HTTP_200_OK


def test_a_path_outside_include_is_never_metered() -> None:
    """Naming the paths to meter leaves every other path alone."""
    # Arrange
    app = _app(_limiter("api", 1), include=("/read",))
    client = TestClient(app, client=CALLER)

    # Act
    with client:
        client.get("/livez")
        response = client.get("/read")

    # Assert
    assert response.status_code == HTTP_200_OK


def test_a_websocket_scope_passes_through() -> None:
    """A rate limiter at the HTTP edge answers HTTP and nothing else."""
    # Arrange
    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        seen.append(scope["type"])

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        """Take what the app says nowhere."""

    middleware = RateLimitMiddleware(
        app, limiters=[_limiter("api", 1)], key=lambda _scope: "one"
    )

    # Act
    anyio.run(middleware, {"type": "websocket"}, receive, send)

    # Assert
    assert seen == ["websocket"]


# --- Added by hand ---


@pytest.mark.parametrize(
    ("options", "path", "header", "stated"),
    [
        ({}, "/read", "x-ratelimit-remaining", "absent"),
        ({"cost": 3}, "/read", "ratelimit", '"api";r=7;'),
        ({"include": ("/read",)}, "/livez", "ratelimit", "absent"),
        ({"exclude": ("/livez",)}, "/livez", "ratelimit", "absent"),
        ({"legacy_headers": True}, "/read", "x-ratelimit-remaining", "9"),
    ],
    ids=["defaults", "cost", "include", "exclude", "superseded fields"],
)
def test_a_hand_added_middleware_takes_every_option(
    options: dict[str, Any], path: str, header: str, stated: str
) -> None:
    """Added by hand, it answers as the registered component does."""
    # Arrange
    app = _starlette(**options)

    # Act
    with TestClient(app, client=CALLER) as client:
        response = client.get(path)

    # Assert
    assert response.headers.get(header, "absent").startswith(stated)


@pytest.mark.parametrize(
    ("options", "status"),
    [
        ({}, HTTP_429_TOO_MANY_REQUESTS),
        ({"max_wait": 1.0}, HTTP_200_OK),
    ],
    ids=["refusing at once", "waiting first"],
)
async def test_a_hand_added_middleware_waits_only_when_told(
    options: dict[str, Any], status: int, clock: VirtualClock
) -> None:
    """Waiting at the edge holds the connection open, so it is opted into."""
    # Arrange
    app = _starlette(_slow_bucket(), **options)

    # Act
    response = await _answer_while_it_refills(app, clock)

    # Assert
    assert response.status_code == status


def test_two_callers_are_two_flood_buckets() -> None:
    """One caller flooding must not turn everybody away."""
    # Arrange
    app = _starlette(flood=_limiter("flood", 1))

    # Act
    with TestClient(app, client=CALLER) as client:
        client.get("/read")
        spent = client.get("/read")
    with TestClient(app, client=OTHER_CALLER) as other:
        fresh = other.get("/read")

    # Assert
    assert spent.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert fresh.status_code == HTTP_200_OK


async def test_the_flood_limit_refuses_at_once(clock: VirtualClock) -> None:
    """A flood is answered before routing, never held waiting there."""
    # Arrange
    app = _starlette(flood=_slow_bucket("flood"), max_wait=1.0)

    # Act
    response = await _answer_while_it_refills(app, clock)

    # Assert
    assert response.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert response.headers["ratelimit"].startswith('"flood";r=0;')


def test_a_refusal_outside_any_framework_is_still_answered() -> None:
    """A pure ASGI scope carries no app to read an error format from."""
    # Arrange
    sent: list[MutableMapping[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
        """Serve the allowed call with nothing."""

    async def receive() -> MutableMapping[str, Any]:
        return {"type": "http.request", "body": b""}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    middleware = RateLimitMiddleware(
        app, limiters=[_limiter("api", 1)], key=lambda _scope: "one"
    )
    scope = {"type": "http", "method": "GET", "path": "/read", "headers": []}

    async def twice() -> None:
        await middleware(dict(scope), receive, send)
        await middleware(dict(scope), receive, send)

    # Act
    anyio.run(twice)

    # Assert
    assert sent[0]["status"] == HTTP_429_TOO_MANY_REQUESTS


# --- What is refused where it is written ---


def test_a_middleware_that_meters_nothing_is_refused() -> None:
    """It would let every request through while reporting a limit."""
    # Act / Assert
    with pytest.raises(TypeError, match="at least one limiter") as caught:
        RateLimitedRequests(trusted=TrustedProxies(list(PROXIES)))
    assert str(caught.value).startswith("RateLimitedRequests ")
    assert "RateLimitMiddleware" not in str(caught.value)


def test_a_middleware_with_no_caller_to_meter_is_refused() -> None:
    """The only key left would be the ingress rather than the caller."""
    # Act / Assert
    with pytest.raises(TypeError) as caught:
        RateLimitedRequests(_limiter("api", 1))
    message = str(caught.value)
    assert message.startswith("RateLimitedRequests ")
    assert "RateLimitMiddleware" not in message
    assert "trusted=TrustedProxies(" in message
    assert "key=" in message


@pytest.mark.parametrize(
    "name", ['say "hi"', "back\\slash", "café"], ids=["quote", "escape", "utf8"]
)
def test_a_name_a_header_cannot_carry_is_refused(name: str) -> None:
    """The name is quoted into the header, so it has to fit in one."""
    # Act / Assert
    with pytest.raises(ValueError, match="RateLimit header"):
        RateLimitedRequests(
            _limiter(name, 1), trusted=TrustedProxies(list(PROXIES))
        )


def test_a_route_that_meters_nothing_is_refused() -> None:
    """Declaring it with no limiter would meter nothing and say it does."""
    # Act / Assert
    with pytest.raises(TypeError, match="at least one limiter"):
        RateLimited()


# --- Per route ---


def test_a_route_spends_its_own_quota_as_well() -> None:
    """An expensive route has a budget of its own on top of the app's."""
    # Arrange
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(
                _limiter("api", 100), trusted=TrustedProxies(list(PROXIES))
            ),
        ]
    )
    app = FastAPI()
    micro.install(app)
    search = _limiter("search", 1)

    @app.get("/search", dependencies=[RateLimited(search)])
    async def do_search() -> dict[str, int]:
        return {"hits": 1}

    @app.get("/read")
    async def read() -> dict[str, int]:
        return {"read": 1}

    # Act
    with TestClient(app, client=CALLER) as client:
        client.get("/search")
        spent = client.get("/search")
        other = client.get("/read")

    # Assert
    assert spent.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert other.status_code == HTTP_200_OK


def test_a_route_states_its_own_quota_too() -> None:
    """Both budgets are the caller's to read."""
    # Arrange
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(
                _limiter("api", 100), trusted=TrustedProxies(list(PROXIES))
            ),
        ]
    )
    app = FastAPI()
    micro.install(app)

    @app.get("/search", dependencies=[RateLimited(_limiter("search", 5))])
    async def do_search() -> dict[str, int]:
        return {"hits": 1}

    # Act
    with TestClient(app, client=CALLER) as client:
        response = client.get("/search")

    # Assert
    assert '"search"' in response.headers["ratelimit"]
    assert '"api"' in response.headers["ratelimit"]


def test_a_route_refusal_carries_what_it_spent() -> None:
    """The quota travels with the status, on the refusal path too."""
    # Arrange
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(
                _limiter("api", 100), trusted=TrustedProxies(list(PROXIES))
            ),
        ]
    )
    app = FastAPI()
    micro.install(app)

    @app.get("/search", dependencies=[RateLimited(_limiter("search", 1))])
    async def do_search() -> dict[str, int]:
        return {"hits": 1}

    # Act
    with TestClient(app, client=CALLER) as client:
        client.get("/search")
        response = client.get("/search")

    # Assert
    assert response.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert '"search";r=0' in response.headers["ratelimit"]
    assert response.headers["retry-after"]


def test_a_route_with_no_caller_to_meter_leaves_the_call_alone() -> None:
    """Metering under the ingress would meter every caller as one."""
    # Arrange
    app = FastAPI()
    Grelmicro(uses=[ErrorResponses()]).install(app)

    @app.get("/search", dependencies=[RateLimited(_limiter("search", 1))])
    async def do_search() -> dict[str, int]:
        return {"hits": 1}

    # Act
    with TestClient(app, client=CALLER) as client:
        client.get("/search")
        response = client.get("/search")

    # Assert
    assert response.status_code == HTTP_200_OK


def test_a_route_resolves_the_caller_itself_when_nothing_else_did() -> None:
    """A route may be the only thing metering, and still meter the caller."""
    # Arrange
    app = FastAPI()
    Grelmicro(uses=[ErrorResponses()]).install(app)

    @app.get(
        "/search",
        dependencies=[
            RateLimited(
                _limiter("search", 1),
                trusted=TrustedProxies(list(PROXIES)),
            )
        ],
    )
    async def do_search() -> dict[str, int]:
        return {"hits": 1}

    # Act
    with TestClient(app, client=CALLER) as client:
        client.get("/search")
        spent = client.get("/search")
    with TestClient(app, client=OTHER_CALLER) as other:
        fresh = other.get("/search")

    # Assert
    assert spent.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert fresh.status_code == HTTP_200_OK


def test_a_route_key_builder_replaces_the_caller() -> None:
    """A route that meters by tenant says what a tenant is."""
    # Arrange
    app = FastAPI()
    Grelmicro(uses=[ErrorResponses()]).install(app)

    @app.get(
        "/search",
        dependencies=[
            RateLimited(
                _limiter("search", 1),
                key=lambda scope: (
                    dict(scope["headers"]).get(b"x-tenant", b"none").decode()
                ),
            )
        ],
    )
    async def do_search() -> dict[str, int]:
        return {"hits": 1}

    # Act
    with TestClient(app, client=CALLER) as client:
        client.get("/search", headers={"X-Tenant": "a"})
        same = client.get("/search", headers={"X-Tenant": "a"})
        other = client.get("/search", headers={"X-Tenant": "b"})

    # Assert
    assert same.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert other.status_code == HTTP_200_OK


# --- Waiting ---


def test_a_budget_waits_rather_than_refusing() -> None:
    """A caller given a budget waits for tokens instead of a 429."""
    # Arrange
    app = _app(
        RateLimiter.token_bucket(
            "api",
            capacity=1,
            refill_rate=100,
            backend=MemoryRateLimiterAdapter(),
        ),
        max_wait=1.0,
    )
    client = TestClient(app, client=CALLER)

    # Act
    with client:
        client.get("/read")
        response = client.get("/read")

    # Assert
    assert response.status_code == HTTP_200_OK


def test_a_budget_that_runs_out_is_a_refusal_not_a_failure() -> None:
    """A wait that gives up is still the limiter turning a caller away."""
    # Arrange
    app = _app(_limiter("api", 1), max_wait=0.05)
    client = TestClient(app, client=CALLER)

    # Act
    with client:
        client.get("/read")
        response = client.get("/read")

    # Assert
    assert response.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert response.headers["retry-after"]
    assert response.headers["ratelimit"].startswith('"api";r=0')
    assert response.headers["ratelimit-policy"] == '"api";q=1;w=60'
    assert response.headers["content-type"] == "application/problem+json"


def test_a_route_budget_that_runs_out_states_its_quota_too() -> None:
    """The refusal carries what it spent, whichever path refused it."""
    # Arrange
    app = FastAPI()
    Grelmicro(uses=[ErrorResponses()]).install(app)

    @app.get(
        "/search",
        dependencies=[
            RateLimited(
                _limiter("search", 1),
                max_wait=0.05,
                trusted=TrustedProxies(list(PROXIES)),
            )
        ],
    )
    async def do_search() -> dict[str, int]:
        return {"hits": 1}

    # Act
    with TestClient(app, client=CALLER) as client:
        client.get("/search")
        response = client.get("/search")

    # Assert
    assert response.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert '"search";r=0' in response.headers["ratelimit"]


def test_a_route_budget_waits_rather_than_refusing() -> None:
    """A route given a budget waits for tokens the way the edge does."""
    # Arrange
    app = FastAPI()
    Grelmicro(uses=[ErrorResponses()]).install(app)

    @app.get(
        "/search",
        dependencies=[
            RateLimited(
                _slow_bucket("search"),
                max_wait=1.0,
                trusted=TrustedProxies(list(PROXIES)),
            )
        ],
    )
    async def do_search() -> dict[str, int]:
        return {"hits": 1}

    # Act
    with TestClient(app, client=CALLER) as client:
        client.get("/search")
        response = client.get("/search")

    # Assert
    assert response.status_code == HTTP_200_OK


def test_a_route_states_the_superseded_fields_when_asked_for() -> None:
    """A client that reads only those learns the route's budget, refused or not."""
    # Arrange
    app = FastAPI()
    Grelmicro(uses=[ErrorResponses()]).install(app)

    @app.get(
        "/search",
        dependencies=[
            RateLimited(
                _limiter("search", 1),
                legacy_headers=True,
                trusted=TrustedProxies(list(PROXIES)),
            )
        ],
    )
    async def do_search() -> dict[str, int]:
        return {"hits": 1}

    # Act
    with TestClient(app, client=CALLER) as client:
        served = client.get("/search")
        refused = client.get("/search")

    # Assert
    assert served.headers["x-ratelimit-remaining"] == "0"
    assert refused.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert refused.headers["x-ratelimit-limit"] == "1"


def test_a_route_refusal_names_the_bucket_it_refused() -> None:
    """A handler of its own reads which caller was turned away."""
    # Arrange
    app = FastAPI()
    refused: list[str] = []

    async def answer(_request: Request, exc: Exception) -> Response:
        refused.append(cast("RateLimitExceededError", exc).key)
        return Response(status_code=HTTP_429_TOO_MANY_REQUESTS)

    app.add_exception_handler(RateLimitExceededError, answer)

    @app.get(
        "/search",
        dependencies=[RateLimited(_limiter("search", 1), key=lambda _: "a")],
    )
    async def do_search() -> dict[str, int]:
        return {"hits": 1}

    # Act
    with TestClient(app, client=CALLER) as client:
        client.get("/search")
        client.get("/search")

    # Assert
    assert refused == ["a"]


@pytest.mark.parametrize(
    "own_response",
    [False, True],
    ids=["built by the framework", "own response"],
)
def test_the_superseded_fields_carry_a_tighter_route(
    *, own_response: bool
) -> None:
    """The route's budget is the one this caller is refused by first."""
    # Arrange
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(
                _limiter("api", 1000),
                trusted=TrustedProxies(list(PROXIES)),
                legacy_headers=True,
            ),
        ]
    )
    app = FastAPI()
    micro.install(app)

    @app.get(
        "/search",
        dependencies=[RateLimited(_limiter("search", 2), legacy_headers=True)],
    )
    async def do_search() -> Any:  # noqa: ANN401
        return JSONResponse({"hits": 1}) if own_response else {"hits": 1}

    # Act
    with TestClient(app, client=CALLER) as client:
        response = client.get("/search")

    # Assert
    assert response.headers["x-ratelimit-limit"] == "2"
    assert response.headers["x-ratelimit-remaining"] == "1"


def test_a_handlers_tighter_count_stands() -> None:
    """A count the handler stated itself answers when it has less left."""
    # Arrange
    app = _app(_limiter("api", 10), legacy_headers=True)

    @app.get("/own")
    async def own() -> Response:
        return JSONResponse({"own": 1}, headers={"X-RateLimit-Remaining": "1"})

    # Act
    with TestClient(app, client=CALLER) as client:
        response = client.get("/own")

    # Assert
    assert response.headers["x-ratelimit-remaining"] == "1"


@pytest.mark.parametrize("cost", [0, 50], ids=["nothing", "more than exists"])
def test_a_cost_no_limiter_can_serve_is_refused(cost: int) -> None:
    """Every request would fail, and it would fail as a server error."""
    # Act / Assert
    with pytest.raises(ValueError, match="cost"):
        RateLimitedRequests(
            _limiter("api", 5),
            trusted=TrustedProxies(list(PROXIES)),
            cost=cost,
        )


def test_two_meters_state_one_field_each() -> None:
    """A client reads one `RateLimit`, and one of each superseded field."""
    # Arrange
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(
                _limiter("api", 10),
                trusted=TrustedProxies(list(PROXIES)),
                legacy_headers=True,
            ),
        ]
    )
    app = FastAPI()
    micro.install(app)

    @app.get(
        "/search",
        dependencies=[RateLimited(_limiter("search", 2), legacy_headers=True)],
    )
    async def do_search() -> dict[str, int]:
        return {"hits": 1}

    # Act
    with TestClient(app, client=CALLER) as client:
        response = client.get("/search")

    # Assert
    assert response.headers["x-ratelimit-limit"].isdigit()
    assert '"search"' in response.headers["ratelimit"]
    assert '"api"' in response.headers["ratelimit"]
    assert response.headers["ratelimit"].count('";r=') == TWO_METERS


def test_a_reset_is_rounded_up() -> None:
    """Rounding down invites a retry that is refused again."""
    # Arrange
    app = _app(_limiter("api", 1, window=1), legacy_headers=True)
    client = TestClient(app, client=CALLER)

    # Act
    with client:
        client.get("/read")
        response = client.get("/read")

    # Assert
    assert response.headers["ratelimit"] == '"api";r=0;t=1'
    assert response.headers["x-ratelimit-remaining"] == "0"
    assert response.headers["x-ratelimit-reset"] == "1"


@pytest.mark.parametrize(
    ("window", "policy"),
    [
        (timedelta(milliseconds=1400), '"api";q=10;w=2'),
        (timedelta(seconds=1), '"api";q=10;w=1'),
    ],
    ids=["rounded up", "one second"],
)
def test_a_window_is_never_published_shorter_than_it_is(
    window: timedelta, policy: str
) -> None:
    """Published short, it invites a pace the limiter refuses."""
    # Arrange
    app = _app(_limiter("api", 10, window=window))
    client = TestClient(app, client=CALLER)

    # Act
    with client:
        response = client.get("/read")

    # Assert
    assert response.headers["ratelimit-policy"] == policy


@pytest.mark.parametrize("cost", [0, 50], ids=["nothing", "more than exists"])
def test_a_route_cost_no_limiter_can_serve_is_refused(cost: int) -> None:
    """The route checks what the middleware checks, where it is written."""
    # Act / Assert
    with pytest.raises(ValueError, match="cost"):
        RateLimited(_limiter("search", 5), cost=cost)


def test_a_route_name_a_header_cannot_carry_is_refused() -> None:
    """It would fail on every call it was declared to meter."""
    # Act / Assert
    with pytest.raises(ValueError, match="RateLimit header"):
        RateLimited(_limiter('bad"name', 5))


def test_a_route_metering_nothing_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Silently metering nothing is the one thing it must not do."""
    # Arrange
    app = FastAPI()
    Grelmicro(uses=[ErrorResponses()]).install(app)

    @app.get("/search", dependencies=[RateLimited(_limiter("search", 1))])
    async def do_search() -> dict[str, int]:
        return {"hits": 1}

    # Act
    with (
        caplog.at_level(logging.WARNING, logger="grelmicro.integrations"),
        TestClient(app, client=CALLER) as client,
    ):
        client.get("/search")
        response = client.get("/search")

    # Assert
    assert response.status_code == HTTP_200_OK
    assert "metered not at all" in caplog.text
    assert len(records_of(caplog, "grelmicro.integrations")) == 1


def test_a_route_metering_nothing_logs_as_the_fastapi_integration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The warning names the FastAPI integration's logger."""
    # Arrange
    app = FastAPI()
    Grelmicro(uses=[ErrorResponses()]).install(app)

    @app.get("/search", dependencies=[RateLimited(_limiter("search", 1))])
    async def do_search() -> dict[str, int]:
        return {"hits": 1}

    # Act
    with (
        caplog.at_level(logging.WARNING, logger="grelmicro.integrations"),
        TestClient(app, client=CALLER) as client,
    ):
        client.get("/search")

    # Assert
    assert [
        record.name for record in records_of(caplog, "grelmicro.integrations")
    ] == ["grelmicro.integrations.fastapi"]


def test_a_route_that_resolves_nobody_meters_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A peer the transport did not give is not a caller to meter."""
    # Arrange
    app = FastAPI()
    Grelmicro(uses=[ErrorResponses()]).install(app)

    @app.get(
        "/search",
        dependencies=[
            RateLimited(
                _limiter("search", 1),
                trusted=TrustedProxies(list(PROXIES)),
            )
        ],
    )
    async def do_search() -> dict[str, int]:
        return {"hits": 1}

    # Act
    with (
        caplog.at_level(logging.WARNING, logger="grelmicro.integrations"),
        TestClient(app) as client,
    ):
        client.get("/search")
        response = client.get("/search")

    # Assert
    assert response.status_code == HTTP_200_OK
    assert "metered not at all" in caplog.text
    assert "trusted=" not in caplog.text


@pytest.mark.parametrize(
    "trusted",
    [
        pytest.param(None, id="no proxies"),
        pytest.param(TrustedProxies(list(PROXIES)), id="trusted proxies"),
    ],
)
def test_a_route_key_builder_that_returns_none_is_not_reported(
    caplog: pytest.LogCaptureFixture,
    trusted: TrustedProxies | None,
) -> None:
    """`None` is how a route's builder says this call is not metered."""
    # Arrange
    app = FastAPI()
    Grelmicro(uses=[ErrorResponses()]).install(app)

    @app.get(
        "/search",
        dependencies=[
            RateLimited(
                _limiter("search", 1), key=lambda _: None, trusted=trusted
            )
        ],
    )
    async def do_search() -> dict[str, int]:
        return {"hits": 1}

    # Act
    with (
        caplog.at_level(logging.WARNING, logger="grelmicro.integrations"),
        TestClient(app, client=CALLER) as client,
    ):
        client.get("/search")
        response = client.get("/search")

    # Assert
    assert response.status_code == HTTP_200_OK
    assert records_of(caplog, "grelmicro.integrations") == []


def test_the_superseded_fields_carry_the_meter_that_refuses_first() -> None:
    """A client reading only those must not be told the wrong budget."""
    # Arrange
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(
                _limiter("burst", 3),
                trusted=TrustedProxies(list(PROXIES)),
                legacy_headers=True,
            ),
        ]
    )
    app = FastAPI()
    micro.install(app)

    @app.get(
        "/search",
        dependencies=[
            RateLimited(_limiter("route", 1000), legacy_headers=True)
        ],
    )
    async def do_search() -> dict[str, int]:
        return {"hits": 1}

    # Act
    with TestClient(app, client=CALLER) as client:
        response = client.get("/search")

    # Assert
    assert response.headers["x-ratelimit-limit"] == "3"
    assert response.headers["x-ratelimit-remaining"] == "2"


def test_a_window_shorter_than_a_second_states_no_policy() -> None:
    """A whole second would publish half the rate it enforces."""
    # Arrange
    app = _app(_limiter("api", 100, window=timedelta(milliseconds=500)))
    client = TestClient(app, client=CALLER)

    # Act
    with client:
        response = client.get("/read")

    # Assert
    assert "ratelimit-policy" not in response.headers


def test_a_rejection_carrying_header_pairs_is_answered() -> None:
    """A list of pairs is what an ASGI-minded caller reaches for."""
    # Arrange
    app = FastAPI()
    Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(
                _limiter("api", 10), trusted=TrustedProxies(list(PROXIES))
            ),
        ]
    ).install(app)

    @app.get("/boom")
    async def boom() -> dict[str, int]:
        error = RateLimitExceededError(key="one", retry_after=1.0)
        error.headers = [("x-mine", "yes")]  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
        raise error

    # Act
    with TestClient(app, client=CALLER) as client:
        response = client.get("/boom")

    # Assert
    assert response.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert response.headers["x-mine"] == "yes"


def test_a_route_returning_a_response_states_its_quota_too() -> None:
    """A framework merges a dependency's headers into what it builds only."""
    # Arrange
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(
                _limiter("api", 100), trusted=TrustedProxies(list(PROXIES))
            ),
        ]
    )
    app = FastAPI()
    micro.install(app)

    @app.get("/search", dependencies=[RateLimited(_limiter("search", 5))])
    async def do_search() -> Response:
        return JSONResponse({"hits": 1})

    # Act
    with TestClient(app, client=CALLER) as client:
        response = client.get("/search")

    # Assert
    assert '"search"' in response.headers["ratelimit"]
    assert '"api"' in response.headers["ratelimit"]


def test_a_stated_header_that_is_not_a_number_is_passed_over() -> None:
    """A value written by something else is not ours to fail a response on."""
    # Arrange
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(
                _limiter("api", 10),
                trusted=TrustedProxies(list(PROXIES)),
                legacy_headers=True,
            ),
        ]
    )
    app = FastAPI()
    micro.install(app)

    @app.get("/read")
    async def read() -> Response:
        return JSONResponse(
            {"read": 1}, headers={"X-RateLimit-Remaining": "unlimited"}
        )

    # Act
    with TestClient(app, client=CALLER) as client:
        response = client.get("/read")

    # Assert
    assert response.status_code == HTTP_200_OK


def test_a_bucket_that_is_only_your_proxy_meters_nobody(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Metering every caller behind it as one lets any spend all."""
    # Arrange
    app = _app(_limiter("api", 1))
    proxy = ("10.1.2.3", 5000)

    # Act
    with (
        caplog.at_level(logging.WARNING, logger="grelmicro.http.ratelimit"),
        TestClient(app, client=proxy) as client,
    ):
        client.get("/read", headers={"X-Forwarded-For": "10.9.9.9"})
        response = client.get("/read", headers={"X-Forwarded-For": "10.9.9.9"})

    # Assert
    assert response.status_code == HTTP_200_OK
    assert "share" in caplog.text


@pytest.mark.parametrize(
    "name", ["a,b", "a;b", "a=b"], ids=["comma", "semicolon", "equals"]
)
def test_a_name_the_field_cannot_be_read_back_from_is_refused(
    name: str,
) -> None:
    """The name is quoted into the field and parsed back out of it."""
    # Act / Assert
    with pytest.raises(ValueError, match="RateLimit header"):
        RateLimitedRequests(
            _limiter(name, 1), trusted=TrustedProxies(list(PROXIES))
        )


def test_one_policy_metered_twice_states_the_smaller_count() -> None:
    """A client paces itself off `r=`, so it reads the one that refuses."""
    # Arrange
    shared = _limiter("shared", 100)
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(shared, trusted=TrustedProxies(list(PROXIES))),
        ]
    )
    app = FastAPI()
    micro.install(app)

    @app.get("/search", dependencies=[RateLimited(shared, cost=10)])
    async def do_search() -> dict[str, int]:
        return {"hits": 1}

    # Act
    with TestClient(app, client=CALLER) as client:
        response = client.get("/search")

    # Assert
    assert re.fullmatch(r'"shared";r=89;t=\d+', response.headers["ratelimit"])


@pytest.mark.parametrize(
    ("stated", "kept"),
    [
        (b'"a";t=1', b'"a";r=0;t=2'),
        (b'"a";r=x;t=1', b'"a";r=0;t=2'),
        (b'"a";r=9;t=1', b'"a";r=0;t=2'),
        (b'"a";r=0;t=1', b'"a";r=0;t=2'),
    ],
    ids=["no count", "unreadable count", "larger count", "same count"],
)
def test_a_policy_without_a_readable_count_yields_to_one(
    stated: bytes, kept: bytes
) -> None:
    """A count that cannot be read is not a count to pace off."""
    # Act
    joined = _joined(stated, kept)

    # Assert
    assert joined == kept


def test_every_policy_of_a_list_is_kept() -> None:
    """A List of two policies joined with a third states all three."""
    # Act
    joined = _joined(b'"a";r=1;t=1, "b";r=2;t=1', b'"c";r=3;t=1')

    # Assert
    assert joined == b'"a";r=1;t=1, "b";r=2;t=1, "c";r=3;t=1'


def test_two_meters_on_one_route_are_two_policies() -> None:
    """A router's budget and a route's are both spent, so both are said."""
    # Arrange
    app = FastAPI()
    Grelmicro(uses=[ErrorResponses()]).install(app)

    @app.get(
        "/search",
        dependencies=[
            RateLimited(
                _limiter("tenant", 50), trusted=TrustedProxies(list(PROXIES))
            ),
            RateLimited(
                _limiter("search", 5), trusted=TrustedProxies(list(PROXIES))
            ),
        ],
    )
    async def do_search() -> dict[str, int]:
        return {"hits": 1}

    # Act
    with TestClient(app, client=CALLER) as client:
        response = client.get("/search")

    # Assert
    assert '"tenant"' in response.headers["ratelimit"]
    assert '"search"' in response.headers["ratelimit"]


def test_a_degraded_bucket_is_reported_once_and_only_as_itself(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two contradictory diagnoses send an operator to the wrong setting."""
    # Arrange
    app = _app(_limiter("api", 1))
    proxy = ("10.1.2.3", 5000)

    # Act
    with (
        caplog.at_level(logging.WARNING, logger="grelmicro.http.ratelimit"),
        TestClient(app, client=proxy) as client,
    ):
        client.get("/read", headers={"X-Forwarded-For": "10.9.9.9"})

    # Assert
    assert len(records_of(caplog, "grelmicro.http.ratelimit")) == 1
    assert "share" in caplog.text
    assert "transport peer" not in caplog.text


def test_two_apps_each_report_their_own_misconfiguration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One process holds two of these, and the second is not the first."""
    # Arrange
    first = _app(_limiter("first", 1))
    second = _app(_limiter("second", 1))
    proxy = ("10.1.2.3", 5000)
    forwarded = {"X-Forwarded-For": "10.9.9.9"}

    # Act
    with caplog.at_level(logging.WARNING, logger="grelmicro.http.ratelimit"):
        with TestClient(first, client=proxy) as client:
            client.get("/read", headers=forwarded)
        with TestClient(second, client=proxy) as client:
            client.get("/read", headers=forwarded)

    # Assert
    assert len(records_of(caplog, "grelmicro.http.ratelimit")) == TWO_METERS


def test_the_count_that_can_be_read_answers_for_the_pair() -> None:
    """A word where a number was expected is not a budget to state."""
    # Arrange
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(
                _limiter("api", 10),
                trusted=TrustedProxies(list(PROXIES)),
                legacy_headers=True,
            ),
        ]
    )
    app = FastAPI()
    micro.install(app)

    @app.get("/read")
    async def read() -> Response:
        return JSONResponse(
            {"read": 1}, headers={"X-RateLimit-Remaining": "unlimited"}
        )

    # Act
    with TestClient(app, client=CALLER) as client:
        response = client.get("/read")

    # Assert
    assert response.headers["x-ratelimit-remaining"] == "9"


@pytest.mark.parametrize(
    ("already", "ours", "wins"),
    [
        ([(b"x-ratelimit-remaining", b"3")], {}, True),
        ([], {b"x-ratelimit-remaining": b"3"}, False),
        ([(b"x-ratelimit-remaining", b"x")], {}, True),
        (
            [(b"X-RateLimit-Remaining", b"3")],
            {b"x-ratelimit-remaining": b"5"},
            True,
        ),
        (
            [(b"x-ratelimit-remaining", b"5")],
            {b"x-ratelimit-remaining": b"3"},
            False,
        ),
        (
            [(b"x-ratelimit-remaining", b"3")],
            {b"x-ratelimit-remaining": b"3"},
            True,
        ),
    ],
    ids=[
        "only theirs",
        "only ours",
        "neither reads",
        "theirs has less",
        "ours has less",
        "a tie keeps theirs",
    ],
)
def test_the_side_that_can_be_read_answers(
    already: list[tuple[bytes, bytes]],
    ours: dict[bytes, bytes],
    wins: bool,  # noqa: FBT001
) -> None:
    """One number has to answer for both, and it has to be a number."""
    # Act / Assert
    assert _has_less_left(already, ours) is wins


def test_a_quota_reconfigured_under_the_cost_lets_the_caller_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A number that changed must not take the service down with it."""
    # Arrange
    limiter = _limiter("api", 10)
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(
                limiter, trusted=TrustedProxies(list(PROXIES)), cost=5
            ),
        ]
    )
    app = FastAPI()
    micro.install(app)

    @app.get("/read")
    async def read() -> dict[str, int]:
        return {"read": 1}

    # Act
    with (
        caplog.at_level(logging.WARNING, logger="grelmicro.http.ratelimit"),
        TestClient(app, client=CALLER) as client,
    ):
        client.get("/read")
        # The quota shrinks under the cost, after the middleware was built
        # with a cost its limiter could serve.
        limiter._state = replace(
            limiter._state, config=SlidingWindowConfig(limit=1, window=WINDOW)
        )
        response = client.get("/read")

    # Assert
    assert response.status_code == HTTP_200_OK
    assert "reconfigured" in caplog.text
    assert response.headers["ratelimit"] == '"api";r=0;t=0'
    assert response.headers["ratelimit-policy"] == '"api";q=1;w=60'


# --- What the component exposes ---


def test_the_limiters_it_spends_are_readable() -> None:
    """An operator asking what meters this app reaches them here."""
    # Arrange
    burst = _limiter("burst", BURST)

    # Act
    component = RateLimitedRequests(
        burst, trusted=TrustedProxies(list(PROXIES))
    )

    # Assert
    assert component.limiters == (burst,)
    assert component.name == "default"


@pytest.mark.parametrize(
    ("options", "variable", "env_load"),
    [
        ({"env_prefix": "GREL_EDGE_"}, "GREL_EDGE_COST", "true"),
        ({"name": "edge"}, "GREL_RATE_LIMITED_REQUESTS_COST", "true"),
        ({"env_load": True}, "GREL_RATE_LIMITED_REQUESTS_COST", "0"),
    ],
    ids=["own prefix", "kind prefix", "env_load"],
)
def test_the_cost_is_read_from_the_environment(
    options: dict[str, Any],
    variable: str,
    env_load: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator prices a request without a code change."""
    # Arrange
    monkeypatch.setenv("GREL_ENV_LOAD", env_load)
    monkeypatch.setenv(variable, "3")

    # Act
    component = RateLimitedRequests(
        _limiter("api", 10), trusted=TrustedProxies(list(PROXIES)), **options
    )

    # Assert
    assert component.config.cost == COST


def test_a_component_from_config_takes_every_option() -> None:
    """The declarative door builds what the keyword door builds."""
    # Arrange
    flood = _limiter("flood", 10)
    component = RateLimitedRequests.from_config(
        RateLimitedRequestsConfig(),
        _limiter("api", 1),
        flood=flood,
        key=lambda _scope: "one",
    )
    app = FastAPI()
    Grelmicro(uses=[ErrorResponses(), component]).install(app)

    @app.get("/read")
    async def read() -> dict[str, int]:
        return {"read": 1}

    # Act
    with TestClient(app, client=CALLER) as client:
        client.get("/read")
    with TestClient(app, client=OTHER_CALLER) as other:
        shared = other.get("/read")
    schema = component._document_openapi(
        {"paths": {"/read": {"get": {"responses": {}}}}}
    )

    # Assert
    assert component.flood is flood
    assert shared.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert '"api";r=0' in shared.headers["ratelimit"]
    assert "429" in schema["paths"]["/read"]["get"]["responses"]


async def test_a_reconfigured_cost_reaches_the_middleware() -> None:
    """A file that reprices a request is read by the next request."""
    # Arrange
    component = RateLimitedRequests(
        _limiter("api", 10), trusted=TrustedProxies(list(PROXIES))
    )
    app = FastAPI()
    Grelmicro(uses=[ErrorResponses(), component]).install(app)

    @app.get("/read")
    async def read() -> dict[str, int]:
        return {"read": 1}

    transport = httpx.ASGITransport(app=app, client=CALLER)

    # Act
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        await client.get("/read")
        await component.reconfigure(RateLimitedRequestsConfig(cost=COST))
        response = await client.get("/read")

    # Assert
    assert re.fullmatch(
        rf'"api";r={10 - 1 - COST};t=\d+', response.headers["ratelimit"]
    )


def test_the_documented_refusal_follows_the_registered_format() -> None:
    """The schema publishes the body the app actually answers with."""
    # Arrange
    app = _app(_limiter("api", 10), errors=ErrorResponses.tmf())

    # Act
    responses = app.openapi()["paths"]["/read"]["get"]["responses"]

    # Assert
    assert responses["429"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("TMFError")


async def test_a_burst_that_spends_the_quota_is_told_the_window() -> None:
    """A same-instant burst learns the quota comes back after the window."""
    # Arrange
    limit = 10
    window = 1
    limiter = _limiter("api", limit, window)
    app = _app(limiter, legacy_headers=True)
    transport = httpx.ASGITransport(app=app, client=CALLER)

    # Act
    async with (
        VirtualClock(start=1000.0),
        httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client,
    ):
        for _ in range(limit):
            response = await client.get("/read")

    # Assert
    assert response.status_code == HTTP_200_OK
    assert response.headers["ratelimit"] == '"api";r=0;t=1'
    assert response.headers["x-ratelimit-reset"] == "1"


async def test_a_refusal_states_the_same_wait_in_every_header() -> None:
    """`Retry-After` and the quota reset state the same whole seconds."""
    # Arrange
    window = 3
    app = _app(_limiter("api", 1, window), legacy_headers=True)
    transport = httpx.ASGITransport(app=app, client=CALLER)

    # Act
    async with (
        VirtualClock(start=1000.0) as clock,
        httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client,
    ):
        await client.get("/read")
        await clock.advance(0.9997)
        response = await client.get("/read")

    # Assert
    assert response.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert response.headers["retry-after"] == "2"
    assert response.headers["ratelimit"] == '"api";r=0;t=2'
    assert response.headers["x-ratelimit-reset"] == "2"


async def test_a_refusal_never_states_the_quota_is_back() -> None:
    """A wait under a millisecond still states a whole second."""
    # Arrange
    bucket = RateLimiter.token_bucket(
        "api", capacity=1, refill_rate=1.0, backend=MemoryRateLimiterAdapter()
    )
    app = _app(bucket, legacy_headers=True)
    transport = httpx.ASGITransport(app=app, client=CALLER)

    # Act
    async with (
        VirtualClock(start=1000.0) as clock,
        httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client,
    ):
        await client.get("/read")
        await clock.advance(0.9996)
        response = await client.get("/read")

    # Assert
    assert response.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert response.headers["ratelimit"] == '"api";r=0;t=1'
    assert response.headers["x-ratelimit-reset"] == "1"
