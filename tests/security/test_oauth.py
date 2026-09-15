"""Outbound tokens against a fake authorization server.

Every test drives `OAuthClient` through `httpx.MockTransport`, so what is
checked is what reaches the wire: the form a token request sends, how the
client authenticates, and how often it asks. Time is a clock the test moves,
so refresh and failure memory are checked at their exact boundaries.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import email.utils
import hashlib
import importlib
import json
import logging
import sys
import threading
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote_plus

import httpx
import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import SecretStr

from grelmicro import Grelmicro
from grelmicro.errors import (
    DependencyNotFoundError,
    OutOfContextError,
    SettingsValidationError,
)
from grelmicro.security import (
    ClientAuth,
    ClientCredentials,
    ClientCredentialsConfig,
    ClientRejectedError,
    OAuthClient,
    OAuthClientConfig,
    TokenExchange,
    TokenExchangeConfig,
    TokenUnavailableError,
    VerifiedToken,
    oauth,
)
from grelmicro.security.principal import _verified_token

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

ISSUER = "https://auth.example.com"
TOKEN_ENDPOINT = "https://auth.example.com/oauth2/token"
RFC8414 = "https://auth.example.com/.well-known/oauth-authorization-server"
OIDC = "https://auth.example.com/.well-known/openid-configuration"
API = "https://payments.internal/charges"
SECRET = "s3cr3t-value"
HOUR = 3600
ASSERTION_LIFETIME = 60
DEFAULT_LIFETIME = 300
FAR_FUTURE = 2_000_000_000

REAL_ASYNC_CLIENT = httpx.AsyncClient
"""The real client class, kept before a test points it at the fake server."""

CORE: Any = importlib.import_module("grelmicro_core")
"""The compiled core, typed the way `grelmicro.security.jwt` reads it."""

P256_KEY = ec.generate_private_key(ec.SECP256R1())
P256_PEM = P256_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
P256_PUBLIC = P256_KEY.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
)


def token_response(value: str, **fields: Any) -> httpx.Response:  # noqa: ANN401
    """Return a successful token response carrying `value`."""
    body = {"access_token": value, "token_type": "Bearer", "expires_in": HOUR}
    body.update(fields)
    return httpx.Response(
        200, json={k: v for k, v in body.items() if v is not None}
    )


def error_response(status: int, **body: Any) -> httpx.Response:  # noqa: ANN401
    """Return an error response with a JSON body."""
    return httpx.Response(status, json=body)


def lost(request: httpx.Request) -> httpx.Response:
    """Lose the connection before any answer."""
    msg = "connection refused"
    raise httpx.ConnectError(msg, request=request)


def timed_out(request: httpx.Request) -> httpx.Response:
    """Time out waiting for the answer."""
    msg = "timed out"
    raise httpx.ReadTimeout(msg, request=request)


class AuthServer:
    """A fake authorization server: metadata and a token endpoint."""

    def __init__(self) -> None:
        """Serve RFC 8414 metadata and issue `token-1`, `token-2`, and so on."""
        self.metadata: dict[str, Any] | None = {
            "issuer": ISSUER,
            "token_endpoint": TOKEN_ENDPOINT,
        }
        self.metadata_url = RFC8414
        self.answers: list[httpx.Response | Callable[..., Any]] = []
        self.requests: list[httpx.Request] = []
        self.issued = 0
        self.gate: asyncio.Event | None = None
        self.metadata_answers: list[httpx.Response | Callable[..., Any]] = []
        self.metadata_gate: asyncio.Event | None = None

    @property
    def token_requests(self) -> list[httpx.Request]:
        """Return the requests the token endpoint received."""
        return [r for r in self.requests if str(r.url) == TOKEN_ENDPOINT]

    def form(self, index: int = -1) -> dict[str, str]:
        """Return the form of one token request."""
        content = self.token_requests[index].content.decode()
        return {
            name: values[0]
            for name, values in parse_qs(
                content, keep_blank_values=True
            ).items()
        }

    async def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer a metadata or a token request."""
        request.read()
        self.requests.append(request)
        url = str(request.url)
        if url in (RFC8414, OIDC):
            return await self._metadata(url, request)
        if url != TOKEN_ENDPOINT:
            return httpx.Response(404)
        return await self._token(request)

    async def _metadata(
        self, url: str, request: httpx.Request
    ) -> httpx.Response:
        """Answer a metadata request, from the queue or with the document."""
        if self.metadata_gate is not None:
            await self.metadata_gate.wait()
        if self.metadata_answers:
            return self._answered(self.metadata_answers.pop(0), request)
        if url != self.metadata_url or self.metadata is None:
            return httpx.Response(404)
        return httpx.Response(200, json=self.metadata)

    async def _token(self, request: httpx.Request) -> httpx.Response:
        """Answer a token request, from the queue or with the next token."""
        if self.gate is not None:
            await self.gate.wait()
        if self.answers:
            return self._answered(self.answers.pop(0), request)
        self.issued += 1
        return token_response(f"token-{self.issued}")

    @staticmethod
    def _answered(
        answer: httpx.Response | Callable[..., Any], request: httpx.Request
    ) -> httpx.Response:
        """Return a queued response, or what a queued callable answers."""
        if isinstance(answer, httpx.Response):
            return answer
        return answer(request)


def assert_fetches(server: AuthServer, count: int) -> None:
    """Assert the token endpoint received exactly `count` requests."""
    assert len(server.token_requests) == count


class Clock:
    """A clock the test moves, standing in for `monotonic` and `time`."""

    def __init__(self) -> None:
        """Start at a fixed monotonic and wall time."""
        self.now = 1_000.0
        self.wall = 1_800_000_000.0

    def monotonic(self) -> float:
        """Return the monotonic time."""
        return self.now

    def time(self) -> float:
        """Return the wall time."""
        return self.wall

    def advance(self, seconds: float) -> None:
        """Move both clocks forward."""
        self.now += seconds
        self.wall += seconds


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> AuthServer:
    """Point every client the module opens at a fake authorization server."""
    fake = AuthServer()

    def factory(**kwargs: Any) -> httpx.AsyncClient:  # noqa: ANN401
        return REAL_ASYNC_CLIENT(
            transport=httpx.MockTransport(fake.handle), **kwargs
        )

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return fake


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Freeze time, and put the refresh jitter at the start of its window."""
    frozen = Clock()
    monkeypatch.setattr(oauth, "monotonic", frozen.monotonic)
    monkeypatch.setattr(oauth, "time", frozen.time)
    monkeypatch.setattr(oauth.random, "uniform", lambda low, _high: low)
    return frozen


def secret_client(**options: Any) -> OAuthClient:  # noqa: ANN401
    """Return a discovering client that authenticates with a secret."""
    options.setdefault("client_auth", ClientAuth.secret(SECRET))
    options.setdefault("env_load", False)
    return OAuthClient.discover(ISSUER, client_id="orders-api", **options)


def payments(client: OAuthClient, **options: Any) -> ClientCredentials:  # noqa: ANN401
    """Return a `ClientCredentials` for the payments API on `client`."""
    options.setdefault("audience", "payments-api")
    return ClientCredentials(
        "payments-api", client=client, env_load=False, **options
    )


def caller(
    value: str = "caller-token", expires_at: int | None = None
) -> VerifiedToken:
    """Return a verified caller token."""
    return _verified_token(value, expires_at)


async def settle() -> None:
    """Let tasks started in the background run to completion."""
    for _ in range(10):
        await asyncio.sleep(0)


def segments(assertion: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the header and claims of a compact JWS."""
    header, claims, _ = assertion.split(".")

    def decoded(segment: str) -> dict[str, Any]:
        padded = segment + "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))

    return decoded(header), decoded(claims)


def basic_pair(request: httpx.Request) -> tuple[str, str]:
    """Return the client ID and secret a Basic header carries, decoded."""
    scheme, _, encoded = request.headers["authorization"].partition(" ")
    assert scheme == "Basic"
    user, _, password = base64.b64decode(encoded).decode().partition(":")
    return unquote_plus(user), unquote_plus(password)


