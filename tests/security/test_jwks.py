"""Tests for verifying against keys fetched from a JWKS endpoint.

The endpoint is never reached. A fetcher is injected for most tests, and the
default `httpx` fetcher is driven through a mock transport, so the suite
exercises the limits without depending on a network or on a provider.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

import anyio
import httpx
import httpx2
import pytest
from pydantic import ValidationError

from grelmicro.errors import (
    DependencyNotFoundError,
    OutOfContextError,
    SettingsValidationError,
)
from grelmicro.security import (
    ClientBannedError,
    ClientBans,
    ClientBansConfig,
    JWKSConfig,
    JWKSUnavailableError,
    JWKSVerifier,
    JWTConfig,
    JWTKey,
    JWTVerifier,
    TokenRejectedError,
    TokenVerifier,
)
from grelmicro.security.jwks import fetch_with_httpx
from tests.security.jwt_signing import Signer

URL = "https://idp.example.com/.well-known/jwks.json"
AUDIENCE = "grelmicro-api"
ISSUER = "https://auth.grel.info/"
HOUR = 3600
BIG = 64
SMALL_LIMIT = 2
EXPECTED_FETCHES = 2
SPRAYED_KIDS = 20
MAX_BYTES = 1_048_576
MAX_KEYS = 64
MIN_RETRY_INTERVAL = 60
LEEWAY = 30
OVERSIZED = 5000

SIGNER = Signer()
ROTATED = Signer()
CLIENT = "203.0.113.9"


def document(signer: Signer = SIGNER, kid: str = "k1", **extra: Any) -> bytes:  # noqa: ANN401
    """Return a JWKS document holding one signing key."""
    return json.dumps(
        {"keys": [signer.public_jwk("RS256", kid=kid, **extra)]}
    ).encode()


def token(signer: Signer = SIGNER, kid: str = "k1", **claims: Any) -> str:  # noqa: ANN401
    """Return a token signed by `signer` and naming `kid`."""
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "sub": "user-1",
        "aud": AUDIENCE,
        "exp": now + HOUR,
        "iat": now,
    }
    payload.update(claims)
    return signer.token(payload, algorithm="RS256", header={"kid": kid})


def config(**overrides: Any) -> JWKSConfig:  # noqa: ANN401
    """Return a config pointed at the fake endpoint."""
    overrides.setdefault("audience", [AUDIENCE])
    overrides.setdefault("issuer", [ISSUER])
    return JWKSConfig(url=URL, **overrides)


def responder2(status: int, body: bytes) -> Any:  # noqa: ANN401
    """Return the same fixed-response handler, for the `httpx2` line."""

    def handle(request: httpx2.Request) -> httpx2.Response:  # noqa: ARG001
        return httpx2.Response(status, content=body)

    return handle


def responder(status: int, body: bytes) -> Any:  # noqa: ANN401
    """Return a mock transport handler serving one fixed response."""

    def handle(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(status, content=body)

    return handle


class Endpoint:
    """A fetcher that serves whatever the test last put in it."""

    def __init__(self, body: bytes | Exception = b"") -> None:
        """Start serving `body`."""
        self.body = body
        self.calls = 0

    async def __call__(
        self,
        url: str,  # noqa: ARG002
        *,
        timeout: float,  # noqa: ARG002, ASYNC109
        max_bytes: int,  # noqa: ARG002
    ) -> bytes:
        """Serve the current body, counting the call."""
        self.calls += 1
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class TestConfiguration:
    """What the endpoint settings refuse."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://idp.example.com/jwks.json",
            "ftp://idp.example.com/jwks.json",
            "idp.example.com/jwks.json",
            "",
        ],
    )
    def test_only_https_is_accepted(self, url: str) -> None:
        """The endpoint decides who is believed, so the channel is checked."""
        with pytest.raises(ValidationError):
            JWKSConfig(url=url)

    @pytest.mark.parametrize("setting", ["ttl", "retry_interval", "timeout"])
    @pytest.mark.parametrize("value", [0, -1])
    def test_durations_must_be_positive(self, setting: str, value: int) -> None:
        """A refresh every zero seconds is not a schedule."""
        settings: dict[str, Any] = {"url": URL, setting: value}

        with pytest.raises(ValidationError):
            JWKSConfig(**settings)

    @pytest.mark.parametrize("setting", ["max_bytes", "max_keys"])
    def test_limits_must_accept_something(self, setting: str) -> None:
        """A limit of zero would refuse every document."""
        settings: dict[str, Any] = {"url": URL, setting: 0}

        with pytest.raises(ValidationError):
            JWKSConfig(**settings)

    def test_the_claim_policy_carries_across(self) -> None:
        """A JWKS verifier enforces what a static one enforces."""
        built = config(
            leeway=LEEWAY, required=["exp", "sub"], cache_key="token"
        )

        assert built.leeway == LEEWAY
        assert built.required == ["exp", "sub"]
        assert built.cache_key == "token"

    def test_secure_defaults(self) -> None:
        """The defaults are the safe ones, not the permissive ones."""
        built = JWKSConfig(url=URL)

        assert built.timeout > 0
        assert built.max_bytes <= MAX_BYTES
        assert built.max_keys <= MAX_KEYS
        assert built.retry_interval >= MIN_RETRY_INTERVAL
        assert built.cache_key == "sha256"
        assert built.required == ["exp"]


