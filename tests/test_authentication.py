"""Tests for authenticating requests at the HTTP edge.

Tokens are signed by the suite's own signer, so a test can build the forged
and misdirected tokens a caller would send. The verifier itself is covered
in `tests/security`, so these hold what the middleware adds: where the
credential is read, what a refused caller is told, and where the middleware
sits among the others.
"""

from __future__ import annotations

import asyncio
import json
import time
import warnings
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Annotated, Any, Self, cast

import pytest
from fastapi import APIRouter, Depends, FastAPI, Security
from fastapi import Request as FastAPIRequest
from fastapi import WebSocket as FastAPIWebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from fastapi.security import (
    HTTPBearer,
    SecurityScopes,
)
from fastapi.testclient import TestClient
from litestar import Litestar, asgi, delete, get, post, websocket
from litestar import Request as LitestarRequest
from litestar import Router as LitestarRouter
from litestar import WebSocket as LitestarWebSocket
from litestar import route as litestar_route
from litestar.config.cors import CORSConfig
from litestar.exceptions import (
    WebSocketDisconnect as LitestarWebSocketDisconnect,
)
from litestar.middleware import DefineMiddleware
from litestar.params import Parameter
from litestar.testing import TestClient as LitestarTestClient
from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    SimpleUser,
)
from starlette.convertors import (  # codespell:ignore
    CONVERTOR_TYPES,
    Convertor,  # codespell:ignore
    register_url_convertor,
)
from starlette.endpoints import HTTPEndpoint
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.responses import Response as StarletteResponse
from starlette.routing import (
    BaseRoute,
    Host,
    Match,
    Mount,
    Route,
    Router,
    WebSocketRoute,
)
from starlette.status import (
    HTTP_307_TEMPORARY_REDIRECT,
    WS_1008_POLICY_VIOLATION,
)
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocket

from grelmicro import ComponentAlreadyRegisteredError, Grelmicro
from grelmicro._describe import _Endpoint, _reads_idempotent
from grelmicro._paths import walk_routes
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.errors import (
    AmbiguousCredentialsError,
    MiddlewarePlacementWarning,
    SettingsValidationError,
)
from grelmicro.http import (
    AuthenticatedRequests,
    AuthenticatedRequestsConfig,
    AuthenticatedRequestsMiddleware,
    CachedResponses,
    ErrorResponses,
    RateLimitedRequests,
)
from grelmicro.http._authentication import (
    _litestar_declares_public,
    document_operations,
)
from grelmicro.http._idempotency import _has_dependencies
from grelmicro.integrations.fastapi import (
    Anonymous,
    Authenticated,
    CachedResponse,
    Claims,
    CurrentPrincipal,
    OptionalPrincipal,
    document_authenticated_requests,
)
from grelmicro.integrations.litestar import Anonymous as LitestarAnonymous
from grelmicro.integrations.litestar import (
    Authenticated as LitestarAuthenticated,
)
from grelmicro.integrations.starlette import (
    Authenticated as StarletteAuthenticated,
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
HTTP_204_NO_CONTENT = 204
HTTP_400_BAD_REQUEST = 400
HTTP_403_FORBIDDEN = 403
HTTP_404_NOT_FOUND = 404
HTTP_405_METHOD_NOT_ALLOWED = 405
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


class Asking:
    """A verifier that answers asynchronously, the way a network one would."""

    def __init__(self, *, known: bool = True) -> None:
        """Answer `unknown-key` until a refresh when not `known`."""
        self._real = verifier()
        self.known = known
        self.refreshes = 0

    async def verify(self, token: str) -> JWTClaims:
        """Verify the token once its key is known."""
        if not self.known:
            raise TokenRejectedError(TokenRejectedReason.UNKNOWN_KEY)
        return self._real.verify(token)

    async def refresh(self, *, force: bool = False) -> bool:  # noqa: ARG002
        """Learn the key."""
        self.refreshes += 1
        self.known = True
        return True


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

    @pytest.mark.parametrize("header", ["Bearer", "Bearer ", "Bearer   "])
    def test_a_bearer_scheme_with_no_token_is_asked_for_one(
        self, header: str
    ) -> None:
        """No token behind the scheme is no credential at all."""
        client = TestClient(app_with(AuthenticatedRequests(verifier())))

        response = client.get("/whoami", headers={"authorization": header})

        assert response.status_code == HTTP_401_UNAUTHORIZED
        assert response.headers["www-authenticate"] == "Bearer"

    def test_spaces_between_the_scheme_and_the_token_are_accepted(
        self,
    ) -> None:
        """RFC 7235 allows one or more spaces after the scheme."""
        client = TestClient(app_with(AuthenticatedRequests(verifier())))

        response = client.get(
            "/whoami", headers={"authorization": f"Bearer   {token()}"}
        )

        assert response.status_code == HTTP_200_OK

    def test_an_excluded_path_never_reads_a_token(self) -> None:
        """A token sent to a path authentication leaves alone is not looked at."""
        client = TestClient(
            app_with(AuthenticatedRequests(verifier(), exclude=("/livez",)))
        )

        response = client.get("/livez", headers=bearer(token(FORGER)))

        assert response.json() == {"live": True}

    def test_a_refusal_is_rendered_in_the_format_the_app_registered(
        self,
    ) -> None:
        """A service answering in TMF refuses a credential in TMF too."""
        tmf = ErrorResponses.tmf()
        app = Starlette(routes=[Route("/whoami", whoami)])
        Grelmicro(uses=[tmf, AuthenticatedRequests(verifier())]).install(app)

        response = TestClient(app).get("/whoami")
        problem = TestClient(app_with(AuthenticatedRequests(verifier()))).get(
            "/whoami"
        )

        assert response.status_code == HTTP_401_UNAUTHORIZED
        assert response.headers["content-type"].split(";")[0] == tmf.media_type
        assert tmf.media_type != ErrorResponses().media_type
        assert problem.json()["instance"] == "/whoami"

    def test_a_tab_after_the_scheme_is_not_a_separator(self) -> None:
        """RFC 7235 separates the scheme from the token with spaces only."""
        client = TestClient(app_with(AuthenticatedRequests(verifier())))

        response = client.get(
            "/whoami", headers={"authorization": f"Bearer \t{token()}"}
        )

        assert response.status_code == HTTP_401_UNAUTHORIZED

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

    async def test_the_component_closes_the_verifier_it_opened(self) -> None:
        """Its background refresh stops when the app stops."""

        class Tracked:
            opened = False
            closed = False

            def verify(self, token: str) -> JWTClaims:
                raise NotImplementedError  # pragma: no cover

            def verify_header(self, header: str | None) -> JWTClaims:
                raise NotImplementedError  # pragma: no cover

            async def __aenter__(self) -> Self:
                self.opened = True
                return self

            async def __aexit__(self, *exc: object) -> None:
                self.closed = True

        tracked = Tracked()
        component = AuthenticatedRequests(tracked)

        async with component:
            opened = tracked.opened

        assert opened
        assert tracked.closed

    def test_a_verifier_that_answers_asynchronously_is_awaited(self) -> None:
        """A valid token is served, and a forged one refused with the reason."""
        client = TestClient(app_with(AuthenticatedRequests(Asking())))

        served = client.get("/whoami", headers=bearer(token()))
        refused = client.get("/whoami", headers=bearer(token(FORGER)))

        assert served.json()["subject"] == "user-1"
        assert refused.status_code == HTTP_401_UNAUTHORIZED
        assert refused.headers["www-authenticate"] == (
            'Bearer error="invalid_token"'
        )

    def test_an_asynchronous_verifier_is_awaited_again_after_a_refresh(
        self,
    ) -> None:
        """A key it learns on refresh serves the request that asked for it."""
        asking = Asking(known=False)
        client = TestClient(app_with(AuthenticatedRequests(asking)))

        response = client.get("/whoami", headers=bearer(token()))

        assert response.json()["subject"] == "user-1"
        assert asking.refreshes == 1

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


def scoped_app(*uses: Any) -> Starlette:  # noqa: ANN401
    """Return a Starlette app whose endpoints require `orders:write`."""

    @StarletteAuthenticated(scopes=["orders:write"])
    async def cancel(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse({"cancelled": True})

    @StarletteAuthenticated(scopes=["orders:write"])
    def cancel_now(request: Request) -> JSONResponse:  # noqa: ARG001
        return JSONResponse({"cancelled": True})

    class Orders(HTTPEndpoint):
        @StarletteAuthenticated(scopes=["orders:write"])
        async def delete(self, request: Request) -> JSONResponse:  # noqa: ARG002
            return JSONResponse({"cancelled": True})

    @StarletteAuthenticated(scopes=["orders:write"])
    async def follow(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"following": True})
        await websocket.close()

    app = Starlette(
        routes=[
            Route("/async", cancel, methods=["DELETE"]),
            Route("/sync", cancel_now, methods=["DELETE"]),
            Route("/endpoint", Orders),
            WebSocketRoute("/follow", follow),
        ]
    )
    micro = Grelmicro(uses=[ErrorResponses(), *uses])
    micro.install(app)
    app.state.micro = micro
    return app


class TestStarlette:
    """Scopes required on Starlette endpoints."""

    @pytest.mark.parametrize("path", ["/async", "/sync", "/endpoint"])
    def test_the_scope_granted_serves_the_endpoint(self, path: str) -> None:
        """A function, a sync function and an endpoint method alike."""
        client = TestClient(scoped_app(AuthenticatedRequests(verifier())))

        response = client.delete(
            path, headers=bearer(token(scope="orders:write"))
        )

        assert response.json() == {"cancelled": True}

    @pytest.mark.parametrize("path", ["/async", "/sync", "/endpoint"])
    def test_a_missing_scope_is_forbidden_with_the_challenge(
        self, path: str
    ) -> None:
        """The refusal names the scope, as RFC 6750 asks."""
        client = TestClient(scoped_app(AuthenticatedRequests(verifier())))

        response = client.delete(path, headers=bearer(token()))

        assert response.status_code == HTTP_403_FORBIDDEN
        assert response.headers["www-authenticate"] == (
            'Bearer error="insufficient_scope", scope="orders:write"'
        )

    def test_a_websocket_without_the_scope_is_denied(self) -> None:
        """The handshake is refused, and granted with the scope."""
        client = TestClient(scoped_app(AuthenticatedRequests(verifier())))

        with (
            pytest.raises(WebSocketDenialResponse) as caught,
            client.websocket_connect("/follow", headers=bearer(token())),
        ):
            pass  # pragma: no cover
        with client.websocket_connect(
            "/follow", headers=bearer(token(scope="orders:write"))
        ) as socket:
            following = socket.receive_json()

        assert caught.value.status_code == HTTP_403_FORBIDDEN
        assert following == {"following": True}

    def test_without_the_component_a_credential_is_asked_for(self) -> None:
        """Nothing verified the caller, so the endpoint asks for a token."""
        client = TestClient(scoped_app())

        response = client.delete("/async")

        assert response.status_code == HTTP_401_UNAUTHORIZED
        assert response.headers["www-authenticate"] == (
            'Bearer scope="orders:write"'
        )

    def test_the_report_names_the_scope(self) -> None:
        """`grelmicro check` reads the decorator as it reads a guard."""
        app = scoped_app(AuthenticatedRequests(verifier()))
        report = app.state.micro.describe(app)

        assert next(
            row.applies
            for row in report.endpoints
            if row.method == "DELETE" and row.path == "/async"
        ) == ("authenticated orders:write",)

    def test_an_endpoint_object_with_an_async_call_is_awaited(self) -> None:
        """An object whose `__call__` is a coroutine is served, not returned."""

        class Cancel:
            async def __call__(self, request: Request) -> JSONResponse:  # noqa: ARG002
                return JSONResponse({"cancelled": True})

        app = Starlette(
            routes=[
                Route(
                    "/object",
                    StarletteAuthenticated(scopes=["orders:write"])(Cancel()),
                    methods=["DELETE"],
                )
            ]
        )
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.delete(
            "/object", headers=bearer(token(scope="orders:write"))
        )

        assert response.json() == {"cancelled": True}

    def test_stacked_decorators_require_and_report_every_scope(self) -> None:
        """Each one adds its scopes, and the refusal names all of them."""

        @StarletteAuthenticated(scopes=["orders:read"])
        @StarletteAuthenticated(scopes=["admin"])
        async def audit(request: Request) -> JSONResponse:  # noqa: ARG001
            return JSONResponse({"audited": True})

        app = Starlette(routes=[Route("/audit", audit)])
        micro = Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        )
        micro.install(app)
        client = TestClient(app)

        refused = client.get(
            "/audit", headers=bearer(token(scope="orders:read"))
        )
        served = client.get(
            "/audit", headers=bearer(token(scope="orders:read admin"))
        )
        applies = next(
            row.applies
            for row in micro.describe(app).endpoints
            if row.method == "GET" and row.path == "/audit"
        )

        assert refused.headers["www-authenticate"] == (
            'Bearer error="insufficient_scope", scope="orders:read admin"'
        )
        assert served.json() == {"audited": True}
        assert applies == ("authenticated orders:read admin",)

    def test_an_endpoint_taking_no_connection_is_refused(self) -> None:
        """There would be nothing to read the caller from."""

        async def orphan() -> None: ...  # pragma: no cover

        with pytest.raises(TypeError, match="request or a websocket"):
            StarletteAuthenticated(scopes=["orders:write"])(orphan)

    def test_scopes_written_as_one_string_are_refused(self) -> None:
        """One string would otherwise read as one scope per character."""
        with pytest.raises(TypeError, match="not a single string"):
            StarletteAuthenticated(scopes="orders:write")


