"""Tests for finding an issuer's signing keys through its metadata.

No provider is reached. A fetcher serving a fixed set of URLs stands in for
one, so each case says exactly which documents the provider publishes.
"""

from __future__ import annotations

import json
import time
from typing import Any

import anyio
import pytest
from pydantic import ValidationError

from grelmicro._config import reconfigure_all
from grelmicro.errors import SettingsValidationError
from grelmicro.security import (
    DiscoveryConfig,
    JWTVerifier,
    SigningKeysUnavailableError,
    TokenRejectedError,
    TokenRejectedReason,
)
from tests.security.jwt_signing import Signer

ISSUER = "https://auth.grel.info/"
AUDIENCE = "grelmicro-api"
JWKS = "https://auth.grel.info/keys"
OAUTH = "https://auth.grel.info/.well-known/oauth-authorization-server"
OIDC = "https://auth.grel.info/.well-known/openid-configuration"
REALM = "https://auth.grel.info/realms/grel"
REALM_OAUTH = (
    "https://auth.grel.info/.well-known/oauth-authorization-server/realms/grel"
)
REALM_OIDC = (
    "https://auth.grel.info/realms/grel/.well-known/openid-configuration"
)
HOUR = 3600
SHORT_TTL = 0.01
LIVE_TTL = 5
SIGNER = Signer()


def metadata(issuer: str = ISSUER, jwks_uri: str = JWKS) -> bytes:
    """Return a metadata document naming `issuer` and its key set."""
    return json.dumps({"issuer": issuer, "jwks_uri": jwks_uri}).encode()


def key_set() -> bytes:
    """Return the key set the provider publishes."""
    return json.dumps({"keys": [SIGNER.public_jwk("RS256", kid="k1")]}).encode()


def token(issuer: str = ISSUER) -> str:
    """Return a token the provider's key signed for `issuer`."""
    now = int(time.time())
    payload = {
        "iss": issuer,
        "sub": "user-1",
        "aud": AUDIENCE,
        "exp": now + HOUR,
        "iat": now,
    }
    return SIGNER.token(payload, algorithm="RS256", header={"kid": "k1"})


class Provider:
    """A fetcher serving a fixed set of documents, and `404` for the rest."""

    def __init__(self, documents: dict[str, bytes | Exception]) -> None:
        """Serve `documents`, keyed by URL."""
        self.documents = documents
        self.calls: list[str] = []

    async def __call__(
        self,
        url: str,
        *,
        timeout: float,  # noqa: ARG002, ASYNC109
        max_bytes: int,  # noqa: ARG002
    ) -> bytes:
        """Serve the document at `url`, recording the call."""
        self.calls.append(url)
        served = self.documents.get(url)
        if served is None:
            answered = "endpoint answered 404"
            raise SigningKeysUnavailableError(answered)
        if isinstance(served, Exception):
            raise served
        return served


def discovering(
    provider: Provider,
    issuer: str = ISSUER,
    **settings: Any,  # noqa: ANN401
) -> JWTVerifier:
    """Return a verifier discovering `issuer` through `provider`."""
    config = DiscoveryConfig(issuer=issuer, audience=AUDIENCE, **settings)
    return JWTVerifier.from_config(config, fetch=provider)


class TestConfiguration:
    """What an issuer to discover must look like."""

    @pytest.mark.parametrize(
        "issuer",
        [
            [],
            ["https://auth.grel.info/", "https://other.grel.info/"],
            ["http://auth.grel.info/"],
            ["https://auth.grel.info/?tenant=grel"],
            ["https://auth.grel.info/#keys"],
            ["auth.grel.info"],
        ],
    )
    def test_only_one_https_issuer_is_accepted(self, issuer: list[str]) -> None:
        """The issuer decides whose keys are fetched, so it is checked."""
        with pytest.raises(ValidationError, match="issuer"):
            DiscoveryConfig(issuer=issuer, audience=AUDIENCE)

    def test_it_carries_the_fetch_settings(self) -> None:
        """A discovering verifier is paced like one given a JWKS URL."""
        config = DiscoveryConfig(issuer=ISSUER, audience=AUDIENCE, ttl=LIVE_TTL)

        assert config.issuer == [ISSUER]
        assert config.ttl == LIVE_TTL
        assert config.timeout > 0


