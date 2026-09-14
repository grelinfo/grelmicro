"""Tests for authenticating requests at the HTTP edge.

Tokens are signed by the suite's own signer, so a test can build the forged
and misdirected tokens a caller would send. The verifier itself is covered
in `tests/security`, so these hold what the middleware adds: where the
credential is read, what a refused caller is told, and where the middleware
sits among the others.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Annotated, Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, Security
from fastapi import Request as FastAPIRequest
from fastapi import WebSocket as FastAPIWebSocket
from fastapi.security import HTTPBearer
from fastapi.testclient import TestClient
from litestar import Litestar, asgi, delete, get, post, websocket
from litestar import Request as LitestarRequest
from litestar import WebSocket as LitestarWebSocket
from litestar.exceptions import (
    WebSocketDisconnect as LitestarWebSocketDisconnect,
)
from litestar.middleware import DefineMiddleware
from litestar.params import Parameter
from litestar.testing import TestClient as LitestarTestClient
from starlette.applications import Starlette
from starlette.convertors import (  # codespell:ignore
    CONVERTOR_TYPES,
    Convertor,  # codespell:ignore
    register_url_convertor,
)
from starlette.endpoints import HTTPEndpoint
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.status import WS_1008_POLICY_VIOLATION
from starlette.testclient import WebSocketDenialResponse

from grelmicro import ComponentAlreadyRegisteredError, Grelmicro
from grelmicro._describe import _Endpoint, _reads_idempotent
from grelmicro._paths import walk_routes
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.http import (
    AuthenticatedRequests,
    AuthenticatedRequestsConfig,
    AuthenticatedRequestsMiddleware,
    CachedResponses,
    ErrorResponses,
    RateLimitedRequests,
)
from grelmicro.http._idempotency import _has_dependencies
from grelmicro.integrations.fastapi import (
    Anonymous,
    Authenticated,
    CachedResponse,
    Claims,
    CurrentPrincipal,
    document_authenticated_requests,
)
from grelmicro.integrations.litestar import Anonymous as LitestarAnonymous
from grelmicro.integrations.litestar import (
    Authenticated as LitestarAuthenticated,
)
from grelmicro.resilience import RateLimiter
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter
from grelmicro.security import (
    ClientBans,
    JWTClaims,
    JWTKey,
    JWTVerifier,
    Principal,
    SigningKeysUnavailableError,
    TokenRejectedError,
    TokenRejectedReason,
    TrustedProxies,
)
from tests.security.jwt_signing import Signer

if TYPE_CHECKING:
    from collections.abc import MutableMapping

    from starlette.requests import Request
    from starlette.websockets import WebSocket

pytestmark = [pytest.mark.timeout(5)]

AUDIENCE = "orders-api"
URL = "https://auth.grel.info/.well-known/jwks.json"
HOUR = 3600
HTTP_200_OK = 200
HTTP_400_BAD_REQUEST = 400
HTTP_403_FORBIDDEN = 403
HTTP_500_INTERNAL_SERVER_ERROR = 500
HTTP_401_UNAUTHORIZED = 401
HTTP_429_TOO_MANY_REQUESTS = 429
HTTP_503_SERVICE_UNAVAILABLE = 503
POLICY_VIOLATION = 1008
CALLER = ("203.0.113.7", 5000)
PROXIES = ["10.0.0.0/8"]

SIGNER = Signer()
FORGER = Signer()


def token(signer: Signer = SIGNER, **claims: Any) -> str:  # noqa: ANN401
    """Return a token `signer` signed for the suite's audience."""
    now = int(time.time())
    payload = {"sub": "user-1", "aud": AUDIENCE, "exp": now + HOUR, "iat": now}
    payload.update(claims)
    return signer.token(payload, algorithm="RS256", header={"kid": "k1"})


def bearer(value: str) -> dict[str, str]:
    """Return the header carrying `value` as a bearer token."""
    return {"authorization": f"Bearer {value}"}


def verifier() -> JWTVerifier:
    """Return a verifier holding the suite signer's public key."""
    return JWTVerifier.keys(
        JWTKey.pem(SIGNER.public_pem("RS256"), algorithm="RS256", kid="k1"),
        audience=AUDIENCE,
    )


def document() -> bytes:
    """Return the JWKS document the suite signer publishes."""
    return json.dumps({"keys": [SIGNER.public_jwk("RS256", kid="k1")]}).encode()


class Endpoint:
    """A fetcher serving whatever the test put in it."""

    def __init__(self, body: bytes | Exception) -> None:
        """Serve `body`."""
        self.body = body

    async def __call__(
        self,
        url: str,  # noqa: ARG002
        *,
        timeout: float,  # noqa: ARG002, ASYNC109
        max_bytes: int,  # noqa: ARG002
    ) -> bytes:
        """Serve the current body."""
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class Rotating:
    """A verifier that learns the new key only once it refreshes."""

    def __init__(self, *, finds: bool | Exception) -> None:
        """Answer `unknown-key` until a refresh that `finds` the key."""
        self._real = verifier()
        self._finds = finds
        self.known = False
        self.refreshes = 0

    def verify(self, token: str) -> JWTClaims:
        """Refuse the token until the refresh loaded its key."""
        if not self.known:
            raise TokenRejectedError(TokenRejectedReason.UNKNOWN_KEY)
        return self._real.verify(token)

    def verify_header(self, header: str | None) -> JWTClaims:
        """Unused by the middleware, which reads the header itself."""
        raise NotImplementedError  # pragma: no cover

    async def refresh(self, *, force: bool = False) -> bool:  # noqa: ARG002
        """Load the new key, or fail the way the test asked."""
        self.refreshes += 1
        if isinstance(self._finds, Exception):
            raise self._finds
        self.known = self._finds
        return self._finds


async def whoami(request: Request) -> JSONResponse:
    """Answer with the caller the middleware put in the scope."""
    return JSONResponse(
        {
            "subject": request.user.subject,
            "scopes": sorted(request.auth.scopes),
        }
    )


async def livez(request: Request) -> JSONResponse:  # noqa: ARG001
    """Answer a probe that carries no credential."""
    return JSONResponse({"live": True})


async def greet(websocket: WebSocket) -> None:
    """Accept, name the caller, and close."""
    await websocket.accept()
    await websocket.send_json({"subject": websocket.user.subject})
    await websocket.close()


def app_with(*uses: Any) -> Starlette:  # noqa: ANN401
    """Return an app answering through the given components."""
    app = Starlette(
        routes=[
            Route("/whoami", whoami),
            Route("/livez", livez),
            WebSocketRoute("/ws", greet),
        ]
    )
    micro = Grelmicro(uses=[ErrorResponses(), *uses])
    micro.install(app)
    app.state.micro = micro
    return app