async def shown(request: Request) -> JSONResponse:
    """Answer with how the caller in the scope reads."""
    return JSONResponse(
        {
            "display": request.user.display_name,
            "authenticated": request.user.is_authenticated,
        }
    )


async def identified(request: Request) -> JSONResponse:
    """Answer with the caller's identity as well."""
    return JSONResponse(
        {
            "identity": request.user.identity,
            "display": request.user.display_name,
            "authenticated": request.user.is_authenticated,
            "scopes": sorted(request.auth.scopes),
        }
    )


class Outer(AuthenticationBackend):
    """An authentication the app runs itself, outside ours."""

    async def authenticate(
        self,
        conn: Any,  # noqa: ANN401, ARG002
    ) -> tuple[AuthCredentials, SimpleUser]:
        """Name every caller `outer`."""
        return AuthCredentials(["outer"]), SimpleUser("outer")


class TestCallerShape:
    """What an endpoint reads as its caller, wherever it is served."""

    def test_the_caller_reads_the_way_starlette_reads_a_user(self) -> None:
        """Empty and not authenticated on an excluded path, named on the rest."""
        app = Starlette(
            routes=[Route("/open", identified), Route("/closed", identified)]
        )
        Grelmicro(
            uses=[
                ErrorResponses(),
                AuthenticatedRequests(verifier(), exclude=("/open",)),
            ]
        ).install(app)
        client = TestClient(app)

        assert client.get("/open").json() == {
            "identity": "",
            "display": "",
            "authenticated": False,
            "scopes": [],
        }
        assert client.get("/closed", headers=bearer(token())).json() == {
            "identity": "user-1",
            "display": "user-1",
            "authenticated": True,
            "scopes": [],
        }

    def test_an_excluded_path_keeps_the_caller_an_outer_middleware_set(
        self,
    ) -> None:
        """Only a path it authenticates has its caller replaced."""
        app = Starlette(
            routes=[Route("/open", shown), Route("/closed", shown)],
            middleware=[Middleware(AuthenticationMiddleware, backend=Outer())],
        )
        Grelmicro(
            uses=[
                ErrorResponses(),
                AuthenticatedRequests(verifier(), exclude=("/open",)),
            ]
        ).install(app)
        client = TestClient(app)

        assert client.get("/open").json() == {
            "display": "outer",
            "authenticated": True,
        }
        assert client.get("/closed", headers=bearer(token())).json() == {
            "display": "user-1",
            "authenticated": True,
        }


class TestUnreachable:
    """A route that could never serve what it was written for is refused."""

    @staticmethod
    def install(app: Any, **options: Any) -> None:  # noqa: ANN401
        """Register authentication with `options`, and install it on `app`."""
        Grelmicro(
            uses=[
                ErrorResponses(),
                AuthenticatedRequests(verifier(), **options),
            ]
        ).install(app)

    def test_an_anonymous_route_requiring_a_caller_is_refused(self) -> None:
        """A request without a token is what `Anonymous()` means to serve."""
        app = FastAPI()

        @app.get("/both", dependencies=[Anonymous(), Authenticated()])
        async def both() -> None: ...  # pragma: no cover

        with pytest.raises(TypeError, match="GET /both declares Anonymous"):
            self.install(app)

    def test_an_anonymous_route_reading_the_caller_is_refused(self) -> None:
        """The message points at the dependency a public route reads."""
        app = FastAPI()

        @app.get("/who", dependencies=[Anonymous()])
        async def who(
            principal: CurrentPrincipal,
        ) -> None: ...  # pragma: no cover

        with pytest.raises(TypeError, match="OptionalPrincipal"):
            self.install(app)

    def test_an_excluded_route_requiring_a_caller_is_refused(self) -> None:
        """A token is never read there, so every request would be refused."""
        app = FastAPI()

        @app.get("/probe", dependencies=[Authenticated()])
        async def probe() -> None: ...  # pragma: no cover

        with pytest.raises(TypeError, match="GET /probe is in exclude"):
            self.install(app, exclude=("/probe",))

    def test_a_security_scope_that_is_not_a_token_is_refused(self) -> None:
        """A scope a `Security` around the caller names is checked at install."""
        app = FastAPI()

        async def outer(principal: CurrentPrincipal) -> Any:  # noqa: ANN401
            return principal  # pragma: no cover

        @app.get("/read", dependencies=[Security(outer, scopes=["réad"])])
        async def read() -> None: ...  # pragma: no cover

        with pytest.raises(
            ValueError, match="GET /read requires the scope 'réad'"
        ):
            self.install(app)

    def test_a_litestar_handler_public_and_guarded_is_refused(self) -> None:
        """`opt=Anonymous()` and an `Authenticated` guard contradict each other."""

        @get("/both", opt=LitestarAnonymous(), guards=[LitestarAuthenticated()])
        async def both() -> None: ...  # pragma: no cover

        with pytest.raises(TypeError, match="GET /both declares Anonymous"):
            self.install(Litestar(route_handlers=[both]))

    def test_a_litestar_websocket_public_and_guarded_is_refused(self) -> None:
        """A websocket handler names no method, and contradicts itself all the same."""

        @websocket(
            "/live", opt=LitestarAnonymous(), guards=[LitestarAuthenticated()]
        )
        async def live(
            socket: LitestarWebSocket,
        ) -> None: ...  # pragma: no cover

        with pytest.raises(TypeError, match="/live declares Anonymous"):
            self.install(Litestar(route_handlers=[live]))

    def test_a_guarded_litestar_websocket_in_exclude_is_refused(self) -> None:
        """A websocket handler names no method, and is refused all the same."""

        @websocket("/live", guards=[LitestarAuthenticated()])
        async def live(
            socket: LitestarWebSocket,
        ) -> None: ...  # pragma: no cover

        with pytest.raises(TypeError, match="/live is in exclude"):
            self.install(Litestar(route_handlers=[live]), exclude=("/live",))

    def test_a_decorated_starlette_endpoint_in_exclude_is_refused(self) -> None:
        """A function and an endpoint method alike."""

        class Orders(HTTPEndpoint):
            @StarletteAuthenticated()
            async def delete(
                self, request: Request
            ) -> None: ...  # pragma: no cover

        @StarletteAuthenticated()
        async def probe(
            request: Request,
        ) -> None: ...  # pragma: no cover

        for route in (Route("/orders", Orders), Route("/probe", probe)):
            with pytest.raises(TypeError, match="is in exclude"):
                self.install(Starlette(routes=[route]), exclude=(route.path,))

    def test_a_route_declared_after_install_is_refused_at_startup(self) -> None:
        """The app is read again when it starts, and checked again."""
        app = FastAPI()
        self.install(app, exclude=("/late",))

        @app.get("/late", dependencies=[Authenticated()])
        async def late() -> None: ...  # pragma: no cover

        with (
            pytest.raises(TypeError, match="GET /late is in exclude"),
            TestClient(app),
        ):
            pass  # pragma: no cover

    def test_a_route_is_checked_on_every_method_it_answers(self) -> None:
        """A guarded read beside an open delete is found, in a stable order."""

        @get("/orders/{order_id:int}", guards=[LitestarAuthenticated()])
        async def read(
            order_id: Annotated[int, Parameter()],
        ) -> None: ...  # pragma: no cover

        @delete("/orders/{order_id:int}")
        async def remove(
            order_id: Annotated[int, Parameter()],
        ) -> None: ...  # pragma: no cover

        with pytest.raises(
            TypeError, match=r"GET /orders/\{order_id\} is in exclude"
        ):
            self.install(
                Litestar(route_handlers=[read, remove]), exclude=("/orders/*",)
            )

    def test_the_refusal_names_the_path_as_the_schema_does(self) -> None:
        """A converter is left out of the path the message names."""

        @StarletteAuthenticated()
        async def item(request: Request) -> None: ...  # pragma: no cover

        with pytest.raises(
            TypeError, match=r"GET /items/\{item_id\} is in exclude"
        ):
            self.install(
                Starlette(routes=[Route("/items/{item_id:int}", item)]),
                exclude=("/items/*",),
            )

    def test_the_refusal_names_the_method_the_route_answers(self) -> None:
        """A route answering only `DELETE` is named by it."""
        app = FastAPI()

        @app.delete("/orders", dependencies=[Authenticated()])
        async def cancel() -> None: ...  # pragma: no cover

        with pytest.raises(TypeError, match="DELETE /orders is in exclude"):
            self.install(app, exclude=("/orders",))

    def test_a_router_included_with_authenticated_is_checked(self) -> None:
        """What the include declares is read along with the route."""
        router = APIRouter()

        @router.get("/health")
        async def health() -> None: ...  # pragma: no cover

        app = FastAPI()
        app.include_router(
            router, prefix="/ops", dependencies=[Authenticated()]
        )

        with pytest.raises(TypeError, match="GET /ops/health is in exclude"):
            self.install(app, exclude=("/ops/*",))

    def test_a_router_included_as_public_is_checked(self) -> None:
        """`Anonymous()` on the include counts as on the route."""
        router = APIRouter()

        @router.get("/profile", dependencies=[Authenticated()])
        async def profile() -> None: ...  # pragma: no cover

        app = FastAPI()
        app.include_router(router, dependencies=[Anonymous()])

        with pytest.raises(TypeError, match="GET /profile declares Anonymous"):
            self.install(app)

    def test_a_route_inside_a_wrapped_mount_is_checked(self) -> None:
        """Middleware around a mounted app hides none of its routes."""
        sub = FastAPI()

        @sub.get("/both", dependencies=[Anonymous(), Authenticated()])
        async def both() -> None: ...  # pragma: no cover

        app = FastAPI()
        app.mount("/sub", GZipMiddleware(sub))

        with pytest.raises(TypeError, match="GET /sub/both declares Anonymous"):
            self.install(app)


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

    @pytest.mark.parametrize("pattern", ["/*", "*"])
    def test_an_exclude_that_matches_every_path_is_refused(
        self, pattern: str
    ) -> None:
        """It would serve every request without a credential."""
        with pytest.raises(ValueError, match="every path"):
            AuthenticatedRequests(verifier(), exclude=(pattern,))
        with pytest.raises(ValueError, match="every path"):
            AuthenticatedRequestsMiddleware(
                app_with(), verifier=verifier(), exclude=("/livez", pattern)
            )

    def test_an_exclude_under_a_prefix_is_kept(self) -> None:
        """A prefix narrower than the whole app is what `exclude` is for."""
        component = AuthenticatedRequests(verifier(), exclude=("/internal/*",))

        assert component.config.exclude == ("/internal/*",)

    def test_from_config_keeps_every_argument(self) -> None:
        """The verifier, the bans, the proxies, the name and the schema opt-out."""
        bans = ClientBans()
        trusted = TrustedProxies(PROXIES)
        trusting = verifier()
        component = AuthenticatedRequests.from_config(
            AuthenticatedRequestsConfig(),
            trusting,
            bans=bans,
            trusted=trusted,
            name="edge",
            openapi=False,
        )
        _, options = component.asgi_middleware()
        app = FastAPI()
        Grelmicro(uses=[ErrorResponses(), component]).install(app)

        assert component.verifier is trusting
        assert component.name == "edge"
        assert options["bans"] is bans
        assert options["trusted"] is trusted
        assert SCHEME not in app.openapi().get("components", {}).get(
            "securitySchemes", {}
        )

    def test_from_config_describes_the_schema_by_default(self) -> None:
        """Only `openapi=False` leaves the schema alone."""
        component = AuthenticatedRequests.from_config(
            AuthenticatedRequestsConfig(), verifier()
        )
        app = FastAPI()
        Grelmicro(uses=[ErrorResponses(), component]).install(app)

        assert SCHEME in app.openapi()["components"]["securitySchemes"]

    def test_exclude_written_as_one_string_is_refused(self) -> None:
        """A missing comma would otherwise exclude by single characters."""
        with pytest.raises(TypeError, match="exclude"):
            AuthenticatedRequests(verifier(), exclude="/livez")  # ty: ignore[invalid-argument-type]
        with pytest.raises(TypeError, match="exclude"):
            AuthenticatedRequestsMiddleware(
                app_with(),
                verifier=verifier(),
                exclude="/livez",  # ty: ignore[invalid-argument-type]
            )

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