class TestDiscovery:
    """Which documents are read, and what they must say."""

    async def test_openid_connect_discovery_alone(self) -> None:
        """A provider publishing only OpenID Connect discovery is found."""
        provider = Provider({OIDC: metadata(), JWKS: key_set()})
        verifier = discovering(provider)

        await verifier.refresh()

        assert verifier.verify(token()).subject == "user-1"
        assert provider.calls == [OAUTH, OIDC, JWKS]

    async def test_authorization_server_metadata_alone(self) -> None:
        """A provider publishing only RFC 8414 metadata is found first."""
        provider = Provider({OAUTH: metadata(), JWKS: key_set()})
        verifier = discovering(provider)

        await verifier.refresh()

        assert verifier.ready is True
        assert provider.calls == [OAUTH, JWKS]

    async def test_an_issuer_with_a_path(self) -> None:
        """RFC 8414 inserts the segment, OpenID Connect appends it."""
        provider = Provider(
            {REALM_OIDC: metadata(issuer=REALM), JWKS: key_set()}
        )
        verifier = discovering(provider, issuer=REALM)

        await verifier.refresh()

        assert verifier.verify(token(issuer=REALM)).subject == "user-1"
        assert provider.calls == [REALM_OAUTH, REALM_OIDC, JWKS]

    async def test_a_rotation_fetches_the_key_set_alone(self) -> None:
        """The metadata is read once per `ttl`, not on every key refresh."""
        provider = Provider({OIDC: metadata(), JWKS: key_set()})
        verifier = discovering(provider)
        await verifier.refresh()
        provider.calls.clear()

        await verifier.refresh(force=True)

        assert provider.calls == [JWKS]

    async def test_the_document_that_answered_is_tried_first(self) -> None:
        """Once found, the provider is not asked for the other one again."""
        provider = Provider({OIDC: metadata(), JWKS: key_set()})
        verifier = discovering(provider, ttl=SHORT_TTL)
        await verifier.refresh()
        await anyio.sleep(SHORT_TTL * 2)
        provider.calls.clear()

        await verifier.refresh(force=True)

        assert provider.calls == [OIDC, JWKS]

    async def test_every_token_must_carry_the_issuer(self) -> None:
        """A token from another issuer is refused, whoever signed it."""
        verifier = discovering(Provider({OIDC: metadata(), JWKS: key_set()}))
        await verifier.refresh()

        with pytest.raises(TokenRejectedError) as caught:
            verifier.verify(token(issuer="https://other.grel.info/"))

        assert caught.value.reason is TokenRejectedReason.ISSUER

    async def test_a_document_for_another_issuer_is_refused(self) -> None:
        """It is never passed over for the next document either."""
        provider = Provider(
            {
                OAUTH: metadata(issuer="https://other.grel.info/"),
                OIDC: metadata(),
                JWKS: key_set(),
            }
        )
        verifier = discovering(provider)

        with pytest.raises(SigningKeysUnavailableError, match="another issuer"):
            await verifier.refresh()

        assert verifier.ready is False
        assert provider.calls == [OAUTH]

    async def test_a_key_set_not_served_over_https_is_refused(self) -> None:
        """The metadata cannot downgrade the channel the keys arrive on."""
        provider = Provider(
            {OIDC: metadata(jwks_uri="http://auth.grel.info/keys")}
        )

        with pytest.raises(SigningKeysUnavailableError, match="https jwks_uri"):
            await discovering(provider).refresh()

    @pytest.mark.parametrize(
        ("body", "reason"),
        [(b"not json", "not valid JSON"), (b"[]", "not a JSON object")],
    )
    async def test_a_document_that_is_not_metadata_is_refused(
        self, body: bytes, reason: str
    ) -> None:
        """An error page served with `200` names no key set."""
        provider = Provider({OAUTH: body})

        with pytest.raises(SigningKeysUnavailableError, match=reason):
            await discovering(provider).refresh()

    async def test_no_document_names_every_url_tried(self) -> None:
        """The error says where the metadata was looked for."""
        provider = Provider({})

        with pytest.raises(SigningKeysUnavailableError) as caught:
            await discovering(provider).refresh()

        assert OAUTH in str(caught.value)
        assert OIDC in str(caught.value)

    async def test_a_fetcher_failing_its_own_way_falls_through(self) -> None:
        """A transport error on one document leaves the other to try."""
        provider = Provider(
            {
                OAUTH: OSError("connection reset"),
                OIDC: metadata(),
                JWKS: key_set(),
            }
        )
        verifier = discovering(provider)

        await verifier.refresh()

        assert verifier.ready is True

    async def test_a_failed_discovery_keeps_the_loaded_keys(self) -> None:
        """A provider going wrong later does not take verification down."""
        provider = Provider({OIDC: metadata(), JWKS: key_set()})
        verifier = discovering(provider, ttl=SHORT_TTL)
        await verifier.refresh()
        await anyio.sleep(SHORT_TTL * 2)
        provider.documents[OIDC] = metadata(issuer="https://other.grel.info/")

        with pytest.raises(SigningKeysUnavailableError, match="another issuer"):
            await verifier.refresh(force=True)

        assert verifier.verify(token()).subject == "user-1"

    async def test_opening_the_verifier_discovers_its_keys(self) -> None:
        """`async with` finds the keys before the first request."""
        provider = Provider({OIDC: metadata(), JWKS: key_set()})

        async with discovering(provider) as verifier:
            assert verifier.ready is True