class TestRefresh:
    """Loading the key set, and not loading it more than needed."""

    async def test_the_first_refresh_loads_the_keys(self) -> None:
        """Nothing is loaded until the first refresh."""
        endpoint = Endpoint(document())
        verifier = JWKSVerifier(config(), fetch=endpoint)

        assert verifier.ready is False
        assert verifier.stale is True
        assert await verifier.refresh() is True
        assert verifier.ready is True
        assert verifier.stale is False

    async def test_a_fresh_key_set_is_not_fetched_again(self) -> None:
        """A scheduled refresh costs nothing while the keys are current."""
        endpoint = Endpoint(document())
        verifier = JWKSVerifier(config(), fetch=endpoint)
        await verifier.refresh()

        assert await verifier.refresh() is False
        assert endpoint.calls == 1

    async def test_an_unchanged_document_does_not_rebuild(self) -> None:
        """Fetching the same bytes twice leaves the verifier alone."""
        endpoint = Endpoint(document())
        verifier = JWKSVerifier(config(), fetch=endpoint)
        await verifier.refresh()

        assert await verifier.refresh(force=True) is False
        assert endpoint.calls == EXPECTED_FETCHES

    async def test_force_fetches_even_when_fresh(self) -> None:
        """An operator can make it look now rather than wait for the TTL."""
        endpoint = Endpoint(document())
        verifier = JWKSVerifier(config(), fetch=endpoint)
        await verifier.refresh()

        endpoint.body = document(ROTATED, kid="k2")

        assert await verifier.refresh(force=True) is True

    async def test_an_expired_ttl_fetches_again(self) -> None:
        """Past the TTL the key set is fetched even without a rotation."""
        endpoint = Endpoint(document())
        verifier = JWKSVerifier(
            config(ttl=0.01, retry_interval=0.01), fetch=endpoint
        )
        await verifier.refresh()
        await anyio.sleep(0.05)

        assert verifier.stale is True
        await verifier.refresh()
        assert endpoint.calls == EXPECTED_FETCHES

    async def test_a_failed_refresh_keeps_the_loaded_keys(self) -> None:
        """A provider going down must not take authentication down."""
        endpoint = Endpoint(document())
        verifier = JWKSVerifier(config(ttl=0.01), fetch=endpoint)
        await verifier.refresh()
        endpoint.body = JWKSUnavailableError("endpoint is down")

        with pytest.raises(JWKSUnavailableError):
            await verifier.refresh(force=True)

        assert verifier.verify(token()).subject == "user-1"