def fastapi_app(*uses: Any, declare: Any = None) -> FastAPI:  # noqa: ANN401, C901
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

    @app.get("/livez")
    async def probe() -> dict[str, bool]:
        return {"live": True}  # pragma: no cover

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

    def test_a_cors_preflight_is_answered_before_authentication(self) -> None:
        """A browser asks before it sends the token, and is answered."""
        app = FastAPI()
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["https://app.example"],
            allow_methods=["*"],
        )

        @app.delete("/orders/{order_id}")
        async def cancel(order_id: int) -> None: ...  # pragma: no cover

        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)

        response = TestClient(app).options(
            "/orders/7",
            headers={
                "origin": "https://app.example",
                "access-control-request-method": "DELETE",
            },
        )

        assert response.status_code == HTTP_200_OK
        assert response.headers["access-control-allow-origin"] == (
            "https://app.example"
        )

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

    def test_a_websocket_route_reads_the_caller_and_its_scopes(self) -> None:
        """`CurrentPrincipal` and `Authenticated` work on a websocket too."""

        def declare(app: FastAPI) -> None:
            @app.websocket(
                "/live", dependencies=[Authenticated(scopes=["live:read"])]
            )
            async def live(
                socket: FastAPIWebSocket, principal: CurrentPrincipal
            ) -> None:
                await socket.accept()
                await socket.send_json({"subject": principal.subject})
                await socket.close()

        client = TestClient(
            fastapi_app(AuthenticatedRequests(verifier()), declare=declare)
        )

        with client.websocket_connect(
            "/live", headers=bearer(token(scope="live:read"))
        ) as socket:
            assert socket.receive_json() == {"subject": "user-1"}

    def test_an_anonymous_route_reads_a_caller_that_presents_a_token(
        self,
    ) -> None:
        """No token is anonymous, a valid one is the caller, a bad one is refused."""

        def declare(app: FastAPI) -> None:
            @app.get("/offers", dependencies=[Anonymous()])
            async def offers(principal: OptionalPrincipal) -> dict[str, Any]:
                return {
                    "subject": None if principal is None else principal.subject
                }

        client = TestClient(
            fastapi_app(AuthenticatedRequests(verifier()), declare=declare)
        )
        refused = client.get("/offers", headers=bearer(token(FORGER)))

        assert client.get("/offers").json() == {"subject": None}
        assert client.get("/offers", headers=bearer(token())).json() == {
            "subject": "user-1"
        }
        assert refused.status_code == HTTP_401_UNAUTHORIZED
        assert refused.headers["www-authenticate"] == (
            'Bearer error="invalid_token"'
        )
        assert client.get(
            "/offers", headers={"authorization": "Basic dXNlcjpwYXNz"}
        ).json() == {"subject": None}
        assert client.get(
            "/offers", headers={"authorization": "Bearer"}
        ).json() == {"subject": None}
        assert (
            client.get(
                "/offers",
                headers=[
                    ("authorization", f"Bearer {token()}"),
                    ("authorization", "Basic dXNlcjpwYXNz"),
                ],
            ).status_code
            == HTTP_400_BAD_REQUEST
        )

    def test_an_anonymous_route_refuses_two_credentials(self) -> None:
        """Two credentials of another scheme are refused, not ignored."""

        def declare(app: FastAPI) -> None:
            @app.get("/brochure", dependencies=[Anonymous()])
            async def brochure() -> dict[str, bool]:
                return {"brochure": True}  # pragma: no cover

        client = TestClient(
            fastapi_app(AuthenticatedRequests(verifier()), declare=declare)
        )

        response = client.get(
            "/brochure",
            headers=[
                ("authorization", "Basic YQ=="),
                ("authorization", "Basic Yg=="),
            ],
        )

        assert response.status_code == HTTP_400_BAD_REQUEST


OAUTH_METADATA = "https://auth.grel.info/.well-known/oauth-authorization-server"
OIDC_METADATA = "https://auth.grel.info/.well-known/openid-configuration"
SCHEME = "AuthenticatedRequests"