class TestCredential:
    """Where the credential is read, and what a refused caller is told."""

    def test_a_valid_token_reaches_the_handler_as_the_caller(self) -> None:
        """`request.user` and `request.auth` carry the verified claims."""
        client = TestClient(app_with(AuthenticatedRequests(verifier())))

        response = client.get(
            "/whoami", headers=bearer(token(scope="orders:read"))
        )

        assert response.status_code == HTTP_200_OK
        assert response.json() == {
            "subject": "user-1",
            "scopes": ["orders:read"],
        }

    def test_no_credential_is_asked_for_with_a_challenge(self) -> None:
        """RFC 6750 answers a missing token with a bare `Bearer`."""
        client = TestClient(app_with(AuthenticatedRequests(verifier())))

        response = client.get("/whoami")

        assert response.status_code == HTTP_401_UNAUTHORIZED
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json()["type"].endswith("#authentication-required")

    def test_another_scheme_is_asked_for_a_bearer_token(self) -> None:
        """A `Basic` credential is not a bearer token, so none was sent."""
        client = TestClient(app_with(AuthenticatedRequests(verifier())))

        response = client.get(
            "/whoami", headers={"authorization": "Basic dXNlcjpwYXNz"}
        )

        assert response.status_code == HTTP_401_UNAUTHORIZED
        assert response.headers["www-authenticate"] == "Bearer"

    def test_the_scheme_is_read_without_regard_to_case(self) -> None:
        """RFC 7235 makes the scheme name case-insensitive."""
        client = TestClient(app_with(AuthenticatedRequests(verifier())))

        response = client.get(
            "/whoami", headers={"authorization": f"bearer {token()}"}
        )

        assert response.status_code == HTTP_200_OK

    def test_a_rejected_token_says_why(self) -> None:
        """The reason is the stable tag, and the challenge names the error."""
        client = TestClient(app_with(AuthenticatedRequests(verifier())))

        response = client.get(
            "/whoami", headers=bearer(token(aud="another-api"))
        )

        assert response.status_code == HTTP_401_UNAUTHORIZED
        assert response.json()["reason"] == "audience"
        assert response.headers["www-authenticate"] == (
            'Bearer error="invalid_token"'
        )

    def test_two_credentials_are_refused(self) -> None:
        """Choosing which of two to believe is not the server's call."""
        client = TestClient(app_with(AuthenticatedRequests(verifier())))

        response = client.get(
            "/whoami",
            headers=[
                ("authorization", f"Bearer {token()}"),
                ("authorization", f"Bearer {token()}"),
            ],
        )

        assert response.status_code == HTTP_400_BAD_REQUEST
        assert response.headers["www-authenticate"] == (
            'Bearer error="invalid_request"'
        )

    def test_an_excluded_path_needs_no_credential(self) -> None:
        """A probe is served without a token, and nothing else is."""
        client = TestClient(
            app_with(AuthenticatedRequests(verifier(), exclude=("/livez",)))
        )

        assert client.get("/livez").status_code == HTTP_200_OK
        assert client.get("/whoami").status_code == HTTP_401_UNAUTHORIZED


class TestKeys:
    """What a request is told while the keys are rotating or missing."""

    def test_a_token_naming_a_new_key_waits_for_one_refresh(self) -> None:
        """A rotation is picked up by the request that first names it."""
        rotating = Rotating(finds=True)
        client = TestClient(app_with(AuthenticatedRequests(rotating)))

        response = client.get("/whoami", headers=bearer(token()))

        assert response.status_code == HTTP_200_OK
        assert rotating.refreshes == 1

    def test_a_refresh_that_finds_no_new_key_leaves_the_token_refused(
        self,
    ) -> None:
        """An invented `kid` is refused once the refresh has nothing."""
        rotating = Rotating(finds=False)
        client = TestClient(app_with(AuthenticatedRequests(rotating)))

        response = client.get("/whoami", headers=bearer(token()))

        assert response.status_code == HTTP_401_UNAUTHORIZED
        assert response.json()["reason"] == "unknown-key"

    def test_a_refresh_that_fails_leaves_the_token_refused(self) -> None:
        """An unreachable provider is not a reason to answer `503` here."""
        rotating = Rotating(finds=SigningKeysUnavailableError("down"))
        client = TestClient(app_with(AuthenticatedRequests(rotating)))

        response = client.get("/whoami", headers=bearer(token()))

        assert response.status_code == HTTP_401_UNAUTHORIZED
        assert response.json()["reason"] == "unknown-key"

    def test_a_verifier_that_cannot_refresh_leaves_the_token_refused(
        self,
    ) -> None:
        """Keys held in code have nothing to fetch, so the refusal stands."""

        class Fixed:
            """A verifier with no published keys to refresh."""

            def verify(self, token: str) -> JWTClaims:  # noqa: ARG002
                raise TokenRejectedError(TokenRejectedReason.UNKNOWN_KEY)

            def verify_header(self, header: str | None) -> JWTClaims:
                raise NotImplementedError  # pragma: no cover

        client = TestClient(app_with(AuthenticatedRequests(Fixed())))

        response = client.get("/whoami", headers=bearer(token()))

        assert response.status_code == HTTP_401_UNAUTHORIZED
        assert response.json()["reason"] == "unknown-key"

    def test_a_verifier_that_opens_nothing_is_served_as_it_is(self) -> None:
        """A verifier with no lifecycle of its own needs none from the app."""
        rotating = Rotating(finds=True)

        with TestClient(app_with(AuthenticatedRequests(rotating))) as client:
            response = client.get("/whoami", headers=bearer(token()))

        assert response.status_code == HTTP_200_OK

    def test_the_component_opens_the_verifier_with_the_app(self) -> None:
        """Published keys load at startup, before the first request."""
        published = JWTVerifier.jwks(
            URL, audience=AUDIENCE, fetch=Endpoint(document())
        )
        app = app_with(AuthenticatedRequests(published))

        with TestClient(app) as client:
            response = client.get("/whoami", headers=bearer(token()))

        assert response.status_code == HTTP_200_OK

    def test_keys_that_never_loaded_answer_503(self) -> None:
        """A service that cannot check a token refuses every one of them."""
        unreachable = JWTVerifier.jwks(
            URL,
            audience=AUDIENCE,
            fetch=Endpoint(SigningKeysUnavailableError("provider is down")),
        )
        app = app_with(AuthenticatedRequests(unreachable))

        with TestClient(app) as client:
            response = client.get("/whoami", headers=bearer(token()))

        assert response.status_code == HTTP_503_SERVICE_UNAVAILABLE
        assert response.json()["type"].endswith("#signing-keys-unavailable")


class TestBans:
    """Refusing a caller that keeps forging, without refusing the wrong one."""

    def test_a_forging_caller_is_banned(self) -> None:
        """The ban answers before the next token is even looked at."""
        bans = ClientBans(failures=1, duration=60.0)
        app = app_with(
            AuthenticatedRequests(
                verifier(), bans=bans, trusted=TrustedProxies(PROXIES)
            )
        )
        client = TestClient(app, client=CALLER)

        forged = client.get("/whoami", headers=bearer(token(FORGER)))
        refused = client.get("/whoami", headers=bearer(token()))

        assert forged.status_code == HTTP_401_UNAUTHORIZED
        assert refused.status_code == HTTP_429_TOO_MANY_REQUESTS
        assert int(refused.headers["retry-after"]) > 0

    def test_an_expired_token_is_never_counted(self) -> None:
        """A client whose token needs refreshing is not an attacker."""
        bans = ClientBans(failures=1, duration=60.0)
        app = app_with(
            AuthenticatedRequests(
                verifier(), bans=bans, trusted=TrustedProxies(PROXIES)
            )
        )
        client = TestClient(app, client=CALLER)
        expired = token(exp=int(time.time()) - HOUR)

        first = client.get("/whoami", headers=bearer(expired))
        second = client.get("/whoami", headers=bearer(expired))

        assert first.status_code == HTTP_401_UNAUTHORIZED
        assert second.status_code == HTTP_401_UNAUTHORIZED

    def test_bans_need_the_proxies_that_name_the_caller(self) -> None:
        """A ban counted against a spoofable address refuses the wrong one."""
        with pytest.raises(TypeError, match="trusted="):
            AuthenticatedRequests(verifier(), bans=ClientBans())