class TestRotation:
    """Following a provider that changes its signing keys."""

    async def test_an_unknown_kid_marks_the_set_stale(self) -> None:
        """The next scheduled refresh fetches, and the request is refused."""
        endpoint = Endpoint(document())
        verifier = JWKSVerifier(config(), fetch=endpoint)
        await verifier.refresh()
        assert verifier.stale is False

        with pytest.raises(TokenRejectedError) as caught:
            verifier.verify(token(ROTATED, kid="k2"))

        assert caught.value.reason == "unknown-key"
        assert verifier.stale is True

    async def test_a_refresh_picks_up_the_new_key(self) -> None:
        """Both the trigger and the recovery work end to end."""
        endpoint = Endpoint(document())
        verifier = JWKSVerifier(config(retry_interval=0.01), fetch=endpoint)
        await verifier.refresh()
        endpoint.body = document(ROTATED, kid="k2")
        rotated = token(ROTATED, kid="k2")

        # The rotated token is refused, and that is what marks the set stale.
        with pytest.raises(TokenRejectedError):
            verifier.verify(rotated)
        await anyio.sleep(0.02)

        assert await verifier.refresh() is True
        assert verifier.verify(rotated).subject == "user-1"

    async def test_the_retry_interval_bounds_fetching(self) -> None:
        """Invented `kid` values cannot make the service hammer its provider."""
        endpoint = Endpoint(document())
        verifier = JWKSVerifier(config(retry_interval=60.0), fetch=endpoint)
        await verifier.refresh()

        for index in range(SPRAYED_KIDS):
            with pytest.raises(TokenRejectedError):
                verifier.verify(token(ROTATED, kid=f"invented-{index}"))
            assert await verifier.refresh() is False

        assert endpoint.calls == 1

    async def test_an_ordinary_rejection_does_not_mark_stale(self) -> None:
        """Only an unknown key means the provider may have rotated."""
        endpoint = Endpoint(document())
        verifier = JWKSVerifier(config(), fetch=endpoint)
        await verifier.refresh()

        with pytest.raises(TokenRejectedError) as caught:
            verifier.verify(token(aud="somewhere-else"))
        assert caught.value.reason == "audience"
        assert verifier.stale is False

        with pytest.raises(TokenRejectedError):
            verifier.verify_header(f"Bearer {token(aud='somewhere-else')}")
        assert verifier.stale is False

    async def test_the_header_path_also_marks_stale(self) -> None:
        """`verify_header` follows the same rotation signal."""
        endpoint = Endpoint(document())
        verifier = JWKSVerifier(config(), fetch=endpoint)
        await verifier.refresh()

        with pytest.raises(TokenRejectedError):
            verifier.verify_header(f"Bearer {token(ROTATED, kid='k2')}")

        assert verifier.stale is True


class TestVerifying:
    """The synchronous path, which never touches the network."""

    async def test_a_bearer_header_verifies(self) -> None:
        """The same header handling as a statically keyed verifier."""
        verifier = JWKSVerifier(config(), fetch=Endpoint(document()))
        await verifier.refresh()

        assert verifier.verify_header(f"Bearer {token()}").subject == "user-1"

    async def test_claims_are_checked(self) -> None:
        """The policy is enforced, not just the signature."""
        verifier = JWKSVerifier(config(), fetch=Endpoint(document()))
        await verifier.refresh()

        with pytest.raises(TokenRejectedError) as caught:
            verifier.verify(token(aud="somewhere-else"))

        assert caught.value.reason == "audience"

    @pytest.mark.parametrize("method", ["verify", "verify_header"])
    def test_verifying_before_loading_is_refused(self, method: str) -> None:
        """Nothing is trusted before a key set has been fetched."""
        verifier = JWKSVerifier(config(), fetch=Endpoint(document()))

        with pytest.raises(OutOfContextError, match="refresh"):
            getattr(verifier, method)(token())


class TestInterchangeable:
    """Either verifier can stand behind the same dependency."""

    async def test_both_satisfy_the_protocol(self) -> None:
        """A caller types against `TokenVerifier` and takes either."""
        static: TokenVerifier = JWTVerifier(
            JWTConfig(
                keys=[
                    JWTKey(
                        algorithm="RS256",
                        key=SIGNER.public_pem("RS256"),
                        kid="k1",
                    )
                ],
                audience=[AUDIENCE],
                issuer=[ISSUER],
            )
        )
        fetched: TokenVerifier = JWKSVerifier(
            config(), fetch=Endpoint(document())
        )
        await fetched.refresh()  # type: ignore[attr-defined]

        for verifier in (static, fetched):
            assert verifier.verify_header(f"Bearer {token(kid='k1')}").subject
            assert verifier.unverified_header(token(kid="k1"))["kid"] == "k1"

    async def test_unverified_header_needs_a_loaded_key_set(self) -> None:
        """It reads nothing before a key set is there to route against."""
        verifier = JWKSVerifier(config(), fetch=Endpoint(document()))

        with pytest.raises(OutOfContextError, match="refresh"):
            verifier.unverified_header(token())