class TestAppWide:
    """Declarations the app makes for every route at once."""

    def test_scopes_the_app_declares_apply_to_every_route(self) -> None:
        """`FastAPI(dependencies=[...])` requires its scopes everywhere."""
        app = FastAPI(dependencies=[Authenticated(scopes=["global"])])

        @app.get("/anything")
        async def anything() -> dict[str, bool]:
            return {"ok": True}

        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)
        client = TestClient(app)

        assert (
            client.get("/anything", headers=bearer(token())).status_code
            == HTTP_403_FORBIDDEN
        )
        assert client.get(
            "/anything", headers=bearer(token(scope="global"))
        ).json() == {"ok": True}
        assert app.openapi()["paths"]["/anything"]["get"]["security"] == [
            {SCHEME: ["global"]}
        ]


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
        """An anonymous read offers the scheme, an excluded path names none."""
        schema = fastapi_app(
            AuthenticatedRequests(verifier(), exclude=("/livez",))
        ).openapi()

        assert schema["paths"]["/catalog"]["get"]["security"] == [
            {},
            {SCHEME: []},
        ]
        assert "401" in schema["paths"]["/catalog"]["get"]["responses"]
        assert "401" not in schema["paths"]["/livez"]["get"]["responses"]
        assert "security" not in schema["paths"]["/livez"]["get"]
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

    def test_the_schemas_own_root_requirement_is_joined(self) -> None:
        """An operation inheriting the app's requirement keeps it, beside ours."""

        def declare(app: FastAPI) -> None:
            generated = app.openapi

            def with_root_security() -> dict[str, Any]:
                schema = generated()
                schema["security"] = [{"ApiKey": []}]
                return schema

            app.openapi = with_root_security  # ty: ignore[invalid-assignment]

        app = fastapi_app(AuthenticatedRequests(verifier()), declare=declare)
        paths = app.openapi()["paths"]

        assert paths["/me"]["get"]["security"] == [{"ApiKey": [], SCHEME: []}]
        assert paths["/catalog"]["get"]["security"] == [
            {"ApiKey": []},
            {"ApiKey": [], SCHEME: []},
        ]

    def test_a_webhook_is_left_without_the_scheme(self) -> None:
        """A webhook is a request the app sends, not one it answers."""

        def declare(app: FastAPI) -> None:
            @app.webhooks.post("new-order")
            def new_order(order: dict[str, str]) -> None:
                """Describe the call made when an order is placed."""

        schema = fastapi_app(
            AuthenticatedRequests(verifier()), declare=declare
        ).openapi()
        operation = schema["webhooks"]["new-order"]["post"]

        assert "security" not in operation
        assert "401" not in operation["responses"]

    def test_a_websocket_route_never_breaks_the_schema(self) -> None:
        """A route that names no method is left out of what is read."""

        def declare(app: FastAPI) -> None:
            @app.websocket("/live")
            async def live(
                socket: FastAPIWebSocket,
            ) -> None: ...  # pragma: no cover

        schema = fastapi_app(
            AuthenticatedRequests(verifier()), declare=declare
        ).openapi()

        assert "/me" in schema["paths"]

    def test_each_refusal_names_its_body_and_its_header(self) -> None:
        """The `401`, `403` and `429` carry the error body and their headers."""
        component = AuthenticatedRequests(
            verifier(), bans=ClientBans(), trusted=TrustedProxies(PROXIES)
        )
        schema = fastapi_app(component).openapi()
        covered = schema["paths"]["/orders/{order_id}"]["delete"]["responses"]
        public = schema["paths"]["/catalog"]["get"]["responses"]

        for responses, statuses in (
            (covered, ("401", "403", "429")),
            (public, ("401", "429")),
        ):
            for status in statuses:
                response = responses[status]
                assert response["description"]
                assert response["content"]["application/problem+json"][
                    "schema"
                ]["$ref"].endswith("ProblemDetail")
        assert "WWW-Authenticate" in covered["401"]["headers"]
        assert "WWW-Authenticate" in covered["403"]["headers"]
        assert "Retry-After" in covered["429"]["headers"]
        assert "403" not in public

    def test_a_public_route_keeps_its_own_security_beside_the_token(
        self,
    ) -> None:
        """What the route checks itself stays required, the token optional."""
        own = HTTPBearer(scheme_name="Own", auto_error=False)

        def declare(app: FastAPI) -> None:
            @app.get("/offers", dependencies=[Anonymous(), Depends(own)])
            async def offers() -> dict[str, bool]:
                return {"offers": True}  # pragma: no cover

        schema = fastapi_app(
            AuthenticatedRequests(verifier()), declare=declare
        ).openapi()

        assert schema["paths"]["/offers"]["get"]["security"] == [
            {"Own": []},
            {"Own": [], SCHEME: []},
        ]

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

    def test_the_options_litestar_answers_on_a_public_path_is_public(
        self,
    ) -> None:
        """The `OPTIONS` Litestar adds to a route is public where a handler is."""
        with LitestarTestClient(
            litestar_app(AuthenticatedRequests(verifier()))
        ) as client:
            public = client.options("/catalog/7")
            private = client.options("/orders/7")

        assert public.status_code == HTTP_204_NO_CONTENT
        assert "GET" in public.headers["allow"]
        assert private.status_code == HTTP_401_UNAUTHORIZED

    def test_an_options_handler_of_its_own_is_authenticated_by_its_own_opt(
        self,
    ) -> None:
        """Only the answer Litestar adds borrows the public handler's declaration."""

        @get("/own", opt=LitestarAnonymous())
        async def read() -> dict[str, bool]:
            return {"read": True}  # pragma: no cover

        @litestar_route("/own", http_method=["OPTIONS"])
        async def describe() -> None: ...  # pragma: no cover

        app = Litestar(route_handlers=[describe, read])
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)
        with LitestarTestClient(app) as client:
            response = client.options("/own")

        assert response.status_code == HTTP_401_UNAUTHORIZED

    def test_a_cors_preflight_is_answered_before_authentication(self) -> None:
        """A browser asks before it sends the token, and is answered."""

        @delete("/orders/{order_id:int}", guards=[LitestarAuthenticated()])
        async def cancel(
            order_id: Annotated[int, Parameter()],
        ) -> None: ...  # pragma: no cover

        app = Litestar(
            route_handlers=[cancel],
            cors_config=CORSConfig(allow_origins=["https://app.example"]),
        )
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)
        with LitestarTestClient(app) as client:
            response = client.options(
                "/orders/7",
                headers={
                    "origin": "https://app.example",
                    "access-control-request-method": "DELETE",
                },
            )

        assert response.status_code == HTTP_204_NO_CONTENT
        assert response.headers["access-control-allow-origin"] == (
            "https://app.example"
        )

    def test_exclude_matches_the_path_litestar_routes(self) -> None:
        """A trailing slash Litestar's router drops is dropped here too."""
        with LitestarTestClient(
            litestar_app(
                AuthenticatedRequests(verifier(), exclude=("/catalog/7",))
            )
        ) as client:
            response = client.post("/catalog/7/")

        assert response.json() == {"item": 7}

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

    def test_litestars_own_opt_out_key_serves_a_handler_publicly(self) -> None:
        """A handler written for Litestar's own authentication is public here."""

        @get("/health", opt={"exclude_from_auth": True})
        async def health() -> dict[str, bool]:
            return {"ok": True}

        app = Litestar(route_handlers=[health])
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)

        with LitestarTestClient(app) as client:
            response = client.get("/health")

        assert response.json() == {"ok": True}
        assert LitestarAnonymous() == {"exclude_from_auth": True}

    def test_the_litestar_schema_describes_what_each_operation_needs(
        self,
    ) -> None:
        """The scheme, the scopes and the refusals, optional on public handlers."""
        with LitestarTestClient(
            litestar_app(
                AuthenticatedRequests(verifier(), exclude=("/schema/*",))
            )
        ) as client:
            schema = client.get("/schema/openapi.json").json()
        paths = schema["paths"]

        assert SCHEME in schema["components"]["securitySchemes"]
        assert paths["/status"]["get"]["security"] == [{}, {SCHEME: []}]
        assert paths["/catalog/{item_id}"]["get"]["security"] == [
            {},
            {SCHEME: []},
        ]
        assert paths["/catalog/{item_id}"]["post"]["security"] == [{SCHEME: []}]
        assert "401" in paths["/catalog/{item_id}"]["post"]["responses"]
        assert paths["/orders/{order_id}"]["delete"]["security"] == [
            {SCHEME: ["orders:write"]}
        ]
        assert "403" in paths["/orders/{order_id}"]["delete"]["responses"]

    def test_a_litestar_app_without_a_schema_starts_all_the_same(self) -> None:
        """No schema is published, so there is nothing to describe."""

        @get("/status", opt=LitestarAnonymous())
        async def status() -> dict[str, bool]:
            return {"up": True}

        app = Litestar(route_handlers=[status], openapi_config=None)
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)

        with LitestarTestClient(app) as client:
            assert client.get("/status").json() == {"up": True}

    def test_an_anonymous_handler_verifies_a_token_it_is_sent(self) -> None:
        """A valid token is the caller, one that does not verify is refused."""

        @get("/hello", opt=LitestarAnonymous())
        async def hello(request: LitestarRequest) -> dict[str, bool]:
            return {"authenticated": request.user.is_authenticated}

        app = Litestar(route_handlers=[hello])
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)

        with LitestarTestClient(app) as client:
            anonymous = client.get("/hello")
            known = client.get("/hello", headers=bearer(token()))
            forged = client.get("/hello", headers=bearer(token(FORGER)))

        assert anonymous.json() == {"authenticated": False}
        assert known.json() == {"authenticated": True}
        assert forged.status_code == HTTP_401_UNAUTHORIZED

    def test_a_public_handler_is_found_under_the_root_path(self) -> None:
        """The root path is taken off the way Litestar's router takes it off."""

        @get("/status", opt=LitestarAnonymous())
        async def status() -> dict[str, bool]:
            return {"up": True}

        @get("/private")
        async def private() -> dict[str, bool]:
            return {"private": True}  # pragma: no cover

        app = Litestar(route_handlers=[status, private])
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)

        with LitestarTestClient(app, root_path="/api") as client:
            served = client.get("/api/status")
            refused = client.get("/api/private")

        assert served.json() == {"up": True}
        assert refused.status_code == HTTP_401_UNAUTHORIZED

    def test_the_root_path_is_taken_off_once(self) -> None:
        """A path repeating the root path is routed as Litestar routes it."""

        @get("/status", opt=LitestarAnonymous())
        async def status() -> dict[str, bool]:
            return {"up": True}  # pragma: no cover

        @get("/v1/api/status")
        async def nested() -> dict[str, bool]:
            return {"nested": True}  # pragma: no cover

        app = Litestar(route_handlers=[status, nested])
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)

        with LitestarTestClient(app, root_path="/api") as client:
            response = client.get("/api/v1/api/status")

        assert response.status_code == HTTP_401_UNAUTHORIZED


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
            AuthenticatedRequests(verifier(), exclude=("/livez",)),
        )

        assert self.applies(app, "GET", "/me") == ("authenticated",)
        assert self.applies(app, "DELETE", "/orders/{order_id}") == (
            "authenticated orders:write",
        )
        assert self.applies(app, "GET", "/reports/export") == (
            "authenticated reports:read reports:export",
        )
        assert self.applies(app, "GET", "/catalog") == ("anonymous",)
        assert self.applies(app, "GET", "/livez") == ()

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

    def test_a_guard_on_one_method_is_reported_on_that_method(self) -> None:
        """A route answering two methods keeps each one's scopes apart."""

        @get("/orders/{order_id:int}")
        async def read(
            order_id: Annotated[int, Parameter()],
        ) -> None: ...  # pragma: no cover

        @delete(
            "/orders/{order_id:int}",
            guards=[LitestarAuthenticated(scopes=["orders:write"])],
        )
        async def remove(
            order_id: Annotated[int, Parameter()],
        ) -> None: ...  # pragma: no cover

        app = Litestar(route_handlers=[read, remove])
        micro = Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        )
        micro.install(app)
        app.state.micro = micro

        assert self.applies(app, "GET", "/orders/{order_id:int}") == (
            "authenticated",
        )
        assert self.applies(app, "DELETE", "/orders/{order_id:int}") == (
            "authenticated orders:write",
        )


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

    def test_an_app_mounted_under_litestar_matches_exclude_as_it_routes(
        self,
    ) -> None:
        """Litestar hands the app `/public/`, which its own router reads whole."""
        inner = FastAPI()

        @inner.get("/public/")
        async def private() -> dict[str, bool]:
            return {"private": True}  # pragma: no cover

        Grelmicro(
            uses=[
                ErrorResponses(),
                AuthenticatedRequests(verifier(), exclude=("/public",)),
            ]
        ).install(inner)

        @asgi("/sub", is_mount=True, copy_scope=False)
        async def sub(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
            await inner(scope, receive, send)

        with LitestarTestClient(Litestar(route_handlers=[sub])) as client:
            response = client.get("/sub/public")

        assert response.status_code == HTTP_401_UNAUTHORIZED

    def test_litestar_middleware_behind_routing_reads_the_routed_path(
        self,
    ) -> None:
        """The root path Litestar's router took off is not taken off twice."""

        @get("/files/{rest:path}")
        async def files(rest: Annotated[str, Parameter()]) -> dict[str, str]:
            return {"private": rest}  # pragma: no cover

        @get("/livez")
        async def livez() -> dict[str, bool]:
            return {"live": True}

        app = Litestar(
            route_handlers=[files, livez],
            middleware=[
                DefineMiddleware(
                    AuthenticatedRequestsMiddleware,  # ty: ignore[invalid-argument-type]
                    verifier=verifier(),
                    exclude=("/livez",),
                )
            ],
        )
        with LitestarTestClient(app, root_path="/v1") as client:
            refused = client.get("/v1/files/v1/livez")
            served = client.get("/v1/livez")

        assert refused.status_code == HTTP_401_UNAUTHORIZED
        assert served.json() == {"live": True}

    def test_litestar_middleware_behind_routing_reads_a_mount_whole(
        self,
    ) -> None:
        """A pattern matches the mount's path, not the path left inside it."""

        @asgi("/other", is_mount=True, copy_scope=False)
        async def other(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
            await JSONResponse({"other": True})(
                scope, receive, send
            )  # pragma: no cover

        @asgi("/assets", is_mount=True, copy_scope=False)
        async def assets(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
            await JSONResponse({"secret": True})(
                scope, receive, send
            )  # pragma: no cover

        app = Litestar(
            route_handlers=[
                other,
                LitestarRouter("/v2", route_handlers=[assets]),
            ],
            middleware=[
                DefineMiddleware(
                    AuthenticatedRequestsMiddleware,  # ty: ignore[invalid-argument-type]
                    verifier=verifier(),
                    exclude=("/livez/*",),
                )
            ],
        )
        with LitestarTestClient(app) as client:
            response = client.get("/v2/assets/livez")

        assert response.status_code == HTTP_401_UNAUTHORIZED

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

    def test_a_public_route_is_redirected_to_without_its_trailing_slash(
        self,
    ) -> None:
        """Starlette's redirect to a public route needs no credential."""
        app = FastAPI()

        @app.get("/catalog/", dependencies=[Anonymous()])
        async def catalog() -> dict[str, bool]:
            return {"catalog": True}

        @app.get("/help/", dependencies=[Anonymous()])
        async def help_page() -> dict[str, bool]:
            return {"help": True}  # pragma: no cover

        @app.post("/help")
        async def ask() -> dict[str, bool]:
            return {"asked": True}  # pragma: no cover

        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)
        client = TestClient(app)

        redirect = client.get("/catalog", follow_redirects=False)

        assert redirect.status_code == HTTP_307_TEMPORARY_REDIRECT
        assert redirect.headers["location"].endswith("/catalog/")
        assert client.get("/catalog").json() == {"catalog": True}
        assert client.get("/help").status_code == HTTP_401_UNAUTHORIZED
        assert client.get("/").status_code == HTTP_401_UNAUTHORIZED
        with (
            pytest.raises(WebSocketDenialResponse),
            client.websocket_connect("/catalog"),
        ):
            pass  # pragma: no cover

    def test_one_registration_on_two_apps_serves_each_by_its_own_routes(
        self,
    ) -> None:
        """A route public on one app never opens the same path on the other."""
        guarded = FastAPI()

        @guarded.get("/admin/{name}")
        async def secret(name: str) -> dict[str, str]:
            return {"secret": name}  # pragma: no cover

        public = FastAPI()

        @public.get("/admin/{name}", dependencies=[Anonymous()])
        async def page(name: str) -> dict[str, str]:
            return {"page": name}

        micro = Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        )
        micro.install(guarded)
        micro.install(public)

        assert TestClient(guarded).get("/admin/x").status_code == (
            HTTP_401_UNAUTHORIZED
        )
        assert TestClient(public).get("/admin/x").json() == {"page": "x"}

    def test_a_request_from_an_app_never_read_is_authenticated(self) -> None:
        """A middleware whose app was never read serves no route publicly."""

        async def page(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
            await JSONResponse({"page": True})(
                scope, receive, send
            )  # pragma: no cover

        middleware, options = AuthenticatedRequests(
            verifier()
        ).asgi_middleware()

        assert TestClient(middleware(page, **options)).get("/").status_code == (
            HTTP_401_UNAUTHORIZED
        )

    def test_a_host_whose_routes_cannot_be_read_keeps_its_paths_authenticated(
        self,
    ) -> None:
        """A `Host` serving another app could answer any path under it."""

        async def admin(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
            await JSONResponse({"admin": True})(
                scope, receive, send
            )  # pragma: no cover

        def build(place: Any) -> FastAPI:  # noqa: ANN401
            app = FastAPI()

            @app.get("/status", dependencies=[Anonymous()])
            async def status() -> dict[str, bool]:
                return {"up": True}

            place(app)
            Grelmicro(
                uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
            ).install(app)
            return app

        admin_host = "http://admin.example.com"
        at_root = build(lambda app: app.host("admin.example.com", admin))
        under = build(
            lambda app: app.mount(
                "/tenants",
                Router(routes=[Host("admin.example.com", app=admin)]),
            )
        )
        looped = build(lambda app: app.mount("/again", app))

        assert (
            TestClient(at_root, base_url=admin_host).get("/status").status_code
            == HTTP_401_UNAUTHORIZED
        )
        assert TestClient(under).get("/status").json() == {"up": True}
        assert (
            TestClient(under, base_url=admin_host).get("/tenants/x").status_code
            == HTTP_401_UNAUTHORIZED
        )
        assert TestClient(looped).get("/status").json() == {"up": True}

    def test_a_mounted_router_never_serves_a_public_path_outside_its_routes(
        self,
    ) -> None:
        """Everything under a mount is its own to answer, its default included."""

        async def secret(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
            await JSONResponse({"secret": True})(
                scope, receive, send
            )  # pragma: no cover

        async def listed(_request: Any) -> JSONResponse:  # noqa: ANN401
            return JSONResponse({"listed": True})  # pragma: no cover

        app = FastAPI()
        app.mount("/sub", Router(routes=[Route("/a", listed)], default=secret))

        @app.get("/sub/x", dependencies=[Anonymous()])
        async def beside() -> dict[str, bool]:
            return {"beside": True}  # pragma: no cover

        @app.get("/status", dependencies=[Anonymous()])
        async def status() -> dict[str, bool]:
            return {"up": True}

        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)
        client = TestClient(app)

        assert client.get("/sub/x").status_code == HTTP_401_UNAUTHORIZED
        assert client.get("/status").json() == {"up": True}

    def test_a_public_route_under_a_host_is_public_only_when_others_get_a_404(
        self,
    ) -> None:
        """A request no `Host` takes falls to its router's default, which decides."""

        async def secret(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
            await JSONResponse({"secret": True})(
                scope, receive, send
            )  # pragma: no cover

        def build(default: Any) -> FastAPI:  # noqa: ANN401
            api = FastAPI()

            @api.get("/x", dependencies=[Anonymous()])
            async def x() -> dict[str, bool]:
                return {"x": True}

            app = FastAPI()
            app.host("api.example.com", api)
            if default is not None:
                app.router.default = default
            Grelmicro(
                uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
            ).install(app)
            return app

        standard = build(None)
        fallback = build(secret)
        api_host = "http://api.example.com"

        assert TestClient(standard, base_url=api_host).get("/x").json() == {
            "x": True
        }
        assert TestClient(standard).get("/x").status_code == HTTP_404_NOT_FOUND
        assert TestClient(fallback).get("/x").status_code == (
            HTTP_401_UNAUTHORIZED
        )

    def test_a_router_that_never_redirects_keeps_the_other_spelling_authenticated(
        self,
    ) -> None:
        """Without the redirect, what answers a missed slash is not the route."""

        async def secret(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
            await JSONResponse({"secret": True})(
                scope, receive, send
            )  # pragma: no cover

        app = FastAPI(redirect_slashes=False)

        @app.get("/catalog", dependencies=[Anonymous()])
        async def catalog() -> dict[str, bool]:
            return {"catalog": True}

        app.router.default = secret
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)
        client = TestClient(app)

        assert client.get("/catalog").json() == {"catalog": True}
        assert client.get("/catalog/").status_code == HTTP_401_UNAUTHORIZED

    def test_a_public_route_a_spanning_mount_never_reaches_stays_authenticated(
        self,
    ) -> None:
        """Starlette splits a mount's path on its own, where a joined path would not."""

        async def secret(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
            await JSONResponse({"secret": True})(
                scope, receive, send
            )  # pragma: no cover

        async def listed() -> dict[str, bool]:
            return {"listed": True}

        def build(mount_path: str, default: Any) -> TestClient:  # noqa: ANN401
            app = FastAPI()
            app.mount(
                mount_path,
                Router(
                    routes=[
                        APIRoute("/a/a", listed, dependencies=[Anonymous()])
                    ],
                    default=default,
                ),
            )
            Grelmicro(
                uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
            ).install(app)
            return TestClient(app)

        spanning = build("/{rest:path}", secret)
        literal = build("/shop", None)

        assert spanning.get("/x/a/a").status_code == HTTP_401_UNAUTHORIZED
        assert literal.get("/shop/a/a").json() == {"listed": True}
        assert literal.get("/elsewhere").status_code == HTTP_401_UNAUTHORIZED

    def test_public_routes_in_a_wrapped_mounted_app_need_no_credential(
        self,
    ) -> None:
        """Middleware of its own, an included router and a default beside it."""

        async def secret(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
            await JSONResponse({"secret": True})(
                scope, receive, send
            )  # pragma: no cover

        sub = FastAPI()

        @sub.get("/status", dependencies=[Anonymous()])
        async def status() -> dict[str, bool]:
            return {"up": True}

        daily = APIRouter()

        @daily.get("/daily", dependencies=[Anonymous()])
        async def report() -> dict[str, bool]:
            return {"daily": True}

        sub.include_router(daily, prefix="/reports")
        sub.add_middleware(GZipMiddleware)
        top = APIRouter()

        @top.get("/news", dependencies=[Anonymous()])
        async def news() -> dict[str, bool]:
            return {"news": True}

        app = FastAPI()
        app.mount("/sub", sub)
        app.include_router(top, prefix="/top")
        app.router.default = secret
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)
        client = TestClient(app)

        assert client.get("/sub/status").json() == {"up": True}
        assert client.get("/sub/reports/daily").json() == {"daily": True}
        assert client.get("/top/news").json() == {"news": True}

    def test_a_host_turning_requests_away_closes_its_included_routes(
        self,
    ) -> None:
        """Through an include, a public route still sits under the host."""

        async def secret(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
            await JSONResponse({"secret": True})(
                scope, receive, send
            )  # pragma: no cover

        versioned = APIRouter()

        @versioned.get("/x", dependencies=[Anonymous()])
        async def x() -> dict[str, bool]:
            return {"x": True}  # pragma: no cover

        api = FastAPI()
        api.include_router(versioned, prefix="/v1")
        app = FastAPI()
        app.host("api.example.com", api)
        app.router.default = secret
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)

        assert TestClient(app).get("/v1/x").status_code == HTTP_401_UNAUTHORIZED

    def test_a_host_inside_a_mounted_router_serves_its_public_route(
        self,
    ) -> None:
        """A plain router answers what no host takes with a `404`."""
        api = FastAPI()

        @api.get("/x", dependencies=[Anonymous()])
        async def x() -> dict[str, bool]:
            return {"x": True}

        app = FastAPI()
        app.mount("/tenants", Router(routes=[Host("api.example.com", app=api)]))
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)
        client = TestClient(app, base_url="http://api.example.com")

        assert client.get("/tenants/x").json() == {"x": True}

    def test_a_route_of_another_kind_keeps_the_app_authenticated(self) -> None:
        """A route with no path and no app could answer anything."""

        class Silent(BaseRoute):
            def matches(self, scope: Any) -> tuple[Match, dict[str, Any]]:  # noqa: ANN401, ARG002
                return Match.NONE, {}

            async def handle(self, scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
                """Answer nothing."""  # pragma: no cover

        app = FastAPI()

        @app.get("/status", dependencies=[Anonymous()])
        async def status() -> dict[str, bool]:
            return {"up": True}  # pragma: no cover

        app.router.routes.append(Silent())
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)

        assert TestClient(app).get("/status").status_code == (
            HTTP_401_UNAUTHORIZED
        )

    def test_apps_mounted_in_each_other_are_read_once(self) -> None:
        """A cycle of mounts ends the reading rather than the process."""
        outer = FastAPI()
        inner = FastAPI()

        @outer.get("/status", dependencies=[Anonymous()])
        async def status() -> dict[str, bool]:
            return {"up": True}

        outer.mount("/inner", inner)
        inner.mount("/outer", outer)
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(outer)

        assert TestClient(outer).get("/status").json() == {"up": True}

    def test_a_public_route_is_redirected_to_without_the_slash_sent(
        self,
    ) -> None:
        """A slash the public route does not have is redirected away."""
        app = FastAPI()

        @app.get("/catalog", dependencies=[Anonymous()])
        async def catalog() -> dict[str, bool]:
            return {"catalog": True}  # pragma: no cover

        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)

        redirect = TestClient(app).get("/catalog/", follow_redirects=False)

        assert redirect.status_code == HTTP_307_TEMPORARY_REDIRECT
        assert redirect.headers["location"].endswith("/catalog")

    def test_a_protected_socket_beside_a_public_one_stays_refused(
        self,
    ) -> None:
        """A websocket route is held against the public one it overlaps."""
        app = FastAPI()

        @app.websocket("/live/me")
        async def mine(
            socket: FastAPIWebSocket,
        ) -> None: ...  # pragma: no cover

        @app.websocket("/live/{room}", dependencies=[Anonymous()])
        async def room(socket: FastAPIWebSocket, room: str) -> None:
            await socket.accept()
            await socket.send_json({"room": room})
            await socket.close()

        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)
        client = TestClient(app)

        with (
            pytest.raises(WebSocketDenialResponse) as refused,
            client.websocket_connect("/live/me"),
        ):
            pass  # pragma: no cover
        with client.websocket_connect("/live/lobby") as socket:
            joined = socket.receive_json()

        assert refused.value.status_code == HTTP_401_UNAUTHORIZED
        assert joined == {"room": "lobby"}

    def test_a_public_route_in_a_mount_wrapped_by_middleware_is_served(
        self,
    ) -> None:
        """Middleware wrapped around a mounted app hides none of its routes."""
        sub = FastAPI()

        @sub.get("/status", dependencies=[Anonymous()])
        async def status() -> dict[str, bool]:
            return {"up": True}

        app = FastAPI()
        app.mount("/sub", GZipMiddleware(sub))
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)

        assert TestClient(app).get("/sub/status").json() == {"up": True}

    def test_a_trailing_newline_never_opens_a_protected_literal_route(
        self,
    ) -> None:
        """Starlette's `$` matches before a final newline, a public route's never."""
        app = FastAPI()

        @app.get("/users/me")
        async def me() -> dict[str, bool]:
            return {"secret": True}  # pragma: no cover

        @app.get("/users/{uid}", dependencies=[Anonymous()])
        async def user(uid: str) -> dict[str, str]:
            return {"user": uid}

        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)
        client = TestClient(app)

        assert client.get("/users/me%0A").status_code == HTTP_401_UNAUTHORIZED
        assert client.get("/users/alice").json() == {"user": "alice"}

    def test_a_root_path_ending_in_a_slash_is_read_as_the_router_reads_it(
        self,
    ) -> None:
        """The route a credential is checked for is the route that answers."""
        app = FastAPI(root_path="/api/")

        @app.get("/x", dependencies=[Anonymous()])
        async def public() -> dict[str, str]:
            return {"route": "public"}  # pragma: no cover

        @app.get("/api/x")
        async def private() -> dict[str, str]:
            return {"route": "private"}  # pragma: no cover

        @app.get("/api/livez")
        async def probe() -> dict[str, bool]:
            return {"live": False}  # pragma: no cover

        Grelmicro(
            uses=[
                ErrorResponses(),
                AuthenticatedRequests(verifier(), exclude=("/livez",)),
            ]
        ).install(app)
        client = TestClient(app)

        assert client.get("/api/x").status_code == HTTP_401_UNAUTHORIZED
        assert client.get("/api/livez").status_code == HTTP_401_UNAUTHORIZED

    def test_a_url_outside_the_root_path_is_matched_as_starlette_matches_it(
        self,
    ) -> None:
        """Beneath a mount, every level reads the whole path again, as Starlette does."""

        async def secret(scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
            await JSONResponse({"secret": True})(
                scope, receive, send
            )  # pragma: no cover

        async def listed() -> dict[str, bool]:
            return {"listed": True}

        app = FastAPI(root_path="/api")
        app.mount(
            "/{tenant}",
            Router(
                routes=[
                    Mount(
                        "/{region}/{zone}",
                        app=Router(
                            routes=[
                                APIRoute(
                                    "/{item}/a/a",
                                    listed,
                                    dependencies=[Anonymous()],
                                )
                            ],
                            default=secret,
                        ),
                    )
                ]
            ),
        )
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)
        client = TestClient(app)

        assert client.get("/x/x/x/x/a/a").status_code == HTTP_401_UNAUTHORIZED
        assert client.get("/api/x/x/x/x/a/a").json() == {"listed": True}

    def test_a_mount_mounted_as_an_app_is_matched_through(self) -> None:
        """A mount holding a mount itself matches by both paths."""

        async def listed() -> dict[str, bool]:
            return {"listed": True}

        app = FastAPI(root_path="/api")
        app.mount(
            "/{tenant}",
            Mount(
                "/{region}",
                routes=[
                    APIRoute("/{item}/a", listed, dependencies=[Anonymous()])
                ],
            ),
        )
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)
        client = TestClient(app)

        assert client.get("/api/x/x/x/a").json() == {"listed": True}
        assert client.get("/x/x/x/a").status_code == HTTP_401_UNAUTHORIZED

    def test_a_route_mounted_as_an_app_is_matched_through(self) -> None:
        """A route mounted in place of an app matches beneath the mount."""

        async def listed() -> dict[str, bool]:
            return {"listed": True}

        app = FastAPI(root_path="/api")
        app.mount(
            "/{tenant}", APIRoute("/{item}", listed, dependencies=[Anonymous()])
        )
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)
        client = TestClient(app)

        assert client.get("/api/x/y").json() == {"listed": True}
        assert client.get("/x/y").status_code == HTTP_401_UNAUTHORIZED

    def test_a_route_of_another_kind_rivals_the_public_routes_beside_it(
        self,
    ) -> None:
        """What such a route answers is unknown, so it may answer any path."""

        class Everything(BaseRoute):
            path = "/{anything:path}"

            def matches(self, scope: Any) -> tuple[Match, dict[str, Any]]:  # noqa: ANN401, ARG002
                return Match.FULL, {}

            async def handle(self, scope: Any, receive: Any, send: Any) -> None:  # noqa: ANN401
                await JSONResponse({"secret": True})(
                    scope, receive, send
                )  # pragma: no cover

        async def listed() -> dict[str, bool]:
            return {"listed": True}  # pragma: no cover

        app = FastAPI()
        app.router.routes.append(Everything())
        app.add_api_route("/open", listed, dependencies=[Anonymous()])
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)

        assert TestClient(app).get("/open").status_code == HTTP_401_UNAUTHORIZED


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

    def test_a_router_included_twice_is_described_per_inclusion(self) -> None:
        """Only the inclusion that declared `Anonymous()` is described public."""
        shared = APIRouter()

        @shared.get("/items")
        async def items() -> dict[str, bool]:
            return {"items": True}

        def declare(app: FastAPI) -> None:
            app.include_router(shared, prefix="/v1")
            app.include_router(shared, prefix="/v2", dependencies=[Anonymous()])

        app = fastapi_app(AuthenticatedRequests(verifier()), declare=declare)
        paths = app.openapi()["paths"]
        client = TestClient(app)

        assert paths["/v1/items"]["get"]["security"] == [{SCHEME: []}]
        assert paths["/v2/items"]["get"]["security"] == [{}, {SCHEME: []}]
        assert self.applies(app, "GET", "/v1/items") == ("authenticated",)
        assert self.applies(app, "GET", "/v2/items") == ("anonymous",)
        assert client.get("/v1/items").status_code == HTTP_401_UNAUTHORIZED
        assert client.get("/v2/items").json() == {"items": True}

    def test_a_public_route_with_a_registered_converter_is_described_public(
        self,
    ) -> None:
        """A sample that fits the converter describes the route as it is served."""

        class Day(Convertor[str]):  # codespell:ignore
            regex = r"\d{4}-\d{2}-\d{2}"

            def convert(self, value: str) -> str:
                return value

            def to_string(self, value: str) -> str:
                return value  # pragma: no cover

        class Nines(Convertor[str]):  # codespell:ignore
            regex = "9{12}"

            def convert(self, value: str) -> str:
                return value  # pragma: no cover

            def to_string(self, value: str) -> str:
                return value  # pragma: no cover

        register_url_convertor("grelmicro_day", Day())
        register_url_convertor("grelmicro_nines", Nines())

        def declare(app: FastAPI) -> None:
            @app.get("/days/{day:grelmicro_day}", dependencies=[Anonymous()])
            async def day(day: str) -> dict[str, str]:
                return {"day": day}

            @app.get(
                "/codes/{code:grelmicro_nines}", dependencies=[Anonymous()]
            )
            async def code(code: str) -> dict[str, str]:
                return {"code": code}  # pragma: no cover

        app = fastapi_app(AuthenticatedRequests(verifier()), declare=declare)
        paths = app.openapi()["paths"]

        assert paths["/days/{day}"]["get"]["security"] == [{}, {SCHEME: []}]
        assert self.applies(app, "GET", "/days/{day:grelmicro_day}") == (
            "anonymous",
        )
        assert TestClient(app).get("/days/2026-01-01").json() == {
            "day": "2026-01-01"
        }
        assert paths["/codes/{code}"]["get"]["security"] == [{SCHEME: []}]

    def test_scopes_a_security_passes_to_the_caller_are_enforced(self) -> None:
        """`Security` around a dependency reading the caller requires its scopes."""

        async def reader(principal: CurrentPrincipal) -> Any:  # noqa: ANN401
            return principal

        async def claims_reader(claims: Claims) -> Any:  # noqa: ANN401
            return claims

        def declare(app: FastAPI) -> None:
            @app.get(
                "/items/mine",
                dependencies=[Security(reader, scopes=["items:read"])],
            )
            async def mine() -> dict[str, bool]:
                return {"mine": True}

            @app.get(
                "/items/signed",
                dependencies=[Security(claims_reader, scopes=["items:sign"])],
            )
            async def signed() -> dict[str, bool]:
                return {"signed": True}  # pragma: no cover

        app = fastapi_app(AuthenticatedRequests(verifier()), declare=declare)
        client = TestClient(app)
        paths = app.openapi()["paths"]

        assert (
            client.get("/items/mine", headers=bearer(token())).status_code
            == HTTP_403_FORBIDDEN
        )
        assert client.get(
            "/items/mine", headers=bearer(token(scope="items:read"))
        ).json() == {"mine": True}
        assert (
            client.get("/items/signed", headers=bearer(token())).status_code
            == HTTP_403_FORBIDDEN
        )
        assert paths["/items/mine"]["get"]["security"] == [
            {SCHEME: ["items:read"]}
        ]
        assert paths["/items/signed"]["get"]["security"] == [
            {SCHEME: ["items:sign"]}
        ]
        assert self.applies(app, "GET", "/items/mine") == (
            "authenticated items:read",
        )

    def test_scopes_of_another_security_stay_off_the_bearer_scheme(
        self,
    ) -> None:
        """Only `Authenticated` and the caller dependencies name its scopes."""

        async def audited(security_scopes: SecurityScopes) -> None:  # noqa: ARG001
            return None

        def declare(app: FastAPI) -> None:
            @app.get(
                "/audit",
                dependencies=[Security(audited, scopes=["audit:read"])],
            )
            async def audit() -> dict[str, bool]:
                return {"audit": True}

        app = fastapi_app(AuthenticatedRequests(verifier()), declare=declare)

        assert app.openapi()["paths"]["/audit"]["get"]["security"] == [
            {SCHEME: []}
        ]
        assert TestClient(app).get(
            "/audit", headers=bearer(token())
        ).json() == {"audit": True}


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

        assert app.openapi()["paths"]["/catalog"]["get"]["security"] == [
            {},
            {SCHEME: []},
        ]
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