class TestPlacement:
    """Authentication runs before anything of ours can answer."""

    def test_authentication_runs_before_the_rate_limit(self) -> None:
        """Registered after, it still answers first."""
        limiter = RateLimiter.sliding_window(
            "burst", limit=100, window=60, backend=MemoryRateLimiterAdapter()
        )
        app = app_with(
            RateLimitedRequests(limiter, trusted=TrustedProxies(PROXIES)),
            AuthenticatedRequests(verifier()),
        )

        response = TestClient(app, client=CALLER).get("/whoami")

        assert response.status_code == HTTP_401_UNAUTHORIZED
        assert "ratelimit" not in response.headers

    def test_litestar_is_authenticated_the_same_way(self) -> None:
        """The same component answers on Litestar."""

        @get("/whoami")
        async def whoami_on_litestar(
            request: LitestarRequest,
        ) -> dict[str, str]:
            return {"subject": request.user.subject}

        app = Litestar(route_handlers=[whoami_on_litestar])
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)

        with LitestarTestClient(app) as client:
            refused = client.get("/whoami")
            served = client.get("/whoami", headers=bearer(token()))

        assert refused.status_code == HTTP_401_UNAUTHORIZED
        assert served.json() == {"subject": "user-1"}


class TestWebSocket:
    """A handshake is authenticated like a request."""

    def test_a_handshake_without_a_token_is_denied_with_a_challenge(
        self,
    ) -> None:
        """The denial response carries the `401` and its challenge."""
        client = TestClient(app_with(AuthenticatedRequests(verifier())))

        with (
            pytest.raises(WebSocketDenialResponse) as caught,
            client.websocket_connect("/ws"),
        ):
            pass  # pragma: no cover

        assert caught.value.status_code == HTTP_401_UNAUTHORIZED
        assert caught.value.headers["www-authenticate"] == "Bearer"

    def test_an_authenticated_handshake_reaches_the_handler(self) -> None:
        """`websocket.user` is the verified caller."""
        client = TestClient(app_with(AuthenticatedRequests(verifier())))

        with client.websocket_connect("/ws", headers=bearer(token())) as ws:
            assert ws.receive_json() == {"subject": "user-1"}

    async def test_a_server_without_the_denial_extension_closes(
        self,
    ) -> None:
        """With no way to send a `401`, the handshake is never completed."""
        sent: list[MutableMapping[str, Any]] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
            raise AssertionError  # pragma: no cover

        async def receive() -> dict[str, str]:
            return {"type": "websocket.connect"}

        async def send(message: MutableMapping[str, Any]) -> None:
            sent.append(message)

        middleware = AuthenticatedRequestsMiddleware(app, verifier=verifier())
        await middleware(
            {
                "type": "websocket",
                "path": "/ws",
                "root_path": "",
                "headers": [],
            },
            receive,
            send,
        )

        assert sent == [{"type": "websocket.close", "code": POLICY_VIOLATION}]

    async def test_a_client_gone_before_the_handshake_is_left_alone(
        self,
    ) -> None:
        """Nothing is sent to a connection that already closed."""
        sent: list[MutableMapping[str, Any]] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
            raise AssertionError  # pragma: no cover

        async def receive() -> dict[str, str]:
            return {"type": "websocket.disconnect"}

        async def send(message: MutableMapping[str, Any]) -> None:
            sent.append(message)  # pragma: no cover

        middleware = AuthenticatedRequestsMiddleware(app, verifier=verifier())
        await middleware(
            {
                "type": "websocket",
                "path": "/ws",
                "root_path": "",
                "headers": [],
            },
            receive,
            send,
        )

        assert sent == []

    async def test_lifespan_passes_through(self) -> None:
        """Startup and shutdown are not requests, so nothing is checked."""
        reached: list[str] = []

        async def app(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
            reached.append(scope["type"])

        middleware = AuthenticatedRequestsMiddleware(app, verifier=verifier())
        await middleware({"type": "lifespan"}, None, None)  # ty: ignore[invalid-argument-type]

        assert reached == ["lifespan"]


class TestConstruction:
    """What the component refuses where it is written."""

    def test_exclude_written_as_one_string_is_refused(self) -> None:
        """A missing comma would otherwise exclude by single characters."""
        with pytest.raises(TypeError, match="string"):
            AuthenticatedRequests(verifier(), exclude="/livez")  # ty: ignore[invalid-argument-type]

    def test_the_component_reports_what_it_was_built_with(self) -> None:
        """What `grelmicro check` and a caller read back is what was given."""
        trusting = verifier()

        component = AuthenticatedRequests(trusting, exclude=("/livez",))

        assert component.name == "default"
        assert component.verifier is trusting
        assert component.config.exclude == ("/livez",)

    async def test_closing_a_component_that_never_opened_is_harmless(
        self,
    ) -> None:
        """An app that failed to start still shuts down cleanly."""
        component = AuthenticatedRequests(verifier())

        assert await component.__aexit__(None, None, None) is None

    def test_from_config_takes_the_config_whole(self) -> None:
        """The declarative door serves exactly what the config says."""
        component = AuthenticatedRequests.from_config(
            AuthenticatedRequestsConfig(exclude=("/livez",)), verifier()
        )
        client = TestClient(app_with(component))

        assert client.get("/livez").status_code == HTTP_200_OK
        assert client.get("/whoami").status_code == HTTP_401_UNAUTHORIZED


class Opaque:
    """A verifier whose callers are not JWTs."""

    def verify(self, token: str) -> Any:  # noqa: ANN401, ARG002
        """Accept any token as the same caller."""
        return _OpaqueCaller()

    def verify_header(self, header: str | None) -> Any:  # noqa: ANN401
        """Unused by the middleware, which reads the header itself."""
        raise NotImplementedError  # pragma: no cover


class _OpaqueCaller:
    """A caller authenticated by something other than a JWT."""

    subject = "service-7"
    issuer = None
    scopes: frozenset[str] = frozenset()
    claims: dict[str, Any] = {}  # noqa: RUF012
    is_authenticated = True


def fastapi_app(*uses: Any, declare: Any = None) -> FastAPI:  # noqa: ANN401
    """Return a FastAPI app with routes declaring how they are authenticated."""
    app = FastAPI()

    @app.get("/me")
    async def me(principal: CurrentPrincipal) -> dict[str, Any]:
        return {
            "subject": principal.subject,
            "authenticated": principal.is_authenticated,
        }

    @app.get("/claims")
    async def claims(claims: Claims) -> dict[str, Any]:
        return {"scope": claims.claims.get("scope")}

    @app.delete(
        "/orders/{order_id}",
        dependencies=[Authenticated(scopes=["orders:write"])],
    )
    async def cancel(order_id: int) -> dict[str, int]:
        return {"cancelled": order_id}

    @app.get("/catalog", dependencies=[Anonymous()])
    async def catalog(request: FastAPIRequest) -> dict[str, bool]:
        return {"authenticated": request.user.is_authenticated}

    @app.post("/catalog")
    async def add_to_catalog() -> dict[str, bool]:
        return {"added": True}

    @app.get("/feed")
    async def feed() -> dict[str, list[str]]:
        return {"feed": []}

    reports = APIRouter(dependencies=[Authenticated(scopes=["reports:read"])])

    @reports.get(
        "/reports/export",
        dependencies=[Authenticated(scopes=["reports:export"])],
    )
    async def export() -> dict[str, bool]:
        return {"exported": True}

    @reports.get(
        "/reports/daily",
        dependencies=[Authenticated(scopes=["reports:read"])],
    )
    async def daily() -> dict[str, bool]:
        return {"daily": True}

    app.include_router(reports)
    if declare is not None:
        declare(app)
    micro = Grelmicro(uses=[ErrorResponses(), *uses])
    micro.install(app)
    app.state.micro = micro
    return app


class TestFastAPI:
    """Route declarations on FastAPI."""

    def test_the_current_principal_is_the_verified_caller(self) -> None:
        """A handler reads the caller, not the token behind it."""
        client = TestClient(fastapi_app(AuthenticatedRequests(verifier())))

        response = client.get("/me", headers=bearer(token()))

        assert response.json() == {"subject": "user-1", "authenticated": True}

    def test_the_claims_are_the_verified_jwt(self) -> None:
        """`Claims` hands over the claim set, typed."""
        client = TestClient(fastapi_app(AuthenticatedRequests(verifier())))

        response = client.get(
            "/claims", headers=bearer(token(scope="orders:read"))
        )

        assert response.json() == {"scope": "orders:read"}

    def test_claims_asked_of_a_caller_that_is_no_jwt_is_a_route_error(
        self,
    ) -> None:
        """A route reading what its verifier never produces is a bug."""
        client = TestClient(fastapi_app(AuthenticatedRequests(Opaque())))

        with pytest.raises(TypeError, match="CurrentPrincipal"):
            client.get("/claims", headers=bearer("opaque"))

    def test_a_missing_scope_is_forbidden_and_named(self) -> None:
        """`403`, never `401`, with a challenge naming the scope."""
        client = TestClient(fastapi_app(AuthenticatedRequests(verifier())))

        response = client.delete(
            "/orders/7", headers=bearer(token(scope="orders:read"))
        )

        assert response.status_code == HTTP_403_FORBIDDEN
        assert response.headers["www-authenticate"] == (
            'Bearer error="insufficient_scope", scope="orders:write"'
        )

    def test_the_scope_granted_serves_the_route(self) -> None:
        """Holding every scope named is enough."""
        client = TestClient(fastapi_app(AuthenticatedRequests(verifier())))

        response = client.delete(
            "/orders/7", headers=bearer(token(scope="orders:write"))
        )

        assert response.json() == {"cancelled": 7}

    def test_a_router_and_its_route_each_apply_their_scopes(self) -> None:
        """A router's scope is required as well as the route's own."""
        client = TestClient(fastapi_app(AuthenticatedRequests(verifier())))

        refused = client.get(
            "/reports/export", headers=bearer(token(scope="reports:export"))
        )
        served = client.get(
            "/reports/export",
            headers=bearer(token(scope="reports:read reports:export")),
        )

        assert refused.status_code == HTTP_403_FORBIDDEN
        assert refused.headers["www-authenticate"] == (
            'Bearer error="insufficient_scope", scope="reports:read"'
        )
        assert served.json() == {"exported": True}

    def test_an_anonymous_route_needs_no_credential(self) -> None:
        """The route is served, and its caller is not authenticated."""
        client = TestClient(fastapi_app(AuthenticatedRequests(verifier())))

        response = client.get("/catalog")

        assert response.json() == {"authenticated": False}

    def test_anonymous_applies_to_the_method_that_declared_it(self) -> None:
        """A public read leaves the write on the same path authenticated."""
        client = TestClient(fastapi_app(AuthenticatedRequests(verifier())))

        response = client.post("/catalog")

        assert response.status_code == HTTP_401_UNAUTHORIZED

    def test_an_anonymous_route_added_after_install_counts_at_startup(
        self,
    ) -> None:
        """The routes are read again when the app starts."""
        app = fastapi_app(AuthenticatedRequests(verifier()))

        @app.get("/status", dependencies=[Anonymous()])
        async def status() -> dict[str, bool]:
            return {"up": True}

        with TestClient(app) as client:
            response = client.get("/status")

        assert response.json() == {"up": True}

    def test_a_scope_without_the_component_is_asked_for_a_credential(
        self,
    ) -> None:
        """With nothing verifying tokens, nobody is authenticated."""
        client = TestClient(fastapi_app())

        response = client.delete("/orders/7", headers=bearer(token()))

        assert response.status_code == HTTP_401_UNAUTHORIZED
        assert response.headers["www-authenticate"] == (
            'Bearer scope="orders:write"'
        )

    def test_the_current_principal_without_the_component_is_refused(
        self,
    ) -> None:
        """A route reading the caller never runs without one."""
        client = TestClient(fastapi_app())

        response = client.get("/me")

        assert response.status_code == HTTP_401_UNAUTHORIZED

    def test_scopes_written_as_one_string_are_refused(self) -> None:
        """One string would otherwise read as one scope per character."""
        with pytest.raises(TypeError, match="not a single string"):
            Authenticated(scopes="orders:write")

    async def test_a_component_opened_before_install_reads_no_routes(
        self,
    ) -> None:
        """Opening it without an app has nothing to read."""
        component = AuthenticatedRequests(verifier())

        async with component:
            assert component.verifier is not None


OAUTH_METADATA = "https://auth.grel.info/.well-known/oauth-authorization-server"
OIDC_METADATA = "https://auth.grel.info/.well-known/openid-configuration"
SCHEME = "AuthenticatedRequests"


class TestOpenAPI:
    """What the schema says about the token every covered operation needs."""

    def test_every_covered_operation_requires_the_scheme(self) -> None:
        """The bearer token is required, and its refusal is described."""
        schema = fastapi_app(AuthenticatedRequests(verifier())).openapi()

        operation = schema["paths"]["/me"]["get"]

        assert schema["components"]["securitySchemes"][SCHEME] == {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
        assert operation["security"] == [{SCHEME: []}]
        assert "401" in operation["responses"]
        assert "403" not in operation["responses"]

    def test_the_scopes_a_route_declares_are_required(self) -> None:
        """A route requiring a scope says so, and can answer `403`."""
        schema = fastapi_app(AuthenticatedRequests(verifier())).openapi()

        cancel = schema["paths"]["/orders/{order_id}"]["delete"]
        export = schema["paths"]["/reports/export"]["get"]

        assert cancel["security"] == [{SCHEME: ["orders:write"]}]
        assert "403" in cancel["responses"]
        assert export["security"] == [
            {SCHEME: ["reports:read", "reports:export"]}
        ]
        assert schema["paths"]["/reports/daily"]["get"]["security"] == [
            {SCHEME: ["reports:read"]}
        ]

    def test_public_and_excluded_operations_need_nothing(self) -> None:
        """An anonymous read and an excluded path carry no requirement."""
        schema = fastapi_app(
            AuthenticatedRequests(verifier(), exclude=("/claims",))
        ).openapi()

        assert "security" not in schema["paths"]["/catalog"]["get"]
        assert "security" not in schema["paths"]["/claims"]["get"]
        assert schema["paths"]["/catalog"]["post"]["security"] == [{SCHEME: []}]

    def test_bans_add_the_429(self) -> None:
        """A banned caller is answered `429`, so the schema says so."""
        component = AuthenticatedRequests(
            verifier(), bans=ClientBans(), trusted=TrustedProxies(PROXIES)
        )

        schema = fastapi_app(component).openapi()

        assert "429" in schema["paths"]["/me"]["get"]["responses"]

    def test_a_discovering_verifier_points_at_the_discovery_document(
        self,
    ) -> None:
        """A client can find the authorization server from the schema."""
        discovering = JWTVerifier.discover(
            "https://auth.grel.info/",
            audience=AUDIENCE,
            fetch=Endpoint(document()),
        )

        schema = fastapi_app(AuthenticatedRequests(discovering)).openapi()

        assert schema["components"]["securitySchemes"][SCHEME] == {
            "type": "openIdConnect",
            "openIdConnectUrl": OIDC_METADATA,
        }

    def test_metadata_found_only_under_rfc_8414_is_a_bearer_token(
        self,
    ) -> None:
        """No OpenID Connect document to point at, so none is named."""
        discovering = JWTVerifier.discover(
            "https://auth.grel.info/",
            audience=AUDIENCE,
            fetch=Endpoint(document()),
        )
        discovering._metadata_url = OAUTH_METADATA

        schema = fastapi_app(AuthenticatedRequests(discovering)).openapi()

        assert schema["components"]["securitySchemes"][SCHEME]["type"] == (
            "http"
        )

    def test_building_the_schema_again_adds_nothing_twice(self) -> None:
        """FastAPI hands back the schema it cached, and it stays whole."""
        app = fastapi_app(AuthenticatedRequests(verifier()))

        app.openapi()
        schema = app.openapi()

        assert schema["paths"]["/me"]["get"]["security"] == [{SCHEME: []}]

    def test_the_apps_own_security_scheme_is_kept_and_joined(self) -> None:
        """A route checking FastAPI's own scheme still needs the token too."""
        own = HTTPBearer()

        def declare(app: FastAPI) -> None:
            @app.get("/legacy", dependencies=[Depends(own)])
            async def legacy() -> dict[str, bool]:
                return {"legacy": True}

        schema = fastapi_app(
            AuthenticatedRequests(verifier()), declare=declare
        ).openapi()

        assert schema["paths"]["/legacy"]["get"]["security"] == [
            {"HTTPBearer": [], SCHEME: []}
        ]
        assert set(schema["components"]["securitySchemes"]) == {
            "HTTPBearer",
            SCHEME,
        }

    def test_openapi_false_leaves_the_schema_alone(self) -> None:
        """The component can stay out of the document."""
        schema = fastapi_app(
            AuthenticatedRequests(verifier(), openapi=False)
        ).openapi()

        assert SCHEME not in schema.get("components", {}).get(
            "securitySchemes", {}
        )

    def test_documenting_an_app_without_the_middleware_is_refused(
        self,
    ) -> None:
        """There is nothing to describe on an app nothing authenticates."""
        with pytest.raises(TypeError, match="AuthenticatedRequestsMiddleware"):
            document_authenticated_requests(FastAPI())


def litestar_app(*uses: Any) -> Litestar:  # noqa: ANN401
    """Return a Litestar app with handlers declaring how they are authenticated."""

    @get("/catalog/{item_id:int}", opt=LitestarAnonymous())
    async def read_item(item_id: Annotated[int, Parameter()]) -> dict[str, int]:
        return {"item": item_id}

    @post("/catalog/{item_id:int}")
    async def write_item(
        item_id: Annotated[int, Parameter()],
    ) -> dict[str, int]:
        return {"item": item_id}

    @get("/files/{rest:path}", opt={**LitestarAnonymous(), "tag": "files"})
    async def read_file(rest: Annotated[str, Parameter()]) -> dict[str, str]:
        return {"file": rest}

    @get("/status", opt=LitestarAnonymous())
    async def status() -> dict[str, bool]:
        return {"up": True}

    @delete(
        "/orders/{order_id:int}",
        guards=[LitestarAuthenticated(scopes=["orders:write"])],
        status_code=HTTP_200_OK,
    )
    async def cancel(order_id: Annotated[int, Parameter()]) -> dict[str, int]:
        return {"cancelled": order_id}

    app = Litestar(
        route_handlers=[read_item, write_item, read_file, status, cancel]
    )
    micro = Grelmicro(uses=[ErrorResponses(), *uses])
    micro.install(app)
    app.state.micro = micro
    return app


class TestLitestar:
    """Handler declarations on Litestar."""

    def test_an_anonymous_handler_needs_no_credential(self) -> None:
        """A public read is served, typed path parameter and all."""
        with LitestarTestClient(
            litestar_app(AuthenticatedRequests(verifier()))
        ) as client:
            response = client.get("/catalog/7")

        assert response.json() == {"item": 7}

    def test_anonymous_applies_to_the_method_that_declared_it(self) -> None:
        """The write on the same path stays authenticated."""
        with LitestarTestClient(
            litestar_app(AuthenticatedRequests(verifier()))
        ) as client:
            response = client.post("/catalog/7")

        assert response.status_code == HTTP_401_UNAUTHORIZED

    def test_a_public_path_parameter_spans_slashes(self) -> None:
        """A `path` parameter matches a nested path, merged options and all."""
        with LitestarTestClient(
            litestar_app(AuthenticatedRequests(verifier()))
        ) as client:
            nested = client.get("/files/reports/2026/q3.csv")
            static = client.get("/status")

        assert nested.status_code == HTTP_200_OK
        assert static.json() == {"up": True}

    def test_a_missing_scope_is_forbidden_and_named(self) -> None:
        """The guard answers `403` with the challenge naming the scope."""
        with LitestarTestClient(
            litestar_app(AuthenticatedRequests(verifier()))
        ) as client:
            refused = client.delete(
                "/orders/7", headers=bearer(token(scope="orders:read"))
            )
            served = client.delete(
                "/orders/7", headers=bearer(token(scope="orders:write"))
            )

        assert refused.status_code == HTTP_403_FORBIDDEN
        assert refused.headers["www-authenticate"] == (
            'Bearer error="insufficient_scope", scope="orders:write"'
        )
        assert served.json() == {"cancelled": 7}

    def test_the_guard_without_the_component_asks_for_a_credential(
        self,
    ) -> None:
        """With nothing verifying tokens, nobody is authenticated."""
        with LitestarTestClient(litestar_app()) as client:
            response = client.delete("/orders/7", headers=bearer(token()))

        assert response.status_code == HTTP_401_UNAUTHORIZED
        assert response.headers["www-authenticate"] == (
            'Bearer scope="orders:write"'
        )

    def test_authentication_runs_before_the_rate_limit_on_litestar(
        self,
    ) -> None:
        """Registered after, it still answers first."""
        limiter = RateLimiter.sliding_window(
            "burst", limit=100, window=60, backend=MemoryRateLimiterAdapter()
        )
        app = litestar_app(
            RateLimitedRequests(limiter, trusted=TrustedProxies(PROXIES)),
            AuthenticatedRequests(verifier()),
        )

        # Litestar types its scope more narrowly than Starlette's client does.
        with TestClient(app, client=CALLER) as client:  # ty: ignore[invalid-argument-type]
            response = client.delete("/orders/7")

        assert response.status_code == HTTP_401_UNAUTHORIZED
        assert "ratelimit" not in response.headers

    def test_scopes_written_as_one_string_are_refused(self) -> None:
        """One string would otherwise read as one scope per character."""
        with pytest.raises(TypeError, match="not a single string"):
            LitestarAuthenticated(scopes="orders:write")


class TestReport:
    """What `grelmicro check` says about each endpoint."""

    @staticmethod
    def applies(app: Any, method: str, path: str) -> tuple[str, ...]:  # noqa: ANN401
        """Return what the report says applies to one endpoint."""
        report = app.state.micro.describe(app)
        return next(
            row.applies
            for row in report.endpoints
            if row.method == method and row.path == path
        )

    def test_each_fastapi_endpoint_says_how_it_is_authenticated(self) -> None:
        """Authenticated with its scopes, anonymous, or left alone."""
        app = fastapi_app(
            Cache(MemoryCacheAdapter()),
            CachedResponses(include=("/feed",)),
            AuthenticatedRequests(verifier(), exclude=("/claims",)),
        )

        assert self.applies(app, "GET", "/me") == ("authenticated",)
        assert self.applies(app, "DELETE", "/orders/{order_id}") == (
            "authenticated orders:write",
        )
        assert self.applies(app, "GET", "/reports/export") == (
            "authenticated reports:read reports:export",
        )
        assert self.applies(app, "GET", "/catalog") == ("anonymous",)
        assert self.applies(app, "GET", "/claims") == ()

    def test_an_authenticated_read_is_never_reported_as_cached(self) -> None:
        """The cache answers an authenticated request from its handler."""
        app = fastapi_app(
            Cache(MemoryCacheAdapter()),
            CachedResponses(include=("/feed",)),
            AuthenticatedRequests(verifier()),
        )

        assert self.applies(app, "GET", "/feed") == ("authenticated",)

    def test_each_litestar_endpoint_says_how_it_is_authenticated(self) -> None:
        """A guard's scopes and a public handler read the same way."""
        app = litestar_app(AuthenticatedRequests(verifier()))

        assert self.applies(app, "GET", "/catalog/{item_id:int}") == (
            "anonymous",
        )
        assert self.applies(app, "POST", "/catalog/{item_id:int}") == (
            "authenticated",
        )
        assert self.applies(app, "DELETE", "/orders/{order_id:int}") == (
            "authenticated orders:write",
        )

    def test_an_authenticated_endpoint_reports_no_idempotent_replay(
        self,
    ) -> None:
        """A replay is skipped for a request that carries a caller."""
        idempotent = SimpleNamespace(
            config=SimpleNamespace(methods=("POST",), include=(), exclude=()),
            _key_maker=None,
            route_is_gated=lambda method, path: False,  # noqa: ARG005
            idempotency=SimpleNamespace(config=SimpleNamespace(ttl=3600.0)),
        )
        read = _reads_idempotent(idempotent)
        public = _Endpoint(
            method="POST", path="/signup", route=None, contexts=()
        )

        assert read(public) == "idempotent 3600s"
        assert read(replace(public, authenticated=True)) is None


class TestDeclarationsElsewhere:
    """`Anonymous()` computes nothing, so nothing else treats it as a gate."""

    def test_an_anonymous_route_is_still_served_from_the_cache(self) -> None:
        """A public read declared cacheable is answered from the store."""
        calls: list[int] = []

        def declare(app: FastAPI) -> None:
            @app.get(
                "/prices", dependencies=[Anonymous(), CachedResponse(ttl=60)]
            )
            async def prices() -> dict[str, int]:
                calls.append(1)
                return {"price": len(calls)}

        app = fastapi_app(
            Cache(MemoryCacheAdapter()),
            CachedResponses(),
            AuthenticatedRequests(verifier()),
            declare=declare,
        )

        with TestClient(app) as client:
            first = client.get("/prices")
            second = client.get("/prices")

        assert first.json() == second.json() == {"price": 1}
        assert len(calls) == 1

    def test_an_anonymous_route_gates_no_idempotent_replay(self) -> None:
        """Declared on the route or on its router, it is not a dependency."""
        app = FastAPI()

        @app.post("/signup", dependencies=[Anonymous()])
        async def signup() -> dict[str, bool]:
            return {"ok": True}

        public = APIRouter(dependencies=[Anonymous()])

        @public.post("/newsletter")
        async def newsletter() -> dict[str, bool]:
            return {"ok": True}

        app.include_router(public)
        routes = {
            route.path: (route, contexts)
            for _, route, contexts in walk_routes(app)
        }

        assert not _has_dependencies(*routes["/signup"])
        assert not _has_dependencies(*routes["/newsletter"])


class TestRouting:
    """Public means the route the router dispatches to, never a pattern overlap."""

    def test_a_public_route_never_unlocks_a_protected_overlapping_one(
        self,
    ) -> None:
        """`/users/me` is answered by its own route, which stays protected."""
        app = FastAPI()

        @app.get("/users/me")
        async def me(principal: CurrentPrincipal) -> dict[str, str | None]:
            return {"subject": principal.subject}

        @app.get("/users/{user_id}", dependencies=[Anonymous()])
        async def user(user_id: str) -> dict[str, str]:
            return {"user": user_id}

        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)
        client = TestClient(app)

        assert client.get("/users/me").status_code == HTTP_401_UNAUTHORIZED
        assert client.get("/users/me", headers=bearer(token())).json() == {
            "subject": "user-1"
        }
        assert client.get("/users/7").json() == {"user": "7"}

    def test_a_public_websocket_leaves_the_same_path_protected_over_http(
        self,
    ) -> None:
        """Declaring a socket public says nothing about the HTTP route."""
        app = FastAPI()

        @app.get("/feed")
        async def feed() -> dict[str, bool]:
            return {"feed": True}

        @app.websocket("/feed", dependencies=[Anonymous()])
        async def feed_socket(socket: FastAPIWebSocket) -> None:
            await socket.accept()
            await socket.send_json({"public": True})
            await socket.close()

        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)
        client = TestClient(app)

        assert client.get("/feed").status_code == HTTP_401_UNAUTHORIZED
        with client.websocket_connect("/feed") as socket:
            assert socket.receive_json() == {"public": True}

    def test_a_public_litestar_route_never_unlocks_a_fixed_segment(
        self,
    ) -> None:
        """Litestar prefers `/users/me`, so that route decides."""

        @get("/users/me")
        async def me(request: LitestarRequest) -> dict[str, str]:
            return {"subject": request.user.subject}

        @get("/users/{user_id:int}", opt=LitestarAnonymous())
        async def user(user_id: Annotated[int, Parameter()]) -> dict[str, int]:
            return {"user": user_id}

        app = Litestar(route_handlers=[me, user])
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)

        with LitestarTestClient(app) as client:
            refused = client.get("/users/me")
            served = client.get("/users/me", headers=bearer(token()))
            public = client.get("/users/7")

        assert refused.status_code == HTTP_401_UNAUTHORIZED
        assert served.json() == {"subject": "user-1"}
        assert public.json() == {"user": 7}

    def test_a_public_litestar_websocket_needs_no_credential(self) -> None:
        """`opt=Anonymous()` on a websocket handler serves the handshake."""

        @websocket("/live", opt=LitestarAnonymous())
        async def live(socket: LitestarWebSocket) -> None:
            await socket.accept()
            await socket.send_json({"public": True})
            await socket.close()

        app = Litestar(route_handlers=[live])
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)

        with (
            LitestarTestClient(app) as client,
            client.websocket_connect("/live") as socket,
        ):
            assert socket.receive_json() == {"public": True}

    def test_a_public_catch_all_never_opens_a_mounted_app(self) -> None:
        """A mounted application answers under its path, public route or not."""
        app = FastAPI()

        async def admin(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
            await JSONResponse({"admin": True})(scope, receive, send)

        app.mount("/admin", admin)

        @app.get("/{rest:path}", dependencies=[Anonymous()])
        async def page(rest: str) -> dict[str, str]:
            return {"page": rest}

        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)
        client = TestClient(app)

        assert client.get("/admin/users").status_code == HTTP_401_UNAUTHORIZED
        assert client.get("/about").json() == {"page": "about"}

    def test_a_public_route_another_route_also_answers_stays_authenticated(
        self,
    ) -> None:
        """Declaration order never decides whether a credential is needed."""
        app = FastAPI()

        @app.get("/users/me", dependencies=[Anonymous()])
        async def me() -> dict[str, bool]:
            return {"public": True}

        @app.get("/users/{user_id}")
        async def user(user_id: str) -> dict[str, str]:
            return {"user": user_id}

        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)
        client = TestClient(app)

        assert client.get("/users/me").status_code == HTTP_401_UNAUTHORIZED
        assert client.get("/users/me", headers=bearer(token())).json() == {
            "public": True
        }

    def test_a_router_included_as_public_needs_no_credential(self) -> None:
        """`include_router(dependencies=[Anonymous()])` counts for its routes."""
        app = FastAPI()
        help_pages = APIRouter()

        @help_pages.get("/help")
        async def help_page() -> dict[str, bool]:
            return {"help": True}

        app.include_router(help_pages, dependencies=[Anonymous()])
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)

        assert TestClient(app).get("/help").json() == {"help": True}

    def test_a_public_route_in_a_mounted_app_needs_no_credential(self) -> None:
        """A sub-application's routes are read, and its public one is served."""
        sub = FastAPI()

        @sub.get("/status", dependencies=[Anonymous()])
        async def status() -> dict[str, bool]:
            return {"up": True}

        app = FastAPI()
        app.mount("/sub", sub)
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)

        assert TestClient(app).get("/sub/status").json() == {"up": True}

    def test_a_url_litestar_would_refuse_is_never_served_publicly(self) -> None:
        """A path that fits the pattern and not the route stays authenticated."""
        with LitestarTestClient(
            litestar_app(AuthenticatedRequests(verifier()))
        ) as client:
            response = client.get("/catalog/not-a-number")

        assert response.status_code == HTTP_401_UNAUTHORIZED

    def test_a_public_litestar_url_is_served_however_litestar_spells_it(
        self,
    ) -> None:
        """A trailing slash reaches the public handler Litestar routes it to."""
        with LitestarTestClient(
            litestar_app(AuthenticatedRequests(verifier()))
        ) as client:
            response = client.get("/status/")

        assert response.json() == {"up": True}

    def test_a_public_litestar_mount_serves_every_path_under_it(self) -> None:
        """`opt=Anonymous()` on a mounted ASGI handler covers what it answers."""

        @asgi(
            "/assets", is_mount=True, copy_scope=False, opt=LitestarAnonymous()
        )
        async def assets(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
            await JSONResponse({"asset": True})(scope, receive, send)

        app = Litestar(route_handlers=[assets])
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)

        with LitestarTestClient(app) as client:
            response = client.get("/assets/css/site.css")

        assert response.json() == {"asset": True}

    def test_a_websocket_to_a_public_litestar_http_route_is_refused(
        self,
    ) -> None:
        """A handshake no public handler answers stays authenticated."""
        with (
            LitestarTestClient(
                litestar_app(AuthenticatedRequests(verifier()))
            ) as client,
            pytest.raises(LitestarWebSocketDisconnect) as refused,
            client.websocket_connect("/status"),
        ):
            pass  # pragma: no cover

        assert refused.value.code == WS_1008_POLICY_VIOLATION


