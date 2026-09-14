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
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from litestar import Litestar, get
from litestar import Request as LitestarRequest
from litestar.testing import TestClient as LitestarTestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import WebSocketDenialResponse

from grelmicro import Grelmicro
from grelmicro.http import (
    AuthenticatedRequests,
    AuthenticatedRequestsConfig,
    AuthenticatedRequestsMiddleware,
    ErrorResponses,
    RateLimitedRequests,
)
from grelmicro.resilience import RateLimiter
from grelmicro.resilience.ratelimiter.memory import MemoryRateLimiterAdapter
from grelmicro.security import (
    ClientBans,
    JWTClaims,
    JWTKey,
    JWTVerifier,
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
    Grelmicro(uses=[ErrorResponses(), *uses]).install(app)
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