RESOURCE = "https://api.example.com/orders"
WELL_KNOWN = "/.well-known/oauth-protected-resource/orders"
METADATA_URL = f"https://api.example.com{WELL_KNOWN}"
ISSUER = "https://auth.grel.info/"


def issuing(issuer: str = ISSUER) -> JWTVerifier:
    """Return a verifier checking `issuer`, and so naming it."""
    return JWTVerifier.keys(
        JWTKey.pem(SIGNER.public_pem("RS256"), algorithm="RS256", kid="k1"),
        audience=AUDIENCE,
        issuer=issuer,
    )


def published(**options: Any) -> FastAPI:  # noqa: ANN401
    """Return a FastAPI app publishing its protected resource metadata."""
    app = FastAPI()

    @app.get("/orders")
    async def orders() -> dict[str, bool]:
        return {"orders": True}  # pragma: no cover

    @app.delete(
        "/orders/{order_id}",
        dependencies=[Authenticated(scopes=["orders:write"])],
    )
    async def cancel(order_id: int) -> dict[str, int]:
        return {"cancelled": order_id}  # pragma: no cover

    @app.websocket("/follow")
    async def follow(websocket: FastAPIWebSocket) -> None:
        await websocket.accept()  # pragma: no cover

    options.setdefault("resource", RESOURCE)
    Grelmicro(
        uses=[ErrorResponses(), AuthenticatedRequests(issuing(), **options)]
    ).install(app)
    return app