class TestBans:
    """The same opt-in ban table, on a verifier fed from an endpoint."""

    async def subject(self) -> JWKSVerifier:
        """Return a loaded verifier that bans after two forged tokens."""
        built = JWKSVerifier(
            config(),
            fetch=Endpoint(document()),
            bans=ClientBans(
                ClientBansConfig(failures=2, window=60.0, duration=60.0)
            ),
        )
        await built.refresh()
        return built

    async def test_repeated_forgery_bans_the_client(self) -> None:
        """A forger is shed here exactly as it is with a static key."""
        verifier = await self.subject()
        forged = token()[:-3] + "AAA"

        for _ in range(2):
            with pytest.raises(TokenRejectedError):
                verifier.verify(forged, client=CLIENT)

        with pytest.raises(ClientBannedError):
            verifier.verify(token(), client=CLIENT)

    async def test_the_header_path_bans_too(self) -> None:
        """Both doors count against the same client."""
        verifier = await self.subject()
        forged = f"Bearer {token()[:-3]}AAA"

        for _ in range(2):
            with pytest.raises(TokenRejectedError):
                verifier.verify_header(forged, client=CLIENT)

        with pytest.raises(ClientBannedError):
            verifier.verify_header(f"Bearer {token()}", client=CLIENT)

    async def test_a_rotation_never_bans(self) -> None:
        """`unknown-key` is what a rotation looks like, so it cannot ban."""
        verifier = await self.subject()
        rotated = token(ROTATED, kid="k2")

        for _ in range(20):
            with pytest.raises(TokenRejectedError):
                verifier.verify(rotated, client=CLIENT)

        assert verifier.verify(token(), client=CLIENT).subject == "user-1"

    async def test_a_missing_client_is_refused_loudly(self) -> None:
        """Protection that counts nothing must not look configured."""
        verifier = await self.subject()

        with pytest.raises(SettingsValidationError, match="client="):
            verifier.verify(token())

    async def test_without_bans_a_client_is_not_required(self) -> None:
        """The default verifier is unchanged."""
        verifier = JWKSVerifier(config(), fetch=Endpoint(document()))
        await verifier.refresh()

        assert verifier.verify(token()).subject == "user-1"


class TestDocumentLimits:
    """What a hostile or broken endpoint cannot do."""

    @pytest.mark.parametrize(
        ("body", "message"),
        [
            (b"not json", "valid JSON"),
            (b'"a string"', "JSON object"),
            (b"[]", "JSON object"),
            (b"{}", "no keys"),
            (b'{"keys": []}', "no keys"),
            (b'{"keys": {}}', "no keys"),
        ],
    )
    async def test_a_malformed_document_is_refused(
        self, body: bytes, message: str
    ) -> None:
        """The verifier is left unloaded rather than half-configured."""
        verifier = JWKSVerifier(config(), fetch=Endpoint(body))

        with pytest.raises(JWKSUnavailableError, match=message):
            await verifier.refresh()

        assert verifier.ready is False

    async def test_too_many_keys_are_refused(self) -> None:
        """A document with thousands of keys is not a key set."""
        keys = [SIGNER.public_jwk("RS256", kid=f"k{i}") for i in range(BIG)]
        body = json.dumps({"keys": keys}).encode()
        verifier = JWKSVerifier(
            config(max_keys=SMALL_LIMIT), fetch=Endpoint(body)
        )

        with pytest.raises(JWKSUnavailableError, match="more than"):
            await verifier.refresh()

    async def test_a_key_that_is_not_a_key_is_refused(self) -> None:
        """A usable document shape with unusable contents still fails."""
        body = json.dumps(
            {"keys": [{"kty": "unheard-of", "kid": "x"}]}
        ).encode()
        verifier = JWKSVerifier(config(), fetch=Endpoint(body))

        with pytest.raises(JWKSUnavailableError, match="no usable key"):
            await verifier.refresh()

    async def test_a_key_the_core_refuses_leaves_the_document_unloaded(
        self,
    ) -> None:
        """A JWK can read as a key here and still be refused by the core.

        `refresh` promises one error, and the document must not be recorded
        as loaded, or the next refresh would see no change and skip the
        rebuild for a whole `ttl`.
        """
        body = json.dumps(
            {
                "keys": [
                    {
                        "kty": "RSA",
                        "kid": "broken",
                        "alg": "RS256",
                        "n": "!!!!",
                        "e": "AQAB",
                    }
                ]
            }
        ).encode()
        endpoint = Endpoint(body)
        verifier = JWKSVerifier(config(retry_interval=0.01), fetch=endpoint)

        with pytest.raises(JWKSUnavailableError, match="no usable key"):
            await verifier.refresh()
        assert verifier.stale is True

        endpoint.body = document()
        await anyio.sleep(0.02)
        await verifier.refresh()

        assert verifier.verify(token()).subject == "user-1"

    async def test_encryption_keys_are_skipped(self) -> None:
        """A signature is never verified with an encryption key."""
        body = json.dumps(
            {
                "keys": [
                    SIGNER.public_jwk("RS256", kid="sig-1"),
                    SIGNER.public_jwk("RS256", kid="enc-1", use="enc"),
                ]
            }
        ).encode()
        verifier = JWKSVerifier(config(), fetch=Endpoint(body))
        await verifier.refresh()

        assert verifier.verify(token(kid="sig-1")).subject == "user-1"

    async def test_a_key_without_an_alg_is_pinned(self) -> None:
        """Entra ID publishes no `alg`, so the config supplies one."""
        body = json.dumps(
            {"keys": [SIGNER.public_jwk(kid="entra-1", alg=None)]}
        ).encode()
        verifier = JWKSVerifier(config(algorithm="RS256"), fetch=Endpoint(body))
        await verifier.refresh()

        assert verifier.verify(token(kid="entra-1")).subject == "user-1"