class TestFactory:
    """`JWTVerifier.discover`, from code and from the deployment."""

    async def test_discover_builds_the_verifier(self) -> None:
        """One URL is all the code names."""
        provider = Provider({OIDC: metadata(), JWKS: key_set()})
        verifier = JWTVerifier.discover(
            ISSUER, audience=AUDIENCE, fetch=provider
        )

        await verifier.refresh()

        assert isinstance(verifier.config, DiscoveryConfig)
        assert verifier.verify(token()).subject == "user-1"

    def test_the_issuer_can_come_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deployment names the issuer at startup."""
        monkeypatch.setenv("GREL_ENV_LOAD", "1")
        monkeypatch.setenv("GREL_JWTVERIFIER_ISSUER", ISSUER)
        monkeypatch.setenv("GREL_JWTVERIFIER_AUDIENCE", AUDIENCE)

        verifier = JWTVerifier.discover()

        assert verifier.config.issuer == [ISSUER]

    def test_an_issuer_nobody_supplies_is_refused(self) -> None:
        """Discovery has nothing to start from without one."""
        with pytest.raises(SettingsValidationError, match="issuer"):
            JWTVerifier.discover(audience=AUDIENCE, env_load=False)

    def test_the_algorithm_is_never_read_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only code chooses how a signature is checked."""
        monkeypatch.setenv("GREL_ENV_LOAD", "1")
        monkeypatch.setenv("GREL_JWTVERIFIER_ALGORITHM", "RS256")

        with pytest.raises(SettingsValidationError, match="only code"):
            JWTVerifier.discover(ISSUER, audience=AUDIENCE)

    async def test_a_mounted_file_paces_the_refresh_and_nothing_else(
        self,
    ) -> None:
        """The key `ttl` is live, the issuer is not."""
        verifier = JWTVerifier.discover(
            ISSUER,
            audience=AUDIENCE,
            name="found",
            fetch=Provider({}),
        )

        await reconfigure_all(
            {
                "GREL_JWTVERIFIER_FOUND_TTL": str(LIVE_TTL),
                "GREL_JWTVERIFIER_FOUND_ISSUER": "https://other.grel.info/",
            }
        )

        config = verifier.config
        assert isinstance(config, DiscoveryConfig)
        assert config.ttl == LIVE_TTL
        assert config.issuer == [ISSUER]
        assert verifier._source is config