class Keyless:
    """A verifier of your own, which names no issuer."""

    def verify(self, token: str) -> Any:  # noqa: ANN401, ARG002
        """Never called: the component is refused before it serves."""
        raise AssertionError  # pragma: no cover


class TestResourceMetadata:
    """Telling a client where to get a token, as RFC 9728 describes."""

    def test_the_document_names_the_resource_and_its_issuer(self) -> None:
        """Public, cacheable, and readable from a browser on any origin."""
        response = TestClient(published()).get(WELL_KNOWN)

        assert response.content == (
            b'{"resource":"https://api.example.com/orders",'
            b'"authorization_servers":["https://auth.grel.info/"],'
            b'"bearer_methods_supported":["header"]}'
        )
        assert response.headers["content-type"] == "application/json"
        assert response.headers["access-control-allow-origin"] == "*"
        assert response.headers["cache-control"] == "public, max-age=3600"

    def test_scopes_are_listed_only_when_passed(self) -> None:
        """The document is public, so no scope name is published unasked."""
        listed = TestClient(
            published(scopes=["orders:read", "orders:write"])
        ).get(WELL_KNOWN)
        unlisted = TestClient(published()).get(WELL_KNOWN)

        assert listed.json()["scopes_supported"] == [
            "orders:read",
            "orders:write",
        ]
        assert "scopes_supported" not in unlisted.json()

    def test_authorization_servers_given_replace_the_issuers(self) -> None:
        """A verifier of your own names none, so they can be given."""
        response = TestClient(
            published(authorization_servers=["https://login.example.com/t1"])
        ).get(WELL_KNOWN)

        assert response.json()["authorization_servers"] == [
            "https://login.example.com/t1"
        ]

    def test_a_resource_at_the_host_is_published_at_the_root(self) -> None:
        """The slash after the host is dropped, and `resource` kept as given."""
        response = TestClient(
            published(resource="https://api.example.com/")
        ).get("/.well-known/oauth-protected-resource")

        assert response.json()["resource"] == "https://api.example.com/"

    def test_a_query_in_the_resource_stays_in_the_metadata_url(self) -> None:
        """The suffix goes between the host and the path, the query after."""
        client = TestClient(
            published(resource="https://api.example.com/orders?tenant=a")
        )

        document = client.get(WELL_KNOWN)
        refused = client.get("/orders")

        assert document.json()["resource"] == (
            "https://api.example.com/orders?tenant=a"
        )
        assert refused.headers["www-authenticate"] == (
            f'Bearer resource_metadata="{METADATA_URL}?tenant=a"'
        )

    def test_every_bearer_challenge_points_at_the_document(self) -> None:
        """The middleware's refusals and a route's alike."""
        client = TestClient(published())
        pointer = f'resource_metadata="{METADATA_URL}"'

        missing = client.get("/orders")
        forged = client.get(
            "/orders", headers=bearer(token(FORGER, iss=ISSUER))
        )
        doubled = client.get(
            "/orders",
            headers=[
                ("authorization", "Bearer a"),
                ("authorization", "Bearer b"),
            ],
        )
        scoped = client.delete("/orders/7", headers=bearer(token(iss=ISSUER)))

        assert missing.headers["www-authenticate"] == f"Bearer {pointer}"
        assert forged.headers["www-authenticate"] == (
            f'Bearer error="invalid_token", {pointer}'
        )
        assert doubled.status_code == HTTP_400_BAD_REQUEST
        assert doubled.headers["www-authenticate"] == (
            f'Bearer error="invalid_request", {pointer}'
        )
        assert scoped.status_code == HTTP_403_FORBIDDEN
        assert scoped.headers["www-authenticate"] == (
            f'Bearer error="insufficient_scope", scope="orders:write", {pointer}'
        )

    def test_a_websocket_denial_points_at_the_document(self) -> None:
        """The denial response carries the same challenge."""
        client = TestClient(published())

        with (
            pytest.raises(WebSocketDenialResponse) as caught,
            client.websocket_connect("/follow"),
        ):
            pass  # pragma: no cover

        assert caught.value.headers["www-authenticate"] == (
            f'Bearer resource_metadata="{METADATA_URL}"'
        )

    def test_a_challenge_in_another_form_is_left_alone(self) -> None:
        """Another scheme, or one naming its own metadata, is not added to."""

        async def basic(request: Request) -> StarletteResponse:  # noqa: ARG001
            return StarletteResponse(
                status_code=HTTP_401_UNAUTHORIZED,
                headers={"www-authenticate": 'Basic realm="files"'},
            )

        async def pointed(request: Request) -> StarletteResponse:  # noqa: ARG001
            return StarletteResponse(
                status_code=HTTP_401_UNAUTHORIZED,
                headers={
                    "www-authenticate": (
                        'Bearer resource_metadata="https://elsewhere.example.com/"'
                    )
                },
            )

        app = Starlette(
            routes=[Route("/basic", basic), Route("/pointed", pointed)]
        )
        Grelmicro(
            uses=[
                ErrorResponses(),
                AuthenticatedRequests(
                    issuing(), resource=RESOURCE, exclude=("/basic", "/pointed")
                ),
            ]
        ).install(app)
        client = TestClient(app)

        assert client.get("/basic").headers["www-authenticate"] == (
            'Basic realm="files"'
        )
        assert client.get("/pointed").headers["www-authenticate"] == (
            'Bearer resource_metadata="https://elsewhere.example.com/"'
        )

    def test_without_a_resource_nothing_is_published(self) -> None:
        """The path is authenticated like any other, and challenges unchanged."""
        app = FastAPI()
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(issuing())]
        ).install(app)

        response = TestClient(app).get(WELL_KNOWN)

        assert response.status_code == HTTP_401_UNAUTHORIZED
        assert response.headers["www-authenticate"] == "Bearer"

    def test_the_document_is_served_whatever_credential_is_sent(self) -> None:
        """No token is read there, so a forged one is neither refused nor counted."""
        response = TestClient(published()).get(
            WELL_KNOWN, headers=bearer(token(FORGER, iss=ISSUER))
        )

        assert response.status_code == HTTP_200_OK

    def test_head_options_and_other_methods(self) -> None:
        """`HEAD` has no body, `OPTIONS` names the methods, the rest are refused."""
        client = TestClient(published())

        document = client.get(WELL_KNOWN)
        head = client.head(WELL_KNOWN)
        options = client.options(WELL_KNOWN)
        post = client.post(WELL_KNOWN)

        assert head.status_code == HTTP_200_OK
        assert head.content == b""
        assert int(head.headers["content-length"]) == len(document.content)
        assert options.status_code == HTTP_204_NO_CONTENT
        assert options.headers["access-control-allow-methods"] == (
            "GET, HEAD, OPTIONS"
        )
        assert options.headers["access-control-allow-origin"] == "*"
        assert post.status_code == HTTP_405_METHOD_NOT_ALLOWED
        assert post.headers["allow"] == "GET, HEAD, OPTIONS"
        assert post.headers["access-control-allow-origin"] == "*"

    def test_starlette_and_litestar_publish_it_too(self) -> None:
        """Litestar's router drops a trailing slash, and the document is still found."""

        async def home(request: Request) -> JSONResponse:  # noqa: ARG001
            return JSONResponse({})  # pragma: no cover

        starlette = Starlette(routes=[Route("/", home)])
        Grelmicro(
            uses=[
                ErrorResponses(),
                AuthenticatedRequests(issuing(), resource=RESOURCE),
            ]
        ).install(starlette)

        @get("/")
        async def index() -> dict[str, bool]:
            return {}  # pragma: no cover

        litestar = Litestar(route_handlers=[index])
        Grelmicro(
            uses=[
                ErrorResponses(),
                AuthenticatedRequests(
                    issuing(), resource="https://api.example.com/orders/"
                ),
            ]
        ).install(litestar)

        with LitestarTestClient(litestar) as client:
            served = client.get(f"{WELL_KNOWN}/")

        assert TestClient(starlette).get(WELL_KNOWN).json()["resource"] == (
            RESOURCE
        )
        assert served.json()["resource"] == "https://api.example.com/orders/"

    def test_the_middleware_added_by_hand_takes_the_same_options(self) -> None:
        """A hand-built stack publishes it the same way."""
        app = Starlette()
        app.add_middleware(
            AuthenticatedRequestsMiddleware,
            verifier=issuing(),
            resource=RESOURCE,
            scopes=("orders:read",),
        )

        response = TestClient(app).get(WELL_KNOWN)

        assert response.json()["scopes_supported"] == ["orders:read"]

    def test_the_schema_describes_the_document(self) -> None:
        """An operation needing nothing, answering the metadata."""
        operation = published().openapi()["paths"][WELL_KNOWN]["get"]

        assert operation["security"] == []
        assert "application/json" in operation["responses"]["200"]["content"]

    @pytest.mark.parametrize(
        "resource",
        [
            "http://api.example.com/orders",
            "https://api.example.com/orders#top",
            "https:///orders",
            'https://api.example.com/"orders"',
            "https://[api.example.com/orders",
            "api.example.com/orders",
        ],
    )
    def test_a_resource_a_client_could_not_use_is_refused(
        self, resource: str
    ) -> None:
        """Not https, a fragment, no host, or a character a header cannot quote."""
        with pytest.raises(SettingsValidationError):
            AuthenticatedRequests(issuing(), resource=resource)

    @pytest.mark.parametrize(
        "server",
        [
            "http://login.example.com/",
            "https://login.example.com/?tenant=a",
            "https://login.example.com/#top",
        ],
    )
    def test_an_authorization_server_that_is_not_an_issuer_is_refused(
        self, server: str
    ) -> None:
        """An issuer identifier is https, with no query and no fragment."""
        with pytest.raises(SettingsValidationError):
            AuthenticatedRequests(
                issuing(), resource=RESOURCE, authorization_servers=[server]
            )

    @pytest.mark.parametrize(
        "options",
        [
            {"scopes": ["orders:read"]},
            {"authorization_servers": ["https://login.example.com/"]},
            {"resource": RESOURCE, "scopes": ["réad"]},
        ],
    )
    def test_what_describes_the_document_is_checked(
        self, options: dict[str, Any]
    ) -> None:
        """Nothing describes a document no resource publishes, and scopes are tokens."""
        with pytest.raises(SettingsValidationError):
            AuthenticatedRequests(issuing(), **options)

    @pytest.mark.parametrize(
        "named",
        [
            verifier,
            lambda: issuing("orders-auth"),
            lambda: issuing("https://auth.grel.info/?tenant=a"),
            Keyless,
        ],
    )
    def test_a_verifier_without_an_issuer_url_needs_authorization_servers(
        self,
        named: Any,  # noqa: ANN401
    ) -> None:
        """Without one, the document could not say where to get a token."""
        with pytest.raises(TypeError, match="authorization_servers="):
            AuthenticatedRequests(named(), resource=RESOURCE)

    def test_a_verifier_of_your_own_publishes_the_servers_given(self) -> None:
        """A verifier naming no issuer is fine once the servers are given."""
        component = AuthenticatedRequests(
            Keyless(), resource=RESOURCE, authorization_servers=[ISSUER]
        )

        assert component.config.authorization_servers == (ISSUER,)

    @pytest.mark.parametrize("parameter", ["authorization_servers", "scopes"])
    def test_names_written_as_one_string_are_refused(
        self, parameter: str
    ) -> None:
        """One string would otherwise read as one name per character."""
        options: dict[str, Any] = {parameter: "orders:read"}

        async def app(scope: Any, receive: Any, send: Any) -> None: ...  # noqa: ANN401  # pragma: no cover

        with pytest.raises(TypeError, match=f"^{parameter}= takes"):
            AuthenticatedRequests(issuing(), resource=RESOURCE, **options)
        with pytest.raises(TypeError, match=f"^{parameter}= takes"):
            AuthenticatedRequestsMiddleware(
                app, verifier=issuing(), resource=RESOURCE, **options
            )