class TestDefaultFetcher:
    """The `httpx` fetcher, driven through a mock transport."""

    def client(
        self,
        monkeypatch: pytest.MonkeyPatch,
        handler: Any,  # noqa: ANN401
    ) -> None:
        """Point `httpx.AsyncClient` at a transport the test controls."""
        real = httpx.AsyncClient

        def factory(**kwargs: Any) -> httpx.AsyncClient:  # noqa: ANN401
            return real(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)

    async def test_it_returns_the_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A healthy endpoint gives back exactly what it served."""
        body = document()
        self.client(monkeypatch, responder(200, body))

        fetched = await fetch_with_httpx(URL, timeout=1.0, max_bytes=1 << 20)

        assert fetched == body

    async def test_a_bad_status_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An error page is not a key set."""
        self.client(monkeypatch, responder(503, b"down"))

        with pytest.raises(JWKSUnavailableError, match="503"):
            await fetch_with_httpx(URL, timeout=1.0, max_bytes=1 << 20)

    async def test_an_oversized_body_is_abandoned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cap is enforced while reading, not from the declared length."""
        self.client(monkeypatch, responder(200, b"x" * OVERSIZED))

        with pytest.raises(JWKSUnavailableError, match="larger than"):
            await fetch_with_httpx(URL, timeout=1.0, max_bytes=100)

    async def test_a_missing_httpx_says_what_to_install(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default fetcher is the only thing that needs it."""
        monkeypatch.setitem(sys.modules, "httpx", None)
        monkeypatch.setitem(sys.modules, "httpx2", None)

        with pytest.raises(DependencyNotFoundError, match="httpx"):
            await fetch_with_httpx(URL, timeout=1.0, max_bytes=1 << 20)

    async def test_httpx2_is_accepted_in_its_place(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An application on the 2.x line needs no separate install."""
        body = document()
        monkeypatch.setitem(sys.modules, "httpx", None)
        real = httpx2.AsyncClient

        def factory(**kwargs: Any) -> Any:  # noqa: ANN401
            transport = httpx2.MockTransport(responder2(200, body))
            return real(transport=transport, **kwargs)

        monkeypatch.setattr(httpx2, "AsyncClient", factory)

        fetched = await fetch_with_httpx(URL, timeout=1.0, max_bytes=1 << 20)

        assert fetched == body

    async def test_it_is_the_default(self) -> None:
        """A verifier with no fetcher uses it."""
        verifier = JWKSVerifier(config())

        assert verifier._fetch is fetch_with_httpx