def certificate_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    """Return a self-signed certificate for `key`, as PEM."""
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "orders-api")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


class TestClientAuth:
    """How a client proves who it is, and what is refused when it is built."""

    def test_bare_constructor_is_refused(self) -> None:
        """No method is a default, so there is no bare constructor."""
        with pytest.raises(TypeError, match="no default method"):
            ClientAuth()

    def test_repr_never_shows_the_secret(self) -> None:
        """A secret never reaches a repr."""
        assert SECRET not in repr(ClientAuth.secret(SECRET))

    def test_unknown_method_is_refused(self) -> None:
        """Only Basic and the request body can carry a secret."""
        with pytest.raises(SettingsValidationError, match="basic"):
            ClientAuth.secret(SECRET, method="header")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    def test_key_that_cannot_sign_is_refused_when_built(self) -> None:
        """An EC key refused for RSA fails at startup, not on the first fetch."""
        with pytest.raises(SettingsValidationError, match="RS256"):
            ClientAuth.private_key(P256_PEM, algorithm="RS256")

    def test_certificate_that_is_not_pem_is_refused(self) -> None:
        """A thumbprint is only taken from a PEM certificate."""
        with pytest.raises(SettingsValidationError, match="certificate"):
            ClientAuth.private_key(
                P256_PEM, algorithm="ES256", certificate=b"not a certificate"
            )

    def test_unknown_audience_is_refused(self) -> None:
        """An assertion names the issuer or the token endpoint, nothing else."""
        with pytest.raises(SettingsValidationError, match="audience"):
            ClientAuth.private_key(
                P256_PEM,
                algorithm="ES256",
                audience="everyone",  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            )


class TestConfiguration:
    """What a client reads from code and from the environment."""

    def test_bare_constructor_is_refused(self) -> None:
        """No authorization server is a default."""
        with pytest.raises(TypeError, match="no default authorization server"):
            OAuthClient()

    def test_secret_method_needs_a_secret(self) -> None:
        """A secret method with no secret anywhere is refused, naming the variable."""
        with pytest.raises(SettingsValidationError, match="CLIENT_SECRET"):
            secret_client(client_auth=ClientAuth.secret())

    def test_everything_but_the_method_comes_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The issuer, the client ID and the secret are read from variables."""
        monkeypatch.setenv("GREL_OAUTHCLIENT_ISSUER", ISSUER)
        monkeypatch.setenv("GREL_OAUTHCLIENT_CLIENT_ID", "orders-api")
        monkeypatch.setenv("GREL_OAUTHCLIENT_CLIENT_SECRET", "from-env")

        client = OAuthClient.discover(
            client_auth=ClientAuth.secret(), env_load=True
        )

        assert client.config.issuer == ISSUER
        assert client.config.client_id == "orders-api"
        secret = client.config.client_secret
        assert secret is not None
        assert secret.get_secret_value() == "from-env"

    def test_named_client_reads_its_own_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A named client never reads the default client's variables."""
        monkeypatch.setenv("GREL_OAUTHCLIENT_CLIENT_ID", "default-api")
        monkeypatch.setenv("GREL_OAUTHCLIENT_PARTNER_CLIENT_ID", "partner-api")

        client = OAuthClient.discover(
            ISSUER,
            client_auth=ClientAuth.secret(SECRET),
            name="partner",
            env_load=True,
        )

        assert client.config.client_id == "partner-api"
        assert client.name == "partner"

    def test_secret_is_refused_for_a_signing_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A secret left in the environment is refused, not silently ignored."""
        monkeypatch.setenv("GREL_OAUTHCLIENT_CLIENT_SECRET", "left-over")

        with pytest.raises(SettingsValidationError, match="CLIENT_SECRET"):
            OAuthClient.discover(
                ISSUER,
                client_id="orders-api",
                client_auth=ClientAuth.private_key(P256_PEM, algorithm="ES256"),
                env_load=True,
            )

    def test_issuer_variable_is_refused_for_an_endpoint_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An endpoint client takes its issuer from code only."""
        monkeypatch.setenv("GREL_OAUTHCLIENT_ISSUER", ISSUER)

        with pytest.raises(SettingsValidationError, match="ISSUER"):
            OAuthClient.endpoint(
                TOKEN_ENDPOINT,
                client_id="orders-api",
                client_auth=ClientAuth.secret(SECRET),
                env_load=True,
            )

    def test_token_endpoint_variable_is_refused_for_discovery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A discovering client reads its endpoint from the metadata only."""
        monkeypatch.setenv("GREL_OAUTHCLIENT_TOKEN_ENDPOINT", TOKEN_ENDPOINT)

        with pytest.raises(SettingsValidationError, match="TOKEN_ENDPOINT"):
            secret_client(env_load=True)

    def test_issuer_audience_needs_an_issuer(self) -> None:
        """An assertion naming the issuer cannot be signed without one."""
        with pytest.raises(SettingsValidationError, match="needs an issuer"):
            OAuthClient.endpoint(
                TOKEN_ENDPOINT,
                client_id="orders-api",
                client_auth=ClientAuth.private_key(P256_PEM, algorithm="ES256"),
                env_load=False,
            )

    def test_plain_http_is_refused(self) -> None:
        """A token request never travels over plain HTTP."""
        with pytest.raises(SettingsValidationError):
            OAuthClient.endpoint(
                "http://auth.example.com/token",
                client_id="orders-api",
                client_auth=ClientAuth.secret(SECRET),
                env_load=False,
            )

    def test_a_refused_setting_never_echoes_the_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A validation error from the environment does not repeat a secret."""
        monkeypatch.setenv("GREL_OAUTHCLIENT_CLIENT_SECRET", "leaky-secret")
        monkeypatch.setenv("GREL_OAUTHCLIENT_TIMEOUT", "not-a-number")

        with pytest.raises(SettingsValidationError) as caught:
            secret_client(client_auth=ClientAuth.secret(), env_load=True)

        assert "leaky-secret" not in str(caught.value)

    def test_from_config_takes_the_config_as_it_is(self) -> None:
        """The declarative door reads no variable and checks the method fits."""
        config = OAuthClientConfig(
            token_endpoint=TOKEN_ENDPOINT,
            client_id="orders-api",
            client_secret=SecretStr(SECRET),
        )
        signing = ClientAuth.private_key(
            P256_PEM, algorithm="ES256", audience="token_endpoint"
        )

        client = OAuthClient.from_config(
            config, client_auth=ClientAuth.secret()
        )

        assert client.token_endpoint == TOKEN_ENDPOINT
        with pytest.raises(SettingsValidationError, match="client_secret"):
            OAuthClient.from_config(config, client_auth=signing)