class TestConsistency:
    """The schema and the report say what the middleware does."""

    @staticmethod
    def applies(app: Any, method: str, path: str) -> tuple[str, ...]:  # noqa: ANN401
        """Return what the report says applies to one endpoint."""
        report = app.state.micro.describe(app)
        return next(
            row.applies
            for row in report.endpoints
            if row.method == method and row.path == path
        )

    def test_an_endpoint_class_never_breaks_the_schema(self) -> None:
        """A route whose endpoint is a class declares no methods of its own."""
        app = fastapi_app(AuthenticatedRequests(verifier()))

        class Legacy(HTTPEndpoint):
            async def get(self, request: Any) -> JSONResponse:  # noqa: ANN401, ARG002
                return JSONResponse({"legacy": True})

        app.add_route("/legacy", Legacy)  # ty: ignore[invalid-argument-type]

        assert "/me" in app.openapi()["paths"]

    def test_a_route_another_route_covers_is_described_as_authenticated(
        self,
    ) -> None:
        """The schema and the report agree with the `401` it is answered."""

        def declare(app: FastAPI) -> None:
            @app.get("/items/featured", dependencies=[Anonymous()])
            async def featured() -> dict[str, bool]:
                return {"featured": True}

            @app.get("/items/{item_id}")
            async def item(item_id: str) -> dict[str, str]:
                return {"item": item_id}

        app = fastapi_app(AuthenticatedRequests(verifier()), declare=declare)

        assert app.openapi()["paths"]["/items/featured"]["get"]["security"] == [
            {SCHEME: []}
        ]
        assert self.applies(app, "GET", "/items/featured") == ("authenticated",)
        assert (
            TestClient(app).get("/items/featured").status_code
            == HTTP_401_UNAUTHORIZED
        )

    def test_scopes_a_parent_security_declares_are_described(self) -> None:
        """A `Security` wrapping `Authenticated()` passes its scopes down."""

        async def orders_user(
            principal: Annotated[Principal, Authenticated()],
        ) -> Principal:
            return principal

        def declare(app: FastAPI) -> None:
            @app.get(
                "/orders/history",
                dependencies=[Security(orders_user, scopes=["orders:read"])],
            )
            async def history() -> dict[str, bool]:
                return {"history": True}

        app = fastapi_app(AuthenticatedRequests(verifier()), declare=declare)

        assert app.openapi()["paths"]["/orders/history"]["get"]["security"] == [
            {SCHEME: ["orders:read"]}
        ]
        assert self.applies(app, "GET", "/orders/history") == (
            "authenticated orders:read",
        )
        assert (
            TestClient(app)
            .get("/orders/history", headers=bearer(token()))
            .status_code
            == HTTP_403_FORBIDDEN
        )

    def test_a_report_on_another_app_changes_nothing_served(self) -> None:
        """Describing a different app never opens a route on the served one."""
        app = fastapi_app(AuthenticatedRequests(verifier()))
        other = FastAPI()

        @other.get("/me", dependencies=[Anonymous()])
        async def me() -> dict[str, bool]:
            return {"public": True}

        app.state.micro.describe(other)

        assert TestClient(app).get("/me").status_code == HTTP_401_UNAUTHORIZED

    def test_a_middleware_added_by_hand_describes_anonymous_as_authenticated(
        self,
    ) -> None:
        """It answers `401` on an `Anonymous()` route, and the schema says so."""
        app = FastAPI()

        @app.get("/catalog", dependencies=[Anonymous()])
        async def catalog() -> dict[str, bool]:
            return {"public": True}  # pragma: no cover

        app.add_middleware(AuthenticatedRequestsMiddleware, verifier=verifier())
        micro = Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        )
        micro.install(app)
        app.state.micro = micro

        assert app.openapi()["paths"]["/catalog"]["get"]["security"] == [
            {SCHEME: []}
        ]
        assert self.applies(app, "GET", "/catalog") == ("authenticated",)
        assert TestClient(app).get("/catalog").status_code == (
            HTTP_401_UNAUTHORIZED
        )

    def test_a_litestar_middleware_added_by_hand_reports_authenticated(
        self,
    ) -> None:
        """It answers `401` on an `Anonymous()` handler, and the report says so."""

        @get("/status", opt=LitestarAnonymous())
        async def status() -> dict[str, bool]:
            return {"up": True}  # pragma: no cover

        app = Litestar(
            route_handlers=[status],
            middleware=[
                DefineMiddleware(
                    AuthenticatedRequestsMiddleware,  # ty: ignore[invalid-argument-type]
                    verifier=verifier(),
                )
            ],
        )
        micro = Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        )
        micro.install(app)
        app.state.micro = micro

        assert self.applies(app, "GET", "/status") == ("authenticated",)
        with LitestarTestClient(app) as client:
            assert client.get("/status").status_code == HTTP_401_UNAUTHORIZED


