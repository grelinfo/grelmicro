"""Tests for verifying against keys fetched from a JWKS endpoint.

The endpoint is never reached. A fetcher is injected for most tests, and the
default `httpx` fetcher is driven through a mock transport, so the suite
exercises the limits without depending on a network or on a provider.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from typing import TYPE_CHECKING, Any

import anyio
import httpx
import httpx2
import pytest
from pydantic import ValidationError

from grelmicro._config import reconfigure_all
from grelmicro.errors import (
    DependencyNotFoundError,
    SettingsValidationError,
)
from grelmicro.security import (
    JWKSConfig,
    JWTKey,
    JWTKeysConfig,
    JWTVerifier,
    SigningKeysUnavailableError,
    TokenRejectedError,
    TokenVerifier,
)
from grelmicro.security.jwks import fetch_with_httpx
from tests.security.jwt_signing import Signer

if TYPE_CHECKING:
    from collections.abc import Callable

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
            JWKSConfig(url=url, audience=AUDIENCE)

    @pytest.mark.parametrize("setting", ["ttl", "retry_interval", "timeout"])
    @pytest.mark.parametrize("value", [0, -1])
    def test_durations_must_be_positive(self, setting: str, value: int) -> None:
        """A refresh every zero seconds is not a schedule."""
        settings: dict[str, Any] = {
            "url": URL,
            "audience": AUDIENCE,
            setting: value,
        }

        with pytest.raises(ValidationError):
            JWKSConfig(**settings)

    @pytest.mark.parametrize("setting", ["max_bytes", "max_keys"])
    def test_limits_must_accept_something(self, setting: str) -> None:
        """A limit of zero would refuse every document."""
        settings: dict[str, Any] = {
            "url": URL,
            "audience": AUDIENCE,
            setting: 0,
        }

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
        built = JWKSConfig(url=URL, audience=AUDIENCE)

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
        verifier = JWTVerifier.from_config(config(), fetch=endpoint)

        assert verifier.ready is False
        assert verifier.stale is True
        assert await verifier.refresh() is True
        assert verifier.ready is True
        assert verifier.stale is False

    async def test_a_fresh_key_set_is_not_fetched_again(self) -> None:
        """A scheduled refresh costs nothing while the keys are current."""
        endpoint = Endpoint(document())
        verifier = JWTVerifier.from_config(config(), fetch=endpoint)
        await verifier.refresh()

        assert await verifier.refresh() is False
        assert endpoint.calls == 1

    async def test_an_unchanged_document_does_not_rebuild(self) -> None:
        """Fetching the same bytes twice leaves the verifier alone."""
        endpoint = Endpoint(document())
        verifier = JWTVerifier.from_config(config(), fetch=endpoint)
        await verifier.refresh()

        assert await verifier.refresh(force=True) is False
        assert endpoint.calls == EXPECTED_FETCHES

    async def test_force_fetches_even_when_fresh(self) -> None:
        """An operator can make it look now rather than wait for the TTL."""
        endpoint = Endpoint(document())
        verifier = JWTVerifier.from_config(config(), fetch=endpoint)
        await verifier.refresh()

        endpoint.body = document(ROTATED, kid="k2")

        assert await verifier.refresh(force=True) is True

    async def test_an_expired_ttl_fetches_again(self) -> None:
        """Past the TTL the key set is fetched even without a rotation."""
        endpoint = Endpoint(document())
        verifier = JWTVerifier.from_config(
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
        verifier = JWTVerifier.from_config(config(ttl=0.01), fetch=endpoint)
        await verifier.refresh()
        endpoint.body = SigningKeysUnavailableError("endpoint is down")

        with pytest.raises(SigningKeysUnavailableError):
            await verifier.refresh(force=True)

        assert verifier.verify(token()).subject == "user-1"


class TestRotation:
    """Following a provider that changes its signing keys."""

    async def test_an_unknown_kid_marks_the_set_stale(self) -> None:
        """The next scheduled refresh fetches, and the request is refused."""
        endpoint = Endpoint(document())
        verifier = JWTVerifier.from_config(config(), fetch=endpoint)
        await verifier.refresh()
        assert verifier.stale is False

        with pytest.raises(TokenRejectedError) as caught:
            verifier.verify(token(ROTATED, kid="k2"))

        assert caught.value.reason == "unknown-key"
        assert verifier.stale is True

    async def test_a_refresh_picks_up_the_new_key(self) -> None:
        """Both the trigger and the recovery work end to end."""
        endpoint = Endpoint(document())
        verifier = JWTVerifier.from_config(
            config(retry_interval=0.01), fetch=endpoint
        )
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
        verifier = JWTVerifier.from_config(
            config(retry_interval=60.0), fetch=endpoint
        )
        await verifier.refresh()

        for index in range(SPRAYED_KIDS):
            with pytest.raises(TokenRejectedError):
                verifier.verify(token(ROTATED, kid=f"invented-{index}"))
            assert await verifier.refresh() is False

        assert endpoint.calls == 1

    async def test_an_ordinary_rejection_does_not_mark_stale(self) -> None:
        """Only an unknown key means the provider may have rotated."""
        endpoint = Endpoint(document())
        verifier = JWTVerifier.from_config(config(), fetch=endpoint)
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
        verifier = JWTVerifier.from_config(config(), fetch=endpoint)
        await verifier.refresh()

        with pytest.raises(TokenRejectedError):
            verifier.verify_header(f"Bearer {token(ROTATED, kid='k2')}")

        assert verifier.stale is True


class TestVerifying:
    """The synchronous path, which never touches the network."""

    async def test_a_bearer_header_verifies(self) -> None:
        """The same header handling as a statically keyed verifier."""
        verifier = JWTVerifier.from_config(config(), fetch=Endpoint(document()))
        await verifier.refresh()

        assert verifier.verify_header(f"Bearer {token()}").subject == "user-1"

    async def test_claims_are_checked(self) -> None:
        """The policy is enforced, not just the signature."""
        verifier = JWTVerifier.from_config(config(), fetch=Endpoint(document()))
        await verifier.refresh()

        with pytest.raises(TokenRejectedError) as caught:
            verifier.verify(token(aud="somewhere-else"))

        assert caught.value.reason == "audience"

    @pytest.mark.parametrize(
        ("method", "prefix"), [("verify", ""), ("verify_header", "Bearer ")]
    )
    def test_verifying_before_loading_is_refused(
        self, method: str, prefix: str
    ) -> None:
        """Nothing is trusted before a key set has been fetched."""
        verifier = JWTVerifier.from_config(config(), fetch=Endpoint(document()))

        with pytest.raises(SigningKeysUnavailableError, match="refresh"):
            getattr(verifier, method)(prefix + token())


class TestInterchangeable:
    """Either verifier can stand behind the same dependency."""

    async def test_both_satisfy_the_protocol(self) -> None:
        """A caller types against `TokenVerifier` and takes either."""
        static: TokenVerifier = JWTVerifier.from_config(
            JWTKeysConfig(
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
        fetched: TokenVerifier = JWTVerifier.from_config(
            config(), fetch=Endpoint(document())
        )
        await fetched.refresh()  # type: ignore[attr-defined]

        for verifier in (static, fetched):
            assert verifier.verify_header(f"Bearer {token(kid='k1')}").subject


class TestFactory:
    """`JWTVerifier.jwks` and the config door build the same verifier."""

    async def test_jwks_builds_a_verifier_that_refreshes(self) -> None:
        """Nothing is fetched until `refresh`, and then the keys verify."""
        verifier = JWTVerifier.jwks(
            URL, audience=AUDIENCE, issuer=ISSUER, fetch=Endpoint(document())
        )

        assert verifier.ready is False
        assert await verifier.refresh() is True
        assert verifier.verify(token()).subject == "user-1"

    def test_jwks_refuses_an_endpoint_that_is_not_https(self) -> None:
        """The factory raises the one error every setting raises."""
        with pytest.raises(SettingsValidationError, match="https"):
            JWTVerifier.jwks("http://idp.example.com/jwks", audience=AUDIENCE)

    def test_a_fetcher_is_refused_for_keys_held_in_code(self) -> None:
        """There is nothing to fetch, so a fetcher is a mistake to report."""
        config = JWTKeysConfig(
            keys=[JWTKey.pem(SIGNER.public_pem("RS256"), algorithm="RS256")],
            audience=AUDIENCE,
        )

        with pytest.raises(TypeError, match="fetch="):
            JWTVerifier.from_config(config, fetch=Endpoint(document()))

    async def test_keys_held_in_code_are_always_ready(self) -> None:
        """A static key set never goes stale and never fetches."""
        verifier = JWTVerifier.keys(
            JWTKey.pem(SIGNER.public_pem("RS256"), algorithm="RS256"),
            audience=AUDIENCE,
        )

        assert verifier.ready is True
        assert verifier.stale is False
        assert await verifier.refresh(force=True) is False

    async def test_a_withdrawn_key_takes_its_cached_tokens_with_it(
        self,
    ) -> None:
        """A token cached under a key the provider withdrew is not served."""
        endpoint = Endpoint(document())
        verifier = JWTVerifier.from_config(config(), fetch=endpoint)
        await verifier.refresh()
        issued = token()
        assert verifier.verify(issued).subject == "user-1"

        endpoint.body = document(ROTATED, kid="k1")
        assert await verifier.refresh(force=True) is True

        with pytest.raises(TokenRejectedError) as caught:
            verifier.verify(issued)

        assert caught.value.reason == "signature"


class Slow(Endpoint):
    """An endpoint that takes a moment to answer, so fetches can overlap."""

    async def __call__(
        self,
        url: str,
        *,
        timeout: float,  # noqa: ASYNC109
        max_bytes: int,
    ) -> bytes:
        """Wait, then serve the current body."""
        await anyio.sleep(0.05)
        return await super().__call__(url, timeout=timeout, max_bytes=max_bytes)


async def until(condition: Callable[[], bool], *, within: float = 5.0) -> None:
    """Wait for `condition` to hold, failing once `within` seconds pass."""
    deadline = time.monotonic() + within
    while not condition():
        if time.monotonic() > deadline:
            msg = "the condition never held"
            raise AssertionError(msg)
        await anyio.sleep(0.005)


def verifies(verifier: JWTVerifier, presented: str) -> bool:
    """Return whether `presented` verifies right now."""
    try:
        verifier.verify(presented)
    except TokenRejectedError:
        return False
    return True


class TestLifecycle:
    """Opening a verifier loads its keys and keeps them fresh until it closes."""

    async def test_entering_loads_the_keys(self) -> None:
        """The first request never arrives before the keys do."""
        endpoint = Endpoint(document())

        async with JWTVerifier.from_config(
            config(), fetch=endpoint
        ) as verifier:
            assert verifier.ready is True
            assert verifier.verify(token()).subject == "user-1"

        assert endpoint.calls == 1

    async def test_keys_held_in_code_need_nothing(self) -> None:
        """There is nothing to fetch, so nothing runs in the background."""
        static = JWTVerifier.keys(
            JWTKey.pem(SIGNER.public_pem("RS256"), algorithm="RS256"),
            audience=AUDIENCE,
        )

        async with static as verifier:
            assert verifier.ready is True
            assert verifier._task is None

    async def test_an_unreachable_provider_does_not_stop_the_app(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The verifier opens without keys and loads them once it can."""
        endpoint = Endpoint(SigningKeysUnavailableError("endpoint is down"))

        with caplog.at_level(logging.WARNING, logger="grelmicro.security.jwt"):
            async with JWTVerifier.from_config(
                config(retry_interval=0.01), fetch=endpoint
            ) as verifier:
                assert verifier.ready is False
                with pytest.raises(SigningKeysUnavailableError):
                    verifier.verify(token())

                endpoint.body = document()
                await until(lambda: verifier.ready)

                assert verifier.verify(token()).subject == "user-1"

        assert "could not be loaded" in caplog.text

    async def test_a_transport_error_does_not_stop_the_app(self) -> None:
        """A fetcher failing in its own way opens the verifier without keys."""
        endpoint = Endpoint(OSError("connection refused"))

        async with JWTVerifier.from_config(
            config(retry_interval=0.01), fetch=endpoint
        ) as verifier:
            assert verifier.ready is False

            endpoint.body = document()
            await until(lambda: verifier.ready)

    async def test_a_transport_error_never_ends_the_background_refresh(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The loop logs it, keeps the keys, and closes cleanly."""
        endpoint = Endpoint(document())

        with caplog.at_level(logging.WARNING, logger="grelmicro.security.jwt"):
            async with JWTVerifier.from_config(
                config(ttl=0.01, retry_interval=0.01), fetch=endpoint
            ) as verifier:
                endpoint.body = OSError("connection reset")
                await until(lambda: "OSError" in caplog.text)

                endpoint.body = document(ROTATED, kid="k2")
                rotated = token(ROTATED, kid="k2")
                await until(lambda: verifies(verifier, rotated))

    async def test_refresh_raises_only_its_own_error(self) -> None:
        """Whatever the fetcher raised, the caller catches one error."""
        verifier = JWTVerifier.from_config(
            config(), fetch=Endpoint(OSError("connection refused"))
        )

        with pytest.raises(SigningKeysUnavailableError, match="OSError"):
            await verifier.refresh()

    async def test_a_document_of_malformed_keys_does_not_stop_the_app(
        self,
    ) -> None:
        """Keys nothing can read leave the verifier open and retrying."""
        body = json.dumps({"keys": ["not-a-key", {"kty": ["RSA"]}]}).encode()
        endpoint = Endpoint(body)

        async with JWTVerifier.from_config(
            config(retry_interval=0.01), fetch=endpoint
        ) as verifier:
            assert verifier.ready is False

            endpoint.body = document()
            await until(lambda: verifier.ready)

    async def test_a_missing_dependency_stops_the_app(self) -> None:
        """A broken install is not an outage, so it is not retried."""
        endpoint = Endpoint(DependencyNotFoundError(module="httpx"))
        verifier = JWTVerifier.from_config(config(), fetch=endpoint)

        with pytest.raises(DependencyNotFoundError):
            await verifier.__aenter__()

        assert verifier._task is None

    async def test_an_unforeseen_error_never_ends_the_background_refresh(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """It is logged, and the next pass still follows a rotation."""
        endpoint = Endpoint(document())

        with caplog.at_level(logging.ERROR, logger="grelmicro.security.jwt"):
            async with JWTVerifier.from_config(
                config(ttl=0.01, retry_interval=0.01), fetch=endpoint
            ) as verifier:
                endpoint.body = DependencyNotFoundError(module="httpx")
                await until(lambda: "could not be refreshed" in caplog.text)

                endpoint.body = document(ROTATED, kid="k2")
                rotated = token(ROTATED, kid="k2")
                await until(lambda: verifies(verifier, rotated))

    async def test_a_background_refresh_follows_a_rotation(self) -> None:
        """A token naming a new key verifies once the next pass runs."""
        endpoint = Endpoint(document())

        async with JWTVerifier.from_config(
            config(retry_interval=0.01), fetch=endpoint
        ) as verifier:
            endpoint.body = document(ROTATED, kid="k2")
            rotated = token(ROTATED, kid="k2")

            await until(lambda: verifies(verifier, rotated))

    async def test_a_failed_background_refresh_keeps_the_keys(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A provider going down later is logged, and verification goes on."""
        endpoint = Endpoint(document())

        with caplog.at_level(logging.WARNING, logger="grelmicro.security.jwt"):
            async with JWTVerifier.from_config(
                config(ttl=0.01, retry_interval=0.01), fetch=endpoint
            ) as verifier:
                endpoint.body = SigningKeysUnavailableError("endpoint is down")

                await until(lambda: "could not be refreshed" in caplog.text)

                assert verifier.verify(token()).subject == "user-1"

    async def test_closing_stops_the_refresh(self) -> None:
        """Nothing keeps fetching after the verifier closes."""
        endpoint = Endpoint(document())
        verifier = JWTVerifier.from_config(
            config(ttl=0.01, retry_interval=0.01), fetch=endpoint
        )

        async with verifier:
            await until(lambda: endpoint.calls >= EXPECTED_FETCHES)
        calls = endpoint.calls
        await anyio.sleep(0.05)

        assert endpoint.calls == calls
        assert verifier._task is None

    async def test_entering_twice_starts_one_refresh(self) -> None:
        """An open verifier opened again keeps the loop it already has."""
        verifier = JWTVerifier.from_config(config(), fetch=Endpoint(document()))

        async with verifier:
            running = verifier._task
            async with verifier:
                assert verifier._task is running

    async def test_concurrent_refreshes_share_one_fetch(self) -> None:
        """A burst of callers costs the provider one request."""
        endpoint = Slow(document())
        verifier = JWTVerifier.from_config(config(), fetch=endpoint)

        results = await asyncio.gather(
            *(verifier.refresh() for _ in range(SPRAYED_KIDS))
        )

        assert results == [True] * SPRAYED_KIDS
        assert endpoint.calls == 1

    async def test_closing_during_a_fetch_abandons_it(self) -> None:
        """A fetch still running when the verifier closes is cancelled."""
        endpoint = Slow(document())
        verifier = JWTVerifier.from_config(
            config(ttl=0.01, retry_interval=0.01), fetch=endpoint
        )

        async with verifier:
            await until(lambda: verifier._inflight is not None)

        assert verifier._inflight is None
        assert verifier._task is None


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
        verifier = JWTVerifier.from_config(config(), fetch=Endpoint(body))

        with pytest.raises(SigningKeysUnavailableError, match=message):
            await verifier.refresh()

        assert verifier.ready is False

    async def test_too_many_keys_are_refused(self) -> None:
        """A document with thousands of keys is not a key set."""
        keys = [SIGNER.public_jwk("RS256", kid=f"k{i}") for i in range(BIG)]
        body = json.dumps({"keys": keys}).encode()
        verifier = JWTVerifier.from_config(
            config(max_keys=SMALL_LIMIT), fetch=Endpoint(body)
        )

        with pytest.raises(SigningKeysUnavailableError, match="more than"):
            await verifier.refresh()

    async def test_a_key_that_is_not_a_key_is_refused(self) -> None:
        """A usable document shape with unusable contents still fails."""
        body = json.dumps(
            {"keys": [{"kty": "unheard-of", "kid": "x"}]}
        ).encode()
        verifier = JWTVerifier.from_config(config(), fetch=Endpoint(body))

        with pytest.raises(SigningKeysUnavailableError, match="no usable key"):
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
        verifier = JWTVerifier.from_config(
            config(retry_interval=0.01), fetch=endpoint
        )

        with pytest.raises(SigningKeysUnavailableError, match="no usable key"):
            await verifier.refresh()
        assert verifier.stale is True

        endpoint.body = document()
        await anyio.sleep(0.02)
        await verifier.refresh()

        assert verifier.verify(token()).subject == "user-1"

    async def test_a_key_the_core_refuses_is_skipped(self) -> None:
        """One key nothing can verify with never takes the good ones down."""
        unreadable = {
            "kty": "RSA",
            "kid": "no-modulus",
            "use": "sig",
            "alg": "RS256",
        }
        body = json.dumps(
            {"keys": [unreadable, SIGNER.public_jwk("RS256", kid="k1")]}
        ).encode()
        verifier = JWTVerifier.from_config(config(), fetch=Endpoint(body))

        await verifier.refresh()

        assert verifier.verify(token()).subject == "user-1"

    async def test_a_document_the_core_reads_no_key_from_is_refused(
        self,
    ) -> None:
        """Skipping every key leaves nothing to verify with."""
        unreadable = {
            "kty": "RSA",
            "kid": "no-modulus",
            "use": "sig",
            "alg": "RS256",
        }
        body = json.dumps({"keys": [unreadable]}).encode()
        verifier = JWTVerifier.from_config(config(), fetch=Endpoint(body))

        with pytest.raises(SigningKeysUnavailableError, match="usable key"):
            await verifier.refresh()

        assert verifier.ready is False

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
        verifier = JWTVerifier.from_config(config(), fetch=Endpoint(body))
        await verifier.refresh()

        assert verifier.verify(token(kid="sig-1")).subject == "user-1"

    async def test_a_key_without_an_alg_is_pinned(self) -> None:
        """Entra ID publishes no `alg`, so the config supplies one."""
        body = json.dumps(
            {"keys": [SIGNER.public_jwk(kid="entra-1", alg=None)]}
        ).encode()
        verifier = JWTVerifier.from_config(
            config(algorithm="RS256"), fetch=Endpoint(body)
        )
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

        with pytest.raises(SigningKeysUnavailableError, match="503"):
            await fetch_with_httpx(URL, timeout=1.0, max_bytes=1 << 20)

    async def test_an_oversized_body_is_abandoned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cap is enforced while reading, not from the declared length."""
        self.client(monkeypatch, responder(200, b"x" * OVERSIZED))

        with pytest.raises(SigningKeysUnavailableError, match="larger than"):
            await fetch_with_httpx(URL, timeout=1.0, max_bytes=100)

    async def test_an_unreachable_endpoint_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A connection that fails raises the fetcher's own error."""

        def refuse(request: httpx.Request) -> httpx.Response:
            refused = "connection refused"
            raise httpx.ConnectError(refused, request=request)

        self.client(monkeypatch, refuse)

        with pytest.raises(SigningKeysUnavailableError, match="ConnectError"):
            await fetch_with_httpx(URL, timeout=1.0, max_bytes=1 << 20)

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
        verifier = JWTVerifier.from_config(config())

        assert verifier._fetch is fetch_with_httpx


class TestEnvironment:
    """Where the keys are published can come from the deployment."""

    async def test_the_endpoint_can_come_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deployment names the JWKS endpoint and the audience."""
        monkeypatch.setenv("GREL_ENV_LOAD", "1")
        monkeypatch.setenv("GREL_JWTVERIFIER_URL", URL)
        monkeypatch.setenv("GREL_JWTVERIFIER_AUDIENCE", AUDIENCE)

        verifier = JWTVerifier.jwks(fetch=Endpoint(document()))
        await verifier.refresh()

        assert verifier.verify(token()).subject == "user-1"

    def test_the_algorithm_is_never_read_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only code chooses how a signature is checked."""
        monkeypatch.setenv("GREL_ENV_LOAD", "1")
        monkeypatch.setenv("GREL_JWTVERIFIER_ALGORITHM", "RS256")

        with pytest.raises(SettingsValidationError, match="only code"):
            JWTVerifier.jwks(URL, audience=AUDIENCE)

    def test_an_algorithm_named_in_code_is_kept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pin written in code is the choice the guard protects."""
        monkeypatch.setenv("GREL_ENV_LOAD", "1")

        verifier = JWTVerifier.jwks(URL, audience=AUDIENCE, algorithm="RS256")

        assert isinstance(verifier.config, JWKSConfig)
        assert verifier.config.algorithm == "RS256"

    async def test_a_mounted_file_paces_the_refresh_and_nothing_else(
        self,
    ) -> None:
        """The refresh interval is live, the endpoint is not."""
        verifier = JWTVerifier.jwks(
            URL,
            audience=AUDIENCE,
            name="paced",
            fetch=Endpoint(document()),
        )

        await reconfigure_all(
            {
                "GREL_JWTVERIFIER_PACED_RETRY_INTERVAL": "5",
                "GREL_JWTVERIFIER_PACED_URL": "https://elsewhere.example.com/k",
            }
        )

        config = verifier.config
        assert isinstance(config, JWKSConfig)
        assert config.retry_interval == 5  # noqa: PLR2004
        assert config.url == URL
        assert verifier._source is config