class TestRootPathRedirects:
    """A trailing slash redirect is predicted under a root path, as Starlette makes it."""

    @staticmethod
    def app() -> FastAPI:
        """Return an app served under `/api`, with public reads."""
        app = FastAPI(root_path="/api")

        @app.get("/items", dependencies=[Anonymous()])
        async def items() -> list[str]:
            return []  # pragma: no cover

        @app.get("/orders", dependencies=[Anonymous()])
        async def orders() -> list[str]:
            return []  # pragma: no cover

        @app.post("/orders/")
        async def create() -> None: ...  # pragma: no cover

        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(verifier())]
        ).install(app)
        return app

    def test_a_public_route_missed_by_its_slash_is_redirected(self) -> None:
        """The redirect to the public route is sent without a credential."""
        response = TestClient(self.app()).get(
            "/api/items/", follow_redirects=False
        )

        assert response.status_code == HTTP_307_TEMPORARY_REDIRECT

    def test_a_route_answering_the_slashed_path_keeps_it_authenticated(
        self,
    ) -> None:
        """Starlette answers the route there, so no redirect is predicted."""
        response = TestClient(self.app()).get(
            "/api/orders/", follow_redirects=False
        )

        assert response.status_code == HTTP_401_UNAUTHORIZED


class TestLitestarDeclarations:
    """What a Litestar route declares public, for a report or a schema."""

    def test_the_options_litestar_adds_is_public_beside_a_public_handler(
        self,
    ) -> None:
        """Only where a handler of the route is public."""

        @get("/catalog", opt=LitestarAnonymous())
        async def catalog() -> list[str]:
            return []  # pragma: no cover

        @get("/orders")
        async def orders() -> list[str]:
            return []  # pragma: no cover

        app = Litestar(route_handlers=[catalog, orders], openapi_config=None)
        declared = {
            route.path: _litestar_declares_public(route, "OPTIONS")
            for _, route, _ in walk_routes(app)
        }

        assert declared == {"/catalog": True, "/orders": False}