class TestIncludedScopes:
    """Scopes a router was included with are described like its own."""

    def test_scopes_an_include_declares_are_described(self) -> None:
        """The schema and the report name what FastAPI enforces."""
        reports = APIRouter()

        @reports.get("/quarterly")
        async def quarterly() -> dict[str, bool]:
            return {"quarterly": True}

        def declare(app: FastAPI) -> None:
            app.include_router(
                reports, dependencies=[Authenticated(scopes=["reports:read"])]
            )

        app = fastapi_app(AuthenticatedRequests(verifier()), declare=declare)
        report = app.state.micro.describe(app)
        row = next(
            entry.applies
            for entry in report.endpoints
            if entry.method == "GET" and entry.path == "/quarterly"
        )

        assert app.openapi()["paths"]["/quarterly"]["get"]["security"] == [
            {SCHEME: ["reports:read"]}
        ]
        assert row == ("authenticated reports:read",)
        assert (
            TestClient(app)
            .get("/quarterly", headers=bearer(token()))
            .status_code
            == HTTP_403_FORBIDDEN
        )


class TestRepeatedParameters:
    """A mount and a route under it may name the same parameter."""

    def test_a_parameter_a_mount_and_its_route_both_name_is_read(self) -> None:
        """Install, the schema and the report read the app Starlette serves."""

        async def member(request: Any) -> JSONResponse:  # noqa: ANN401
            return JSONResponse(dict(request.path_params))

        def declare(app: FastAPI) -> None:
            app.mount(
                "/orgs/{id}",
                Starlette(routes=[Route("/members/{id}", member)]),
            )

        app = fastapi_app(AuthenticatedRequests(verifier()), declare=declare)
        client = TestClient(app)

        assert "security" not in app.openapi()["paths"]["/catalog"]["get"]
        assert TestReport.applies(app, "GET", "/catalog") == ("anonymous",)
        assert client.get("/catalog").status_code == HTTP_200_OK
        assert client.get("/orgs/1/members/2").status_code == (
            HTTP_401_UNAUTHORIZED
        )
        assert client.get(
            "/orgs/1/members/2", headers=bearer(token())
        ).json() == {"id": "2"}