class TestDiscovery:
    """Finding the token endpoint in the issuer's metadata."""

    async def test_rfc8414_metadata_is_read_first(
        self, server: AuthServer
    ) -> None:
        """RFC 8414 answers, so OpenID Connect discovery is never asked."""
        async with secret_client() as client:
            assert client.token_endpoint == TOKEN_ENDPOINT

        assert [str(r.url) for r in server.requests] == [RFC8414]

    async def test_openid_configuration_is_the_fallback(
        self, server: AuthServer
    ) -> None:
        """An issuer publishing only OpenID Connect discovery is found too."""
        server.metadata_url = OIDC

        async with secret_client() as client:
            assert client.token_endpoint == TOKEN_ENDPOINT

    async def test_metadata_for_another_issuer_is_never_used(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """A document answering for another issuer never chooses the endpoint."""
        assert server.metadata is not None
        server.metadata["issuer"] = "https://evil.example.com"

        async with secret_client() as client:
            clock.advance(10)
            with pytest.raises(TokenUnavailableError, match="another issuer"):
                await payments(client).token()
            assert client.token_endpoint is None

    async def test_tenant_placeholder_says_to_name_the_tenant(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """Entra's shared metadata is refused with a message saying why."""
        assert server.metadata is not None
        server.metadata["issuer"] = (
            "https://login.microsoftonline.com/{tenantid}/v2.0"
        )

        async with secret_client() as client:
            clock.advance(10)
            with pytest.raises(TokenUnavailableError, match="tenant by its ID"):
                await payments(client).token()

    async def test_plain_http_token_endpoint_is_refused(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """A token endpoint that is not https is never used."""
        assert server.metadata is not None
        server.metadata["token_endpoint"] = "http://auth.example.com/token"

        async with secret_client() as client:
            clock.advance(10)
            with pytest.raises(TokenUnavailableError, match="https"):
                await payments(client).token()

    async def test_unreachable_metadata_at_startup_does_not_stop_the_app(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """The client opens, fails fast for a while, then discovers again."""
        server.metadata_url = "nowhere"

        async with secret_client() as client:
            with pytest.raises(TokenUnavailableError, match="metadata"):
                await payments(client).token()
            asked = len(server.requests)
            with pytest.raises(TokenUnavailableError):
                await payments(client).token()
            assert len(server.requests) == asked

            server.metadata_url = RFC8414
            clock.advance(6)
            token = await payments(client).token()

        assert token.value == "token-1"


class TestClientCredentials:
    """A token for the service itself."""

    async def test_form_names_the_grant_and_the_target(
        self, server: AuthServer
    ) -> None:
        """Audience, resource and scopes are sent the way the pattern names them."""
        async with secret_client() as client:
            await payments(
                client,
                resource="https://payments.internal",
                scopes=["charges:write", "charges:read"],
            ).token()

        assert server.form() == {
            "grant_type": "client_credentials",
            "audience": "payments-api",
            "resource": "https://payments.internal",
            "scope": "charges:write charges:read",
        }

    async def test_token_is_reused_until_its_refresh_window(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """A second request inside the token's life costs no fetch."""
        async with secret_client() as client:
            pattern = payments(client)
            first = await pattern.token()
            clock.advance(HOUR - 61)
            second = await pattern.token()

        assert first.value == second.value == "token-1"
        assert_fetches(server, 1)

    async def test_secret_goes_in_basic_by_default(
        self, server: AuthServer
    ) -> None:
        """With no method listed, the secret is sent in `Authorization: Basic`."""
        async with secret_client() as client:
            await payments(client).token()

        assert basic_pair(server.token_requests[0]) == ("orders-api", SECRET)
        assert "client_secret" not in server.form()

    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    @given(
        client_id=st.text(min_size=1, max_size=40),
        secret=st.text(min_size=1, max_size=40),
    )
    def test_basic_encoding_round_trips_any_characters(
        self, client_id: str, secret: str
    ) -> None:
        """A colon, a space or a non-ASCII character in either part survives."""
        client = OAuthClient.endpoint(
            TOKEN_ENDPOINT,
            client_id=client_id,
            client_auth=ClientAuth.secret(secret),
            env_load=False,
        )
        form: dict[str, str] = {}
        headers: dict[str, str] = {}

        asyncio.run(client._authenticate(form, headers, TOKEN_ENDPOINT))

        request = httpx.Request("POST", TOKEN_ENDPOINT, headers=headers)
        assert basic_pair(request) == (client_id, secret)

    async def test_secret_goes_in_the_body_when_only_that_is_listed(
        self, server: AuthServer
    ) -> None:
        """A server listing only `client_secret_post` gets the secret in the body."""
        assert server.metadata is not None
        server.metadata["token_endpoint_auth_methods_supported"] = [
            "client_secret_post"
        ]

        async with secret_client() as client:
            await payments(client).token()

        form = server.form()
        assert form["client_id"] == "orders-api"
        assert form["client_secret"] == SECRET
        assert "authorization" not in server.token_requests[0].headers

    async def test_basic_wins_when_both_are_listed(
        self, server: AuthServer
    ) -> None:
        """A server listing both gets Basic."""
        assert server.metadata is not None
        server.metadata["token_endpoint_auth_methods_supported"] = [
            "client_secret_post",
            "client_secret_basic",
        ]

        async with secret_client() as client:
            await payments(client).token()

        assert "client_secret" not in server.form()

    async def test_pinned_method_wins_over_the_metadata(
        self, server: AuthServer
    ) -> None:
        """`method=` decides whatever the server lists."""
        assert server.metadata is not None
        server.metadata["token_endpoint_auth_methods_supported"] = [
            "client_secret_basic"
        ]

        async with secret_client(
            client_auth=ClientAuth.secret(SECRET, method="post")
        ) as client:
            await payments(client).token()

        assert server.form()["client_secret"] == SECRET

    async def test_token_carries_what_the_server_granted(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """Scopes, the scheme and the expiry are read from the response."""
        server.answers.append(
            token_response("granted", scope="a b", token_type="bearer")
        )

        async with secret_client() as client:
            token = await payments(client).token()

        assert token.value == "granted"
        assert token.token_type == "Bearer"
        assert token.scopes == frozenset({"a", "b"})
        assert token.expires_at == int(clock.wall) + HOUR
        assert "granted" not in repr(token)

    async def test_missing_expires_in_takes_the_default_lifetime(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """A response with no `expires_in` lives `default_lifetime`, never forever."""
        server.answers.append(token_response("short", expires_in=None))

        async with secret_client() as client:
            token = await payments(client).token()

        assert token.expires_at == int(clock.wall) + DEFAULT_LIFETIME

    async def test_refresh_serves_the_cached_token_and_fetches_once(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """Inside the window, callers get the cached token while one fetch runs."""
        async with secret_client() as client:
            pattern = payments(client)
            await pattern.token()
            clock.advance(HOUR - 60)

            served = await asyncio.gather(*(pattern.token() for _ in range(5)))
            await settle()
            refreshed = await pattern.token()

        assert {token.value for token in served} == {"token-1"}
        assert refreshed.value == "token-2"
        assert_fetches(server, 2)

    async def test_refresh_window_is_at_most_half_the_lifetime(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """A sixty second token refreshes after thirty, not at once."""
        server.answers.append(token_response("brief", expires_in=60))

        async with secret_client() as client:
            pattern = payments(client)
            await pattern.token()
            clock.advance(29)
            await pattern.token()
            await settle()
            assert_fetches(server, 1)

            clock.advance(2)
            await pattern.token()
            await settle()

        assert_fetches(server, 2)

    async def test_server_refresh_in_decides_the_window(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """A `refresh_in` the server sends is followed."""
        server.answers.append(token_response("hinted", refresh_in=100))

        async with secret_client() as client:
            pattern = payments(client)
            await pattern.token()
            clock.advance(101)
            await pattern.token()
            await settle()

        assert_fetches(server, 2)

    async def test_concurrent_callers_share_one_fetch(
        self, server: AuthServer
    ) -> None:
        """A burst of requests with no token cached costs one fetch."""
        async with secret_client() as client:
            pattern = payments(client)
            server.gate = asyncio.Event()
            waiting = [asyncio.create_task(pattern.token()) for _ in range(20)]
            await settle()
            server.gate.set()
            tokens = await asyncio.gather(*waiting)

        assert {token.value for token in tokens} == {"token-1"}
        assert_fetches(server, 1)

    async def test_a_cancelled_caller_does_not_cancel_the_fetch(
        self, server: AuthServer
    ) -> None:
        """The fetch belongs to the client, so the others still get their token."""
        async with secret_client() as client:
            pattern = payments(client)
            server.gate = asyncio.Event()
            first = asyncio.create_task(pattern.token())
            second = asyncio.create_task(pattern.token())
            await settle()
            first.cancel()
            await settle()
            server.gate.set()

            token = await second

        assert token.value == "token-1"
        assert first.cancelled()
        assert_fetches(server, 1)


class TestFailures:
    """What is remembered when the authorization server fails, and for whom."""

    async def test_server_error_is_remembered_for_the_whole_client(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """A server that fails fails every token at once, for `retry_interval`."""
        server.answers.append(httpx.Response(500))

        async with secret_client() as client:
            with pytest.raises(TokenUnavailableError, match="500"):
                await payments(client).token()
            with pytest.raises(TokenUnavailableError):
                await payments(client, audience="other-api").token()
            assert_fetches(server, 1)

            clock.advance(5)
            token = await payments(client).token()

        assert token.value == "token-1"

    async def test_retry_after_is_honoured_and_capped_at_an_hour(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """A server asking for a day of quiet gets an hour."""
        server.answers.append(
            httpx.Response(429, headers={"Retry-After": "86400"})
        )

        async with secret_client() as client:
            pattern = payments(client)
            with pytest.raises(TokenUnavailableError, match="429"):
                await pattern.token()
            clock.advance(HOUR - 1)
            with pytest.raises(TokenUnavailableError):
                await pattern.token()
            assert_fetches(server, 1)

            clock.advance(1)
            await pattern.token()

        assert_fetches(server, 2)

    async def test_retry_after_as_a_date_is_read(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """An HTTP date in `Retry-After` is honoured like a number of seconds."""
        when = email.utils.formatdate(clock.wall + 120, usegmt=True)
        server.answers.append(
            httpx.Response(503, headers={"Retry-After": when})
        )

        async with secret_client() as client:
            pattern = payments(client)
            with pytest.raises(TokenUnavailableError):
                await pattern.token()
            clock.advance(119)
            with pytest.raises(TokenUnavailableError):
                await pattern.token()
            clock.advance(2)
            await pattern.token()

        assert_fetches(server, 2)

    async def test_refusal_of_one_token_is_remembered_for_that_token_only(
        self, server: AuthServer
    ) -> None:
        """One user's refused exchange does not block the next user."""
        server.answers.append(error_response(400, error="invalid_grant"))

        async with secret_client() as client:
            exchange = TokenExchange(
                "payments-api",
                audience="payments-api",
                client=client,
                env_load=False,
            )
            refused = caller("refused-user", expires_at=FAR_FUTURE)
            with pytest.raises(TokenUnavailableError) as caught:
                await exchange.token(refused)
            assert caught.value.error == "invalid_grant"
            with pytest.raises(TokenUnavailableError):
                await exchange.token(refused)

            token = await exchange.token(
                caller("next-user", expires_at=FAR_FUTURE)
            )

        assert token.value == "token-1"
        assert_fetches(server, 2)

    async def test_refused_client_is_its_own_error_and_a_security_event(
        self, server: AuthServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`invalid_client` names the client, keeps the description, logs an event."""
        server.answers.append(
            error_response(
                401,
                error="invalid_client",
                error_description=f"secret {SECRET} is wrong",
            )
        )

        with caplog.at_level(
            logging.WARNING, logger="grelmicro.security.events"
        ):
            async with secret_client() as client:
                with pytest.raises(ClientRejectedError) as caught:
                    await payments(client).token()

        error = caught.value
        assert error.error == "invalid_client"
        assert error.description is not None
        assert SECRET not in str(error)
        events = [
            record
            for record in caplog.records
            if record.name == "grelmicro.security.events"
        ]
        assert len(events) == 1
        action = getattr(events[0], "event.action")
        assert action == "grelmicro.oauth_client.rejected"
        assert SECRET not in events[0].getMessage()

    async def test_refused_signed_assertion_suggests_the_token_endpoint(
        self, server: AuthServer
    ) -> None:
        """An issuer-audience assertion refused hints at what Okta and Entra want."""
        server.answers.append(error_response(401, error="invalid_client"))
        client = OAuthClient.discover(
            ISSUER,
            client_id="orders-api",
            client_auth=ClientAuth.private_key(P256_PEM, algorithm="ES256"),
            env_load=False,
        )

        async with client:
            with pytest.raises(
                ClientRejectedError, match='audience="token_endpoint"'
            ):
                await payments(client).token()

    async def test_lost_connection_is_retried_once(
        self, server: AuthServer
    ) -> None:
        """A keep-alive connection closed under the request costs one retry."""
        server.answers.append(lost)

        async with secret_client() as client:
            token = await payments(client).token()

        assert token.value == "token-1"
        assert_fetches(server, 2)

    async def test_connection_lost_twice_fails(
        self, server: AuthServer
    ) -> None:
        """The retry happens once."""
        server.answers.extend([lost, lost])

        async with secret_client() as client:
            with pytest.raises(TokenUnavailableError, match="ConnectError"):
                await payments(client).token()

        assert_fetches(server, 2)

    async def test_timeout_is_not_retried(self, server: AuthServer) -> None:
        """A timeout already spent the wait."""
        server.answers.append(timed_out)

        async with secret_client() as client:
            with pytest.raises(TokenUnavailableError, match="ReadTimeout"):
                await payments(client).token()

        assert_fetches(server, 1)

    async def test_oversized_response_is_abandoned(
        self, server: AuthServer
    ) -> None:
        """A body past `max_bytes` is refused while it is read."""
        server.answers.append(httpx.Response(200, content=b"x" * 500))
        config = OAuthClientConfig(
            token_endpoint=TOKEN_ENDPOINT,
            client_id="orders-api",
            client_secret=SecretStr(SECRET),
            max_bytes=100,
        )

        async with OAuthClient.from_config(
            config, client_auth=ClientAuth.secret()
        ) as client:
            with pytest.raises(
                TokenUnavailableError, match="more than 100 bytes"
            ):
                await payments(client).token()

    async def test_a_token_type_other_than_bearer_is_refused(
        self, server: AuthServer
    ) -> None:
        """A DPoP token is never sent as a bearer token."""
        server.answers.append(token_response("bound", token_type="DPoP"))

        async with secret_client() as client:
            with pytest.raises(TokenUnavailableError, match="Bearer"):
                await payments(client).token()

    async def test_error_code_outside_the_rfc_charset_is_not_trusted(
        self, server: AuthServer
    ) -> None:
        """A code carrying a line break is treated as a failed response."""
        server.answers.append(error_response(400, error="bad\ncode"))

        async with secret_client() as client:
            with pytest.raises(TokenUnavailableError) as caught:
                await payments(client).token()

        assert caught.value.error is None
        assert "\n" not in str(caught.value)

    async def test_a_failed_refresh_keeps_serving_the_cached_token(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """The cached token is served until it expires, whatever the refresh did."""
        async with secret_client() as client:
            pattern = payments(client)
            await pattern.token()
            clock.advance(HOUR - 60)
            server.answers.append(httpx.Response(500))

            during = await pattern.token()
            await settle()
            after = await pattern.token()

        assert during.value == after.value == "token-1"


class TestSignedAssertion:
    """A client that proves who it is with its private key."""

    def signing_client(self, **auth: Any) -> OAuthClient:  # noqa: ANN401
        """Return a discovering client signing with the P-256 key."""
        auth.setdefault("algorithm", "ES256")
        return OAuthClient.discover(
            ISSUER,
            client_id="orders-api",
            client_auth=ClientAuth.private_key(P256_PEM, **auth),
            env_load=False,
        )

    async def test_assertion_names_the_issuer_and_verifies(
        self, server: AuthServer
    ) -> None:
        """RFC 7523bis: the issuer as the only audience, and the assertion type."""
        async with self.signing_client(kid="orders-2026") as client:
            await payments(client).token()

        form = server.form()
        assert form["client_assertion_type"] == (
            "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        )
        assert form["client_id"] == "orders-api"
        verifier = CORE.Verifier(
            [("orders-2026", "ES256", P256_PUBLIC, "pem")],
            audience=[ISSUER],
            issuer=["orders-api"],
            required=["exp", "iat", "nbf", "jti", "sub"],
            token_type="client-authentication+jwt",
        )
        claims = verifier.verify(form["client_assertion"])
        assert claims["sub"] == "orders-api"
        assert claims["aud"] == ISSUER
        assert claims["exp"] - claims["iat"] == ASSERTION_LIFETIME

    async def test_token_endpoint_audience_carries_no_type(
        self, server: AuthServer
    ) -> None:
        """An assertion naming the endpoint does not promise the new rules."""
        async with self.signing_client(audience="token_endpoint") as client:
            await payments(client).token()

        header, claims = segments(server.form()["client_assertion"])
        assert "typ" not in header
        assert claims["aud"] == TOKEN_ENDPOINT

    async def test_every_fetch_signs_a_new_jti(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """An assertion is never reused."""
        async with self.signing_client() as client:
            pattern = payments(client)
            await pattern.token()
            clock.advance(HOUR + 1)
            await pattern.token()

        first = segments(server.form(0)["client_assertion"])[1]
        second = segments(server.form(1)["client_assertion"])[1]
        assert first["jti"] != second["jti"]

    async def test_retry_after_a_lost_connection_signs_again(
        self, server: AuthServer
    ) -> None:
        """A server that may have seen the first assertion never sees its jti twice."""
        server.answers.append(lost)

        async with self.signing_client() as client:
            await payments(client).token()

        first = segments(server.form(0)["client_assertion"])[1]
        second = segments(server.form(1)["client_assertion"])[1]
        assert first["jti"] != second["jti"]

    async def test_certificate_thumbprint_is_sent(
        self, server: AuthServer
    ) -> None:
        """`certificate=` adds the SHA-256 thumbprint Entra finds the key by."""
        pem = certificate_pem(P256_KEY)
        der = x509.load_pem_x509_certificate(pem).public_bytes(
            serialization.Encoding.DER
        )
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(der).digest())
            .rstrip(b"=")
            .decode()
        )

        async with self.signing_client(certificate=pem) as client:
            await payments(client).token()

        header, _ = segments(server.form()["client_assertion"])
        assert header["x5t#S256"] == expected
        assert "x5t" not in header


class TestAssertionFile:
    """A client that sends an assertion the platform writes to a file."""

    async def test_file_is_read_again_on_every_fetch(
        self, server: AuthServer, clock: Clock, tmp_path: Path
    ) -> None:
        """A token the kubelet rotated in place is the one sent."""
        path = tmp_path / "token"
        path.write_text("assertion-1\n")
        client = OAuthClient.discover(
            ISSUER,
            client_id="orders-api",
            client_auth=ClientAuth.assertion_file(path),
            env_load=False,
        )

        async with client:
            pattern = payments(client)
            await pattern.token()
            path.write_text("assertion-2")
            clock.advance(HOUR + 1)
            await pattern.token()

        assert server.form(0)["client_assertion"] == "assertion-1"
        assert server.form(1)["client_assertion"] == "assertion-2"

    async def test_missing_file_fails_the_fetch(
        self, server: AuthServer, tmp_path: Path
    ) -> None:
        """A file that is not there fails the request, naming the error type only."""
        client = OAuthClient.discover(
            ISSUER,
            client_id="orders-api",
            client_auth=ClientAuth.assertion_file(tmp_path / "missing"),
            env_load=False,
        )

        async with client:
            with pytest.raises(
                TokenUnavailableError, match="FileNotFoundError"
            ):
                await payments(client).token()

        assert server.token_requests == []

    def test_path_comes_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The path, such as `$AZURE_FEDERATED_TOKEN_FILE`, is read from a variable."""
        monkeypatch.setenv(
            "GREL_OAUTHCLIENT_ASSERTION_FILE", str(tmp_path / "t")
        )

        client = OAuthClient.discover(
            ISSUER,
            client_id="orders-api",
            client_auth=ClientAuth.assertion_file(),
            env_load=True,
        )

        assert client.config.assertion_file == str(tmp_path / "t")


class TestTokenExchange:
    """A token for another API, exchanged for the caller's verified token."""

    def exchange(self, client: OAuthClient, **options: Any) -> TokenExchange:  # noqa: ANN401
        """Return an RFC 8693 exchange for the payments API."""
        options.setdefault("audience", "payments-api")
        return TokenExchange(
            "payments-api", client=client, env_load=False, **options
        )

    async def test_a_token_string_is_refused(self, server: AuthServer) -> None:
        """Only a verified token can be exchanged."""
        async with secret_client() as client:
            exchange = self.exchange(client)
            with pytest.raises(TypeError, match="VerifiedToken"):
                await exchange.token("raw-token")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            with pytest.raises(TypeError, match="VerifiedToken"):
                exchange.auth("raw-token")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

        assert server.token_requests == []

    async def test_rfc8693_form(self, server: AuthServer) -> None:
        """The caller's token is the subject, typed as an access token."""
        async with secret_client() as client:
            await self.exchange(client).token(caller(expires_at=FAR_FUTURE))

        access_token = "urn:ietf:params:oauth:token-type:access_token"
        assert server.form() == {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": "caller-token",
            "subject_token_type": access_token,
            "requested_token_type": access_token,
            "audience": "payments-api",
        }

    async def test_on_behalf_of_form(self, server: AuthServer) -> None:
        """The on-behalf-of grant sends the caller's token as the assertion."""
        async with secret_client() as client:
            exchange = TokenExchange.on_behalf_of(
                "payments-api",
                scopes="api://payments/.default",
                client=client,
                env_load=False,
            )
            await exchange.token(caller(expires_at=FAR_FUTURE))

        assert server.form() == {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": "caller-token",
            "requested_token_use": "on_behalf_of",
            "scope": "api://payments/.default",
        }

    def test_on_behalf_of_refuses_an_audience_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The on-behalf-of grant names the API with scopes only."""
        monkeypatch.setenv(
            "GREL_TOKENEXCHANGE_PAYMENTS_API_AUDIENCE", "payments"
        )

        with pytest.raises(SettingsValidationError, match="scopes"):
            TokenExchange.on_behalf_of("payments-api", env_load=True)

    async def test_cached_per_caller(self, server: AuthServer) -> None:
        """The same caller costs one exchange, another caller costs another."""
        async with secret_client() as client:
            exchange = self.exchange(client)
            alice = caller("alice", expires_at=FAR_FUTURE)
            first = await exchange.token(alice)
            again = await exchange.token(alice)
            bob = await exchange.token(caller("bob", expires_at=FAR_FUTURE))

        assert first.value == again.value == "token-1"
        assert bob.value == "token-2"
        assert_fetches(server, 2)

    async def test_never_outlives_the_callers_token(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """A token issued for a user expires when the user's token does."""
        async with secret_client() as client:
            exchange = self.exchange(client)
            subject = caller(expires_at=int(clock.wall) + 100)
            token = await exchange.token(subject)
            clock.advance(101)
            server.answers.append(error_response(400, error="invalid_grant"))
            with pytest.raises(TokenUnavailableError):
                await exchange.token(subject)

        assert token.expires_at == int(clock.wall) - 1
        assert_fetches(server, 2)

    async def test_caller_without_expiry_is_not_cached(
        self, server: AuthServer
    ) -> None:
        """Nothing bounds a caller with no expiry, so every call exchanges."""
        async with secret_client() as client:
            exchange = self.exchange(client)
            subject = caller(expires_at=None)
            await exchange.token(subject)
            await exchange.token(subject)

        assert_fetches(server, 2)

    async def test_cache_size_zero_turns_the_cache_off(
        self, server: AuthServer
    ) -> None:
        """No exchanged token is held."""
        async with secret_client() as client:
            exchange = self.exchange(client, cache_size=0)
            subject = caller(expires_at=FAR_FUTURE)
            await exchange.token(subject)
            await exchange.token(subject)

        assert_fetches(server, 2)


def api_client(
    auth: Any,  # noqa: ANN401
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    """Return a real client calling a fake API through `auth`."""
    return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), auth=auth)


class FakeAPI:
    """A fake downstream API that records the tokens it was sent."""

    def __init__(
        self, *statuses: int, challenge: str = 'Bearer error="invalid_token"'
    ) -> None:
        """Answer `statuses` in order, then `200`."""
        self.statuses = list(statuses)
        self.challenge = challenge
        self.tokens: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record the token, and answer the next status."""
        self.tokens.append(request.headers.get("authorization", ""))
        status = self.statuses.pop(0) if self.statuses else httpx.codes.OK
        headers = (
            {"WWW-Authenticate": self.challenge}
            if status == httpx.codes.UNAUTHORIZED
            else {}
        )
        return httpx.Response(status, headers=headers)


@pytest.mark.usefixtures("server")
class TestAuth:
    """Sending the token with `httpx`, and handling an API that refuses it."""

    async def test_bearer_token_is_sent(self) -> None:
        """Every request carries the token."""
        api = FakeAPI()

        async with (
            secret_client() as client,
            api_client(payments(client).auth(), api) as http,
        ):
            response = await http.get(API)

        assert response.status_code == httpx.codes.OK
        assert api.tokens == ["Bearer token-1"]

    async def test_refused_get_is_sent_again_once_with_a_new_token(
        self,
    ) -> None:
        """A safe request refused with `invalid_token` gets one more try."""
        api = FakeAPI(httpx.codes.UNAUTHORIZED)

        async with (
            secret_client() as client,
            api_client(payments(client).auth(), api) as http,
        ):
            response = await http.get(API)

        assert response.status_code == httpx.codes.OK
        assert api.tokens == ["Bearer token-1", "Bearer token-2"]

    async def test_refused_post_is_not_sent_again_but_the_token_is_dropped(
        self,
    ) -> None:
        """A `401` does not prove the request was not applied."""
        api = FakeAPI(httpx.codes.UNAUTHORIZED)

        async with (
            secret_client() as client,
            api_client(payments(client).auth(), api) as http,
        ):
            refused = await http.post(API, json={"amount": 100})
            after = await http.get(API)

        assert refused.status_code == httpx.codes.UNAUTHORIZED
        assert after.status_code == httpx.codes.OK
        assert api.tokens == ["Bearer token-1", "Bearer token-2"]

    async def test_other_401_is_left_alone(self, server: AuthServer) -> None:
        """A challenge that does not refuse the token keeps it."""
        api = FakeAPI(
            httpx.codes.UNAUTHORIZED, challenge='Bearer realm="payments"'
        )

        async with (
            secret_client() as client,
            api_client(payments(client).auth(), api) as http,
        ):
            response = await http.get(API)

        assert response.status_code == httpx.codes.UNAUTHORIZED
        assert_fetches(server, 1)

    async def test_a_body_not_held_in_memory_is_never_sent_again(
        self,
    ) -> None:
        """A streamed body can be sent once, so only a buffered one is resent.

        Checked on the request itself, on both httpx lines. A mock transport
        reads the body into memory before answering, which a real one does
        not, so a round trip through it would make every body replayable.
        """

        async def body() -> AsyncIterator[bytes]:
            yield b"chunk"

        for module in (httpx, httpx2):
            streamed = module.Request("PUT", API, content=body())
            buffered = module.Request("PUT", API, json={"amount": 100})

            assert not oauth._replayable(streamed)
            assert oauth._replayable(buffered)
            await streamed.aread()
            assert oauth._replayable(streamed)

    async def test_resent_only_once(self) -> None:
        """An API that keeps refusing gets its refusal back after one retry."""
        api = FakeAPI(
            httpx.codes.UNAUTHORIZED,
            httpx.codes.UNAUTHORIZED,
            httpx.codes.UNAUTHORIZED,
        )

        async with (
            secret_client() as client,
            api_client(payments(client).auth(), api) as http,
        ):
            response = await http.get(API)

        assert response.status_code == httpx.codes.UNAUTHORIZED
        assert api.tokens == ["Bearer token-1", "Bearer token-2"]

    async def test_httpx2_client_accepts_it(self) -> None:
        """One auth object works on both httpx lines."""
        seen: list[str] = []

        def handler(request: Any) -> Any:  # noqa: ANN401
            seen.append(request.headers["authorization"])
            return httpx2.Response(200)

        async with (
            secret_client() as client,
            httpx2.AsyncClient(
                transport=httpx2.MockTransport(handler),
                auth=payments(client).auth(),
            ) as http,
        ):
            await http.get(API)

        assert seen == ["Bearer token-1"]

    def test_sync_client_is_refused(self) -> None:
        """A synchronous client cannot await a shared fetch."""
        auth = payments(secret_client()).auth()

        with (
            httpx.Client(
                transport=httpx.MockTransport(FakeAPI()), auth=auth
            ) as http,
            pytest.raises(RuntimeError, match="AsyncClient"),
        ):
            http.get(API)

    async def test_exchange_auth_is_passed_per_request(self) -> None:
        """Each request carries the token exchanged for its own caller."""
        api = FakeAPI()

        async with (
            secret_client() as client,
            REAL_ASYNC_CLIENT(transport=httpx.MockTransport(api)) as http,
        ):
            exchange = TokenExchange(
                "payments-api",
                audience="payments-api",
                client=client,
                env_load=False,
            )
            await http.get(API, auth=exchange.auth(caller("alice", FAR_FUTURE)))
            await http.get(API, auth=exchange.auth(caller("bob", FAR_FUTURE)))

        assert api.tokens == ["Bearer token-1", "Bearer token-2"]


class TestLifecycle:
    """Opening, resolving, closing and reconfiguring a client."""

    async def test_a_client_that_is_not_open_says_so(self) -> None:
        """A token request before opening names the fix."""
        with pytest.raises(OutOfContextError, match="not open"):
            await payments(secret_client()).token()

    @pytest.mark.usefixtures("server")
    async def test_pattern_finds_the_client_through_the_app(self) -> None:
        """A registered client is found the way `Lock` finds its backend."""
        micro = Grelmicro(uses=[secret_client()])
        pattern = ClientCredentials(
            "payments-api", audience="payments-api", env_load=False
        )

        async with micro:
            token = await pattern.token()

        assert token.value == "token-1"

    @pytest.mark.usefixtures("server")
    async def test_pattern_finds_a_named_client(self) -> None:
        """`client=` names a second registered client."""
        micro = Grelmicro(uses=[secret_client(name="partner")])
        pattern = ClientCredentials(
            "payments-api",
            audience="payments-api",
            client="partner",
            env_load=False,
        )

        async with micro:
            token = await pattern.token()

        assert token.value == "token-1"

    async def test_no_client_registered_says_so(self) -> None:
        """A pattern with nothing to resolve names every way to fix it."""
        pattern = ClientCredentials(
            "payments-api", audience="payments-api", env_load=False
        )

        with pytest.raises(OutOfContextError, match="found no OAuthClient"):
            await pattern.token()

    @pytest.mark.usefixtures("server")
    async def test_a_call_from_another_event_loop_is_served(self) -> None:
        """A synchronous handler going through a thread still gets its token."""
        results: list[str] = []

        async with secret_client() as client:
            pattern = payments(client)

            def run() -> None:
                results.append(asyncio.run(pattern.token()).value)

            thread = threading.Thread(target=run)
            thread.start()
            await asyncio.to_thread(thread.join)

        assert results == ["token-1"]

    async def test_closing_abandons_a_fetch_in_flight(
        self, server: AuthServer
    ) -> None:
        """A client closing while a fetch waits does not wait with it."""
        client = secret_client()
        await client.__aenter__()
        server.gate = asyncio.Event()
        waiting = asyncio.create_task(payments(client).token())
        await settle()

        await asyncio.wait_for(client.__aexit__(None, None, None), timeout=1)

        with pytest.raises(asyncio.CancelledError):
            await waiting

    async def test_a_rotated_secret_applies_to_the_next_fetch(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """A secret reloaded from a mounted Secret is sent without a restart."""
        async with secret_client() as client:
            pattern = payments(client)
            await pattern.token()
            await client.reconfigure(
                client.config.model_copy(
                    update={"client_secret": SecretStr("rotated")}
                )
            )
            clock.advance(HOUR + 1)
            await pattern.token()

        assert basic_pair(server.token_requests[0])[1] == SECRET
        assert basic_pair(server.token_requests[1])[1] == "rotated"

    async def test_reconfigure_refuses_a_new_client_id(self) -> None:
        """Who the service is only applies at startup."""
        client = secret_client()

        with pytest.raises(ValueError, match="client_id"):
            await client.reconfigure(
                client.config.model_copy(update={"client_id": "someone-else"})
            )

    def test_rsa_signing_client_can_be_built(self) -> None:
        """An RSA key signs under PS256, as Entra recommends."""
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )

        auth = ClientAuth.private_key(
            pem, algorithm="PS256", audience="token_endpoint"
        )

        assert "PS256" in repr(auth)


class TestEdges:
    """Every refusal and boundary the flows above do not reach."""

    def test_verified_token_cannot_be_built_from_a_string(self) -> None:
        """A token that never verified cannot pass for one."""
        with pytest.raises(TypeError, match="CurrentToken"):
            VerifiedToken("forged", 1)

        assert "live-token" not in repr(caller("live-token", 5))

    def test_assertion_file_repr_hides_the_path(self, tmp_path: Path) -> None:
        """A path never reaches a repr."""
        auth = ClientAuth.assertion_file(tmp_path / "token")

        assert str(tmp_path) not in repr(auth)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("client_id", ""),
            ("timeout", 0),
            ("refresh_before", -1),
            ("default_lifetime", 0),
            ("max_bytes", 0),
            ("retry_interval", -1),
        ],
    )
    def test_config_refuses_a_value_that_cannot_work(
        self, field: str, value: object
    ) -> None:
        """An empty client ID, a zero timeout or a negative interval is refused."""
        settings = {
            "token_endpoint": TOKEN_ENDPOINT,
            "client_id": "orders-api",
            field: value,
        }

        with pytest.raises(ValueError, match=field):
            OAuthClientConfig.model_validate(settings)

    def test_config_needs_an_issuer_or_a_token_endpoint(self) -> None:
        """A client with nowhere to ask is refused."""
        with pytest.raises(ValueError, match="issuer or a token endpoint"):
            OAuthClientConfig(client_id="orders-api")

    def test_exchange_config_refuses_a_negative_cache(self) -> None:
        """A cache cannot hold fewer than zero tokens."""
        with pytest.raises(ValueError, match="cache_size"):
            TokenExchangeConfig(cache_size=-1)

    def test_discover_without_an_issuer_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A token endpoint from the environment does not stand in for an issuer."""
        monkeypatch.setenv("GREL_OAUTHCLIENT_TOKEN_ENDPOINT", TOKEN_ENDPOINT)

        with pytest.raises(SettingsValidationError, match="needs an issuer"):
            OAuthClient.discover(
                client_id="orders-api",
                client_auth=ClientAuth.secret(SECRET),
                env_load=True,
            )

    def test_client_auth_must_be_a_client_auth(self) -> None:
        """A string is not a way to authenticate."""
        with pytest.raises(TypeError, match="ClientAuth"):
            OAuthClient.discover(
                ISSUER,
                client_id="orders-api",
                client_auth="secret",  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
                env_load=False,
            )

    def test_client_repr_names_no_credential(self) -> None:
        """A client's repr names it, and never its secret."""
        text = repr(secret_client())

        assert "orders-api" in text
        assert SECRET not in text

    def test_assertion_file_variable_is_refused_for_a_secret_client(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An assertion path left in the environment is refused, not ignored."""
        monkeypatch.setenv(
            "GREL_OAUTHCLIENT_ASSERTION_FILE", str(tmp_path / "token")
        )

        with pytest.raises(SettingsValidationError, match="ASSERTION_FILE"):
            secret_client(env_load=True)

    def test_assertion_file_needs_a_path(self) -> None:
        """An assertion file method with no path anywhere is refused."""
        with pytest.raises(SettingsValidationError, match="needs a path"):
            OAuthClient.discover(
                ISSUER,
                client_id="orders-api",
                client_auth=ClientAuth.assertion_file(),
                env_load=False,
            )

    def test_patterns_build_from_a_config(self) -> None:
        """Both patterns take a config that is already whole."""
        credentials = ClientCredentials.from_config(
            "payments-api",
            ClientCredentialsConfig(audience="payments-api"),
            client=secret_client(),
        )
        exchange = TokenExchange.from_config(
            "payments-api",
            TokenExchangeConfig(scopes=["api://payments/.default"]),
            grant="on_behalf_of",
        )

        assert credentials.name == "payments-api"
        assert credentials.config.audience == "payments-api"
        assert exchange.config.scopes == ["api://payments/.default"]
        with pytest.raises(SettingsValidationError, match="grant"):
            TokenExchange.from_config(
                "payments-api",
                TokenExchangeConfig(),
                grant="password",  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            )

    async def test_opening_twice_opens_once(self, server: AuthServer) -> None:
        """A client already open is not opened again."""
        client = secret_client()

        async with client:
            assert await client.__aenter__() is client

        assert [str(r.url) for r in server.requests] == [RFC8414]

    async def test_closing_a_client_never_opened_does_nothing(self) -> None:
        """Closing without opening is harmless."""
        await secret_client().__aexit__(None, None, None)

    async def test_reconfigure_refuses_a_config_the_method_cannot_use(
        self,
    ) -> None:
        """A reload that removes the secret a secret client needs is refused."""
        client = secret_client()

        with pytest.raises(ValueError, match="needs a client secret"):
            await client.reconfigure(
                client.config.model_copy(update={"client_secret": None})
            )

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (b"not json", "not a JSON object"),
            (b'["a list"]', "not a JSON object"),
            (b'{"token_type": "Bearer"}', "no access_token"),
        ],
    )
    async def test_unusable_token_response_is_refused(
        self, server: AuthServer, body: bytes, message: str
    ) -> None:
        """A successful status with no usable token in it is a failure."""
        server.answers.append(httpx.Response(200, content=body))

        async with secret_client() as client:
            with pytest.raises(TokenUnavailableError, match=message):
                await payments(client).token()

    @pytest.mark.parametrize(
        ("expires_in", "lifetime"),
        [
            (True, DEFAULT_LIFETIME),
            ("120", 120),
            (-5, DEFAULT_LIFETIME),
            ("soon", DEFAULT_LIFETIME),
        ],
    )
    async def test_expires_in_is_read_only_as_a_positive_number(
        self,
        server: AuthServer,
        clock: Clock,
        expires_in: object,
        lifetime: int,
    ) -> None:
        """A boolean, a negative or a word is not a lifetime, digits are."""
        server.answers.append(token_response("token", expires_in=expires_in))

        async with secret_client() as client:
            token = await payments(client).token()

        assert token.expires_at == int(clock.wall) + lifetime

    @pytest.mark.parametrize("retry_after", [None, "whenever"])
    async def test_throttled_without_a_usable_retry_after_waits_retry_interval(
        self, server: AuthServer, clock: Clock, retry_after: str | None
    ) -> None:
        """A `429` saying nothing usable about when is remembered for the interval."""
        headers = {} if retry_after is None else {"Retry-After": retry_after}
        server.answers.append(httpx.Response(429, headers=headers))

        async with secret_client() as client:
            pattern = payments(client)
            with pytest.raises(TokenUnavailableError):
                await pattern.token()
            clock.advance(4.9)
            with pytest.raises(TokenUnavailableError):
                await pattern.token()
            clock.advance(0.2)
            await pattern.token()

        assert_fetches(server, 2)

    async def test_basic_when_the_metadata_lists_neither_secret_method(
        self, server: AuthServer
    ) -> None:
        """A server listing only other methods gets the secret in Basic."""
        assert server.metadata is not None
        server.metadata["token_endpoint_auth_methods_supported"] = [
            "private_key_jwt"
        ]

        async with secret_client() as client:
            await payments(client).token()

        assert basic_pair(server.token_requests[0]) == ("orders-api", SECRET)

    async def test_unreachable_metadata_names_the_error(
        self, server: AuthServer
    ) -> None:
        """A metadata endpoint that cannot be reached is reported by error type."""
        server.metadata_answers.extend([lost, lost])

        async with secret_client() as client:
            with pytest.raises(TokenUnavailableError, match="ConnectError"):
                await payments(client).token()

    async def test_oversized_metadata_is_passed_over(
        self, server: AuthServer
    ) -> None:
        """A metadata document past `max_bytes` is abandoned while read."""
        config = OAuthClientConfig(
            issuer=ISSUER,
            client_id="orders-api",
            client_secret=SecretStr(SECRET),
            max_bytes=20,
        )

        async with OAuthClient.from_config(
            config, client_auth=ClientAuth.secret()
        ) as client:
            with pytest.raises(TokenUnavailableError, match="larger than 20"):
                await payments(client).token()

        assert client.token_endpoint is None
        assert server.token_requests == []

    async def test_metadata_that_is_not_an_object_is_passed_over(
        self, server: AuthServer
    ) -> None:
        """A document that is not a JSON object names no endpoint."""
        server.metadata_answers.extend(
            [httpx.Response(200, json=["a list"]), httpx.Response(404)]
        )

        async with secret_client() as client:
            with pytest.raises(
                TokenUnavailableError, match="not a JSON object"
            ):
                await payments(client).token()

    async def test_concurrent_requests_share_one_discovery(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """Requests for different APIs arriving together read the metadata once."""
        server.metadata_url = "nowhere"

        async with secret_client() as client:
            server.metadata_url = RFC8414
            clock.advance(6)
            server.requests.clear()
            server.metadata_gate = asyncio.Event()
            waiting = [
                asyncio.create_task(
                    payments(client, audience=f"api-{index}").token()
                )
                for index in range(3)
            ]
            await settle()
            server.metadata_gate.set()
            await asyncio.gather(*waiting)

        metadata = [
            str(r.url) for r in server.requests if "well-known" in str(r.url)
        ]
        assert metadata == [RFC8414]

    async def test_closing_abandons_a_discovery_in_flight(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """A client closing while it reads metadata does not wait for it."""
        server.metadata_url = "nowhere"
        client = secret_client()
        await client.__aenter__()
        server.metadata_url = RFC8414
        clock.advance(6)
        server.metadata_gate = asyncio.Event()
        waiting = asyncio.create_task(payments(client).token())
        await settle()

        await asyncio.wait_for(client.__aexit__(None, None, None), timeout=1)

        with pytest.raises(asyncio.CancelledError):
            await waiting

    async def test_empty_assertion_file_fails_the_fetch(
        self, server: AuthServer, tmp_path: Path
    ) -> None:
        """A file holding only whitespace is not an assertion."""
        path = tmp_path / "token"
        path.write_text("  \n")
        client = OAuthClient.discover(
            ISSUER,
            client_id="orders-api",
            client_auth=ClientAuth.assertion_file(path),
            env_load=False,
        )

        async with client:
            with pytest.raises(TokenUnavailableError, match="empty"):
                await payments(client).token()

        assert server.token_requests == []

    @pytest.mark.usefixtures("server")
    async def test_exchange_cache_evicts_the_oldest_caller_when_full(
        self,
    ) -> None:
        """A full cache makes room by dropping the caller cached first."""
        alice = caller("alice", FAR_FUTURE)
        bob = caller("bob", FAR_FUTURE)
        carol = caller("carol", FAR_FUTURE)

        async with secret_client() as client:
            exchange = TokenExchange(
                "payments-api",
                audience="payments-api",
                cache_size=2,
                client=client,
                env_load=False,
            )
            for subject in (alice, bob, carol):
                await exchange.token(subject)
            still_cached = await exchange.token(bob)
            fetched_again = await exchange.token(alice)

        assert still_cached.value == "token-2"
        assert fetched_again.value == "token-4"

    async def test_refusal_is_forgotten_after_retry_interval(
        self, server: AuthServer, clock: Clock
    ) -> None:
        """A refused caller is asked for again once the interval has passed."""
        server.answers.append(error_response(400, error="invalid_grant"))
        subject = caller("user", FAR_FUTURE)

        async with secret_client() as client:
            exchange = TokenExchange(
                "payments-api",
                audience="payments-api",
                client=client,
                env_load=False,
            )
            with pytest.raises(TokenUnavailableError):
                await exchange.token(subject)
            clock.advance(5)
            token = await exchange.token(subject)

        assert token.value == "token-1"

    async def test_remembered_refusals_are_bounded(
        self, server: AuthServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The oldest refusal is forgotten first once the table is full."""
        monkeypatch.setattr(oauth, "_REMEMBERED_REFUSALS", 1)
        server.answers.extend(
            [
                error_response(400, error="invalid_grant"),
                error_response(400, error="invalid_grant"),
            ]
        )
        first = caller("first", FAR_FUTURE)
        second = caller("second", FAR_FUTURE)

        async with secret_client() as client:
            exchange = TokenExchange(
                "payments-api",
                audience="payments-api",
                client=client,
                env_load=False,
            )
            for subject in (first, second):
                with pytest.raises(TokenUnavailableError):
                    await exchange.token(subject)
            token = await exchange.token(first)

        assert token.value == "token-1"

    async def test_refused_exchanged_token_is_dropped_for_its_caller(
        self, server: AuthServer
    ) -> None:
        """An API refusing an exchanged token gets a new one for the same caller."""
        api = FakeAPI(httpx.codes.UNAUTHORIZED)

        async with (
            secret_client() as client,
            REAL_ASYNC_CLIENT(transport=httpx.MockTransport(api)) as http,
        ):
            exchange = TokenExchange(
                "payments-api",
                audience="payments-api",
                client=client,
                env_load=False,
            )
            response = await http.get(
                API, auth=exchange.auth(caller("alice", FAR_FUTURE))
            )

        assert response.status_code == httpx.codes.OK
        assert api.tokens == ["Bearer token-1", "Bearer token-2"]
        assert_fetches(server, 2)

    async def test_a_late_refusal_does_not_drop_a_newer_token(
        self, server: AuthServer
    ) -> None:
        """A refusal naming a token already replaced leaves the new one cached."""
        async with secret_client() as client:
            pattern = payments(client)
            old = await pattern.token()
            pattern._invalidate(None, old)
            new = await pattern.token()
            pattern._invalidate(None, old)
            current = await pattern.token()

        assert current.value == new.value == "token-2"
        assert_fetches(server, 2)

    def test_auth_class_needs_an_httpx_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With neither httpx line installed, `auth()` says what to install."""
        oauth._auth_class.cache_clear()
        monkeypatch.setitem(sys.modules, "httpx2", None)
        try:
            only_httpx = oauth._auth_class()
            oauth._auth_class.cache_clear()
            monkeypatch.setitem(sys.modules, "httpx", None)
            with pytest.raises(DependencyNotFoundError, match="httpx"):
                oauth._auth_class()
        finally:
            oauth._auth_class.cache_clear()

        assert only_httpx.__mro__[1] is httpx.Auth

    async def test_refused_client_writes_no_event_when_the_logger_is_off(
        self, server: AuthServer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A security events logger set above warnings costs nothing."""
        server.answers.append(error_response(401, error="invalid_client"))

        with caplog.at_level(logging.ERROR, logger="grelmicro.security.events"):
            async with secret_client() as client:
                with pytest.raises(ClientRejectedError):
                    await payments(client).token()

        assert not [
            record
            for record in caplog.records
            if record.name == "grelmicro.security.events"
        ]