class TestDocumentOperations:
    """The shared schema annotation, on schemas no framework built."""

    @staticmethod
    def annotate(schema: dict[str, Any], **options: Any) -> dict[str, Any]:  # noqa: ANN401
        """Annotate `schema` for a bearer token, with no route public."""
        errors = ErrorResponses()
        document_operations(
            schema,
            verifier=verifier(),
            bans=False,
            exclude=(),
            public=set(),
            scopes={},
            media_type=errors.media_type,
            model=errors.model,
            **options,
        )
        return schema

    def test_an_operation_after_a_path_level_field_is_annotated(self) -> None:
        """A path item may list shared fields before its operations."""
        schema = self.annotate(
            {"paths": {"/orders": {"parameters": [], "get": {}}}}
        )

        assert schema["paths"]["/orders"]["get"]["security"] == [{SCHEME: []}]

    def test_the_metadata_is_described_on_a_schema_without_paths(self) -> None:
        """The document is described even when nothing else is."""
        schema = self.annotate({}, metadata_path=WELL_KNOWN)

        assert schema["paths"][WELL_KNOWN]["get"]["security"] == []

    def test_paths_around_the_metadata_are_kept_and_annotated(self) -> None:
        """A path after the metadata is still annotated, and none is dropped."""
        schema = self.annotate(
            {
                "paths": {
                    WELL_KNOWN: {"get": {"security": []}},
                    "/orders": {"get": {}},
                }
            },
            metadata_path=WELL_KNOWN,
        )

        assert schema["paths"]["/orders"]["get"]["security"] == [{SCHEME: []}]
        assert schema["paths"][WELL_KNOWN]["get"]["security"] == []

    def test_each_schema_gets_its_own_description_of_the_metadata(self) -> None:
        """Changing one schema's description leaves the next one's alone."""
        first = self.annotate({}, metadata_path=WELL_KNOWN)
        first["paths"][WELL_KNOWN]["get"]["responses"]["200"]["description"] = (
            "changed"
        )

        second = self.annotate({}, metadata_path=WELL_KNOWN)

        assert (
            second["paths"][WELL_KNOWN]["get"]["responses"]["200"][
                "description"
            ]
            == "The protected resource metadata."
        )


class TestResourceMetadataEdges:
    """What a client meets fetching the document, however it gets there."""

    def test_the_schema_keeps_the_document_public_on_every_build(self) -> None:
        """A cached schema annotated again, and another app, stay unchanged."""
        app = published()

        app.openapi()
        rebuilt = app.openapi()["paths"][WELL_KNOWN]["get"]
        other = published().openapi()["paths"][WELL_KNOWN]["get"]

        assert rebuilt["security"] == []
        assert "401" not in rebuilt["responses"]
        assert other["security"] == []
        assert "401" not in other["responses"]

    @pytest.mark.parametrize("segment", ["men%C3%BC", "order%20book"])
    def test_a_percent_encoded_resource_path_is_found(
        self, segment: str
    ) -> None:
        """The request arrives decoded, and the pointer keeps the URL as written."""
        client = TestClient(
            published(resource=f"https://api.example.com/{segment}")
        )

        document = client.get(
            f"/.well-known/oauth-protected-resource/{segment}"
        )
        refused = client.get("/orders")

        assert document.status_code == HTTP_200_OK
        assert refused.headers["www-authenticate"] == (
            "Bearer resource_metadata="
            f'"https://api.example.com/.well-known/oauth-protected-resource/{segment}"'
        )

    def test_a_preflight_is_allowed_the_headers_it_asks_for(self) -> None:
        """A browser client sending its own headers can still read the document."""
        client = TestClient(published())

        asking = client.options(
            WELL_KNOWN,
            headers={
                "origin": "https://app.example.com",
                "access-control-request-method": "GET",
                "access-control-request-headers": "authorization, mcp-protocol-version",
            },
        )
        plain = client.options(WELL_KNOWN)

        assert asking.status_code == HTTP_204_NO_CONTENT
        assert asking.headers["access-control-allow-headers"] == (
            "authorization, mcp-protocol-version"
        )
        assert "access-control-allow-headers" not in plain.headers

    def test_a_request_body_reaches_the_route(self) -> None:
        """Publishing the metadata leaves what the route reads untouched."""
        app = FastAPI()

        @app.post("/echo", dependencies=[Anonymous()])
        async def echo(payload: dict[str, int]) -> dict[str, int]:
            return payload

        Grelmicro(
            uses=[
                ErrorResponses(),
                AuthenticatedRequests(issuing(), resource=RESOURCE),
            ]
        ).install(app)

        response = TestClient(app).post("/echo", json={"items": 3})

        assert response.json() == {"items": 3}


def declared_on_litestar(
    *handlers: Any,  # noqa: ANN401
    **declared: Any,  # noqa: ANN401
) -> Litestar:
    """Return a Litestar app declaring the middleware, publishing metadata."""

    @get("/orders")
    async def orders() -> dict[str, bool]:
        return {"orders": True}  # pragma: no cover

    declared.setdefault("resource", RESOURCE)
    return Litestar(
        route_handlers=[orders, *handlers],
        middleware=[
            DefineMiddleware(
                AuthenticatedRequestsMiddleware,  # ty: ignore[invalid-argument-type]
                verifier=issuing(),
                **declared,
            )
        ],
        openapi_config=None,
    )


class TestResourceMetadataOnLitestar:
    """A middleware Litestar runs behind its router still serves the document."""

    def test_install_adds_the_route_a_declared_middleware_needs(self) -> None:
        """The document is found, and nothing warns."""
        app = declared_on_litestar()
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(issuing())]
        ).install(app)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with LitestarTestClient(app) as client:
                document = client.get(WELL_KNOWN)
                refused = client.get("/orders")

        assert document.json()["resource"] == RESOURCE
        assert refused.headers["www-authenticate"] == (
            f'Bearer resource_metadata="{METADATA_URL}"'
        )
        assert not [
            warning
            for warning in caught
            if issubclass(warning.category, MiddlewarePlacementWarning)
        ]

    def test_the_route_serves_the_document_itself(self) -> None:
        """Whatever reaches the route gets the declared middleware's document."""
        app = declared_on_litestar(
            scopes=("orders:read",),
            authorization_servers=("https://login.example.com/t1",),
        )
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(issuing())]
        ).install(app)
        _, handler, *_ = app.asgi_router.handle_routing(
            path=WELL_KNOWN, method="GET"
        )
        sent: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request"}  # pragma: no cover

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        async def serve() -> None:
            await handler.fn(
                {"type": "http", "method": "GET", "headers": []}, receive, send
            )

        asyncio.run(serve())
        document = json.loads(sent[1]["body"])

        assert sent[0]["status"] == HTTP_200_OK
        assert document["scopes_supported"] == ["orders:read"]
        assert document["authorization_servers"] == [
            "https://login.example.com/t1"
        ]

    def test_a_path_the_app_already_routes_is_left_alone(self) -> None:
        """Install does not register a second route there."""

        @get(WELL_KNOWN)
        async def own() -> dict[str, bool]:
            return {"own": True}  # pragma: no cover

        app = declared_on_litestar(own)
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(issuing())]
        ).install(app)

        with LitestarTestClient(app) as client:
            document = client.get(WELL_KNOWN)

        assert document.json()["resource"] == RESOURCE

    def test_a_middleware_built_by_hand_warns_once_without_the_route(
        self,
    ) -> None:
        """Nothing added the route, so the document would be answered `404`."""
        app = declared_on_litestar()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with LitestarTestClient(app) as client:
                client.get("/orders")
                client.get("/orders")

        placement = [
            warning
            for warning in caught
            if issubclass(warning.category, MiddlewarePlacementWarning)
        ]
        assert len(placement) == 1
        assert f"no route at {WELL_KNOWN}" in str(placement[0].message)

    def test_a_middleware_wrapping_litestar_by_hand_finds_a_slashed_resource(
        self,
    ) -> None:
        """Litestar's router drops the slash, and the document is still served."""

        @get("/orders")
        async def orders() -> dict[str, bool]:
            return {"orders": True}  # pragma: no cover

        app = Litestar(route_handlers=[orders], openapi_config=None)
        app.asgi_handler = cast(
            "Any",
            AuthenticatedRequestsMiddleware(
                cast("Any", app.asgi_handler),
                verifier=issuing(),
                resource="https://api.example.com/orders/",
            ),
        )

        with LitestarTestClient(app) as client:
            response = client.get(f"{WELL_KNOWN}/")

        assert response.json()["resource"] == "https://api.example.com/orders/"

    def test_a_route_of_the_app_at_the_path_needs_no_warning(self) -> None:
        """A hand-built middleware behind a router routing the path stays quiet."""

        @get(WELL_KNOWN)
        async def own() -> dict[str, bool]:
            return {"own": True}  # pragma: no cover

        app = declared_on_litestar(own)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with LitestarTestClient(app) as client:
                refused = client.get("/orders")

        assert refused.status_code == HTTP_401_UNAUTHORIZED
        assert not [
            warning
            for warning in caught
            if issubclass(warning.category, MiddlewarePlacementWarning)
        ]

    def test_a_slashed_resource_is_routed_without_a_warning(self) -> None:
        """Litestar routes the path without its slash, and so it is looked up."""
        app = declared_on_litestar(resource="https://api.example.com/orders/")
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(issuing())]
        ).install(app)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with LitestarTestClient(app) as client:
                document = client.get(f"{WELL_KNOWN}/")
                client.get("/orders")

        assert document.json()["resource"] == "https://api.example.com/orders/"
        assert not [
            warning
            for warning in caught
            if issubclass(warning.category, MiddlewarePlacementWarning)
        ]

    def test_a_path_routed_for_another_method_is_left_alone(self) -> None:
        """Install does not clash with it, and the unreachable document warns."""

        @post(WELL_KNOWN)
        async def own() -> None: ...  # pragma: no cover

        app = declared_on_litestar(own)
        Grelmicro(
            uses=[ErrorResponses(), AuthenticatedRequests(issuing())]
        ).install(app)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with LitestarTestClient(app) as client:
                client.get("/orders")

        assert [
            warning
            for warning in caught
            if issubclass(warning.category, MiddlewarePlacementWarning)
        ]

    def test_a_refusal_a_guard_raises_points_at_the_document(self) -> None:
        """Rendered above the middleware, the challenge still names the metadata."""
        with LitestarTestClient(
            litestar_app(AuthenticatedRequests(issuing(), resource=RESOURCE))
        ) as client:
            refused = client.delete(
                "/orders/7", headers=bearer(token(iss=ISSUER))
            )

        assert refused.status_code == HTTP_403_FORBIDDEN
        assert refused.headers["www-authenticate"] == (
            'Bearer error="insufficient_scope", scope="orders:write", '
            f'resource_metadata="{METADATA_URL}"'
        )

    def test_a_route_refusal_without_metadata_names_none(self) -> None:
        """Nothing is added where no metadata is published."""
        with LitestarTestClient(
            litestar_app(AuthenticatedRequests(issuing()))
        ) as client:
            refused = client.delete(
                "/orders/7", headers=bearer(token(iss=ISSUER))
            )

        assert refused.headers["www-authenticate"] == (
            'Bearer error="insufficient_scope", scope="orders:write"'
        )

    @pytest.mark.parametrize(
        ("refusal", "challenge"),
        [
            (
                lambda: TokenRejectedError(TokenRejectedReason.SIGNATURE),
                'Bearer error="invalid_token"',
            ),
            (AmbiguousCredentialsError, 'Bearer error="invalid_request"'),
        ],
    )
    def test_every_refusal_a_handler_raises_points_at_the_document(
        self,
        refusal: Any,  # noqa: ANN401
        challenge: str,
    ) -> None:
        """Whichever bearer refusal it is, rendered above the middleware."""

        @get("/check", opt=LitestarAnonymous())
        async def check() -> None:
            raise refusal()

        app = Litestar(route_handlers=[check], openapi_config=None)
        Grelmicro(
            uses=[
                ErrorResponses(),
                AuthenticatedRequests(issuing(), resource=RESOURCE),
            ]
        ).install(app)

        with LitestarTestClient(app) as client:
            refused = client.get("/check")

        assert refused.headers["www-authenticate"] == (
            f'{challenge}, resource_metadata="{METADATA_URL}"'
        )