class TestBoundaries:
    """Where one route's reach ends, and how many authentications an app has."""

    def test_a_convertor_spanning_segments_is_held_at_every_depth(self) -> None:
        """A protected route whose parameter matches a slash stays protected."""

        class Rest(Convertor[str]):  # codespell:ignore
            regex = ".+"

            def convert(self, value: str) -> str:
                return value

            def to_string(self, value: str) -> str:
                return value

        register_url_convertor("grelmicro_rest", Rest())
        try:
            app = FastAPI()

            @app.get("/admin/{rest:grelmicro_rest}")
            async def admin(rest: str) -> dict[str, str]:
                return {"who": "admin", "rest": rest}

            @app.get("/{a}/{b}/{c}", dependencies=[Anonymous()])
            async def triple(a: str, b: str, c: str) -> dict[str, str]:
                return {"who": "public", "path": f"{a}/{b}/{c}"}

            Grelmicro(
                uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
            ).install(app)
            client = TestClient(app)

            refused = client.get("/admin/x/y")
            served = client.get("/one/two/three")
        finally:
            CONVERTOR_TYPES.pop("grelmicro_rest", None)

        assert refused.status_code == HTTP_401_UNAUTHORIZED
        assert served.json() == {"who": "public", "path": "one/two/three"}

    def test_a_second_authentication_is_refused_where_it_is_registered(
        self,
    ) -> None:
        """Two would require every token to pass both verifiers."""
        with pytest.raises(ComponentAlreadyRegisteredError, match="issuers"):
            Grelmicro(
                uses=[
                    AuthenticatedRequests(verifier()),
                    AuthenticatedRequests(verifier(), name="partner"),
                ]
            )
