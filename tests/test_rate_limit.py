"""Tests for rate limit decisions at the HTTP edge."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from grelmicro import Grelmicro
from grelmicro.http import (
    ErrorResponses,
    RateLimitedRequests,
    RateLimitMiddleware,
)
from grelmicro.integrations.fastapi import RateLimited
from grelmicro.resilience import RateLimiter
from grelmicro.resilience.errors import RateLimitExceededError
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter
from grelmicro.security import ClientAddressMiddleware, TrustedProxies

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


def _limiter(name: str, limit: int, window: float = WINDOW) -> RateLimiter:
    """Return a limiter over a backend of its own."""
    return RateLimiter.sliding_window(
        name, limit=limit, window=window, backend=MemoryRateLimiterAdapter()
    )


def _app(
    *limiters: RateLimiter,
    exclude: tuple[str, ...] = (),
    max_wait: float = 0.0,
    legacy_headers: bool = False,
) -> FastAPI:
    """Return an app metering every request through the component."""
    micro = Grelmicro(
        uses=[
            ErrorResponses(),
            RateLimitedRequests(
                *limiters,
                trusted=TrustedProxies(list(PROXIES)),
                exclude=exclude,
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


# --- Who is metered ---


def test_two_callers_are_two_buckets() -> None:
    """One caller spending its budget must not spend everybody's."""
    # Arrange
    app = _app(_limiter("api", 1))

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
    assert len(caplog.records) == 1


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


# --- What is refused where it is written ---


def test_a_middleware_that_meters_nothing_is_refused() -> None:
    """It would let every request through while reporting a limit."""
    # Act / Assert
    with pytest.raises(TypeError, match="at least one limiter"):
        RateLimitedRequests(trusted=TrustedProxies(list(PROXIES)))


def test_a_middleware_with_no_caller_to_meter_is_refused() -> None:
    """The only key left would be the ingress rather than the caller."""
    # Act / Assert
    with pytest.raises(TypeError, match="trusted= to resolve the caller"):
        RateLimitedRequests(_limiter("api", 1))


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
    assert response.headers["x-ratelimit-reset"] == "1"


def test_a_window_is_never_published_shorter_than_it_is() -> None:
    """Published short, it invites a pace the limiter refuses."""
    # Arrange
    app = _app(_limiter("api", 10, window=1.4))
    client = TestClient(app, client=CALLER)

    # Act
    with client:
        response = client.get("/read")

    # Assert
    assert response.headers["ratelimit-policy"] == '"api";q=10;w=2'


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
    assert len(caplog.records) == 1


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
    app = _app(_limiter("api", 100, window=0.5))
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
