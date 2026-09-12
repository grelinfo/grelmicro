"""Compatibility with the JWKS documents real identity providers publish.

Each fixture is shaped the way that provider actually serves its keys, down to
the fields it includes and leaves out, because those differences are what
breaks a verifier. The key material is generated here, so nothing reaches the
network and no provider's real key is embedded.

The differences that matter:

- Entra ID publishes signing keys with no `alg`, alongside `x5c` and `x5t`.
- AWS Cognito access tokens carry no `aud`, naming the app in `client_id`.
- Keycloak serves its encryption key in the same document as its signing key.
- Google and Okta rotate by publishing the next key beside the current one.
- Auth0 serves a single RS256 key with a certificate chain attached.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from grelmicro.security import (
    JWKSConfig,
    JWKSUnavailableError,
    JWKSVerifier,
    TokenRejectedError,
)
from tests.security.jwt_signing import Signer

CURRENT = Signer()
NEXT = Signer()
HOUR = 3600
EXPECTED_KEYS = 2


def served(document: dict[str, Any]) -> Any:  # noqa: ANN401
    """Return a fetcher serving `document`."""

    async def fetch(
        url: str,  # noqa: ARG001
        *,
        timeout: float,  # noqa: ARG001, ASYNC109
        max_bytes: int,  # noqa: ARG001
    ) -> bytes:
        """Serve the document the test configured."""
        return json.dumps(document).encode()

    return fetch


def issue(signer: Signer, kid: str, issuer: str, **claims: Any) -> str:  # noqa: ANN401
    """Return a token as that provider would mint it."""
    now = int(time.time())
    payload = {"iss": issuer, "sub": "user-1", "exp": now + HOUR, "iat": now}
    payload.update(claims)
    return signer.token(payload, algorithm="RS256", header={"kid": kid})


# --- Provider documents, shaped as each one serves them --------------------


def entra_jwks() -> dict[str, Any]:
    """Entra ID: no `alg`, with a certificate thumbprint and chain."""
    key = CURRENT.public_jwk(alg=None, kid="entra-current")
    key.update({"x5t": "n0tAreal7humbprint", "x5c": ["MIIC...not-real..."]})
    return {"keys": [key]}


def cognito_jwks() -> dict[str, Any]:
    """AWS Cognito: `alg` published, two keys live at once."""
    return {
        "keys": [
            CURRENT.public_jwk("RS256", kid="cognito-1"),
            NEXT.public_jwk("RS256", kid="cognito-2"),
        ]
    }


def keycloak_jwks() -> dict[str, Any]:
    """Keycloak: the signing key beside the encryption key."""
    signing = CURRENT.public_jwk("RS256", kid="keycloak-sig")
    signing["key_ops"] = ["verify"]
    encryption = NEXT.public_jwk("RSA-OAEP", kid="keycloak-enc", use="enc")
    return {"keys": [signing, encryption]}


def auth0_jwks() -> dict[str, Any]:
    """Auth0: one RS256 key with a chain attached."""
    key = CURRENT.public_jwk("RS256", kid="auth0-current")
    key.update({"x5c": ["MIID...not-real..."], "x5t": "n0tReal"})
    return {"keys": [key]}


def okta_jwks() -> dict[str, Any]:
    """Okta: the current key and the one it will rotate to."""
    return {
        "keys": [
            CURRENT.public_jwk("RS256", kid="okta-current"),
            NEXT.public_jwk("RS256", kid="okta-next"),
        ]
    }


def google_jwks() -> dict[str, Any]:
    """Google: two RS256 keys, no `use` on either."""
    current = CURRENT.public_jwk("RS256", kid="google-1", use=None)
    following = NEXT.public_jwk("RS256", kid="google-2", use=None)
    return {"keys": [current, following]}


class TestProviders:
    """A verifier built from each provider's document accepts its tokens."""

    @pytest.mark.parametrize(
        ("document", "kid", "issuer", "algorithm"),
        [
            pytest.param(
                entra_jwks(),
                "entra-current",
                "https://login.microsoftonline.com/tid/v2.0",
                "RS256",
            ),
            pytest.param(
                cognito_jwks(),
                "cognito-1",
                "https://cognito-idp.eu-central-1.amazonaws.com/pool",
                None,
            ),
            pytest.param(
                keycloak_jwks(),
                "keycloak-sig",
                "https://sso.example.com/realms/prod",
                None,
            ),
            pytest.param(
                auth0_jwks(),
                "auth0-current",
                "https://t.eu.auth0.com/",
                None,
            ),
            pytest.param(
                okta_jwks(),
                "okta-current",
                "https://t.okta.com/oauth2/default",
                None,
            ),
            pytest.param(
                google_jwks(),
                "google-1",
                "https://accounts.google.com",
                None,
            ),
        ],
    )
    async def test_a_provider_token_verifies(
        self,
        document: dict[str, Any],
        kid: str,
        issuer: str,
        algorithm: str | None,
    ) -> None:
        """The document loads and a token signed by its key is accepted."""
        verifier = JWKSVerifier(
            JWKSConfig(
                url="https://idp.example.com/jwks.json",
                audience=["my-api"],
                issuer=[issuer],
                algorithm=algorithm,
            ),
            fetch=served(document),
        )
        await verifier.refresh()

        claims = verifier.verify(issue(CURRENT, kid, issuer, aud="my-api"))

        assert claims.subject == "user-1"
        assert claims.issuer == issuer

    async def test_keycloak_encryption_key_is_not_loaded(self) -> None:
        """A signature is never checked with the encryption key beside it."""
        issuer = "https://sso.example.com/realms/prod"
        verifier = JWKSVerifier(
            JWKSConfig(
                url="https://idp.example.com/jwks.json",
                audience=["my-api"],
                issuer=[issuer],
            ),
            fetch=served(keycloak_jwks()),
        )
        await verifier.refresh()

        with pytest.raises(TokenRejectedError) as caught:
            verifier.verify(issue(NEXT, "keycloak-enc", issuer, aud="my-api"))

        assert caught.value.reason == "unknown-key"

    async def test_a_rotation_pair_keeps_both_keys_live(self) -> None:
        """Okta and Google publish the next key early, and both must work."""
        issuer = "https://t.okta.com/oauth2/default"
        verifier = JWKSVerifier(
            JWKSConfig(
                url="https://idp.example.com/jwks.json",
                audience=["my-api"],
                issuer=[issuer],
            ),
            fetch=served(okta_jwks()),
        )
        await verifier.refresh()

        assert verifier.verify(
            issue(CURRENT, "okta-current", issuer, aud="my-api")
        )
        assert verifier.verify(issue(NEXT, "okta-next", issuer, aud="my-api"))


class TestCognitoAccessTokens:
    """Cognito access tokens carry `client_id` rather than `aud`."""

    ISSUER = "https://cognito-idp.eu-central-1.amazonaws.com/pool"

    async def verifier(self) -> JWKSVerifier:
        """Return a verifier configured the way the docs recommend."""
        built = JWKSVerifier(
            JWKSConfig(
                url="https://idp.example.com/jwks.json",
                issuer=[self.ISSUER],
                required=["exp", "token_use"],
            ),
            fetch=served(cognito_jwks()),
        )
        await built.refresh()
        return built

    async def test_an_access_token_with_no_audience_verifies(self) -> None:
        """Naming no audience is exactly the Cognito access token shape."""
        verifier = await self.verifier()

        claims = verifier.verify(
            issue(
                CURRENT,
                "cognito-1",
                self.ISSUER,
                token_use="access",
                client_id="app-1",
            )
        )

        assert claims.audience is None
        assert claims.raw["client_id"] == "app-1"

    async def test_a_token_without_token_use_is_refused(self) -> None:
        """`required` covers the claim Cognito uses to separate its tokens."""
        verifier = await self.verifier()

        with pytest.raises(TokenRejectedError) as caught:
            verifier.verify(issue(CURRENT, "cognito-1", self.ISSUER))

        assert caught.value.reason == "missing-claim"

    async def test_an_id_token_still_checks_its_audience(self) -> None:
        """An ID token does carry `aud`, and it is checked when configured."""
        verifier = JWKSVerifier(
            JWKSConfig(
                url="https://idp.example.com/jwks.json",
                issuer=[self.ISSUER],
                audience=["app-1"],
            ),
            fetch=served(cognito_jwks()),
        )
        await verifier.refresh()

        with pytest.raises(TokenRejectedError) as caught:
            verifier.verify(
                issue(CURRENT, "cognito-1", self.ISSUER, aud="another-app")
            )

        assert caught.value.reason == "audience"


class TestAwkwardDocuments:
    """Shapes a provider can serve that must not take the whole set down."""

    async def load(self, document: dict[str, Any]) -> JWKSVerifier:
        """Load `document` and return the verifier."""
        verifier = JWKSVerifier(
            JWKSConfig(
                url="https://idp.example.com/jwks.json", audience=["my-api"]
            ),
            fetch=served(document),
        )
        await verifier.refresh()
        return verifier

    async def test_a_key_type_we_cannot_read_is_skipped(self) -> None:
        """One unreadable key must not cost the keys that do work."""
        document = {
            "keys": [
                {"kty": "unheard-of", "kid": "future", "use": "sig"},
                CURRENT.public_jwk("RS256", kid="usable"),
            ]
        }

        verifier = await self.load(document)

        assert verifier.verify(
            issue(CURRENT, "usable", "https://any/", aud="my-api")
        ).subject

    async def test_a_document_of_only_unusable_keys_is_refused(self) -> None:
        """Skipping every key leaves nothing to verify with, which is an error."""
        document = {"keys": [{"kty": "unheard-of", "kid": "future"}]}
        verifier = JWKSVerifier(
            JWKSConfig(url="https://idp.example.com/jwks.json"),
            fetch=served(document),
        )

        with pytest.raises(JWKSUnavailableError, match="no usable key"):
            await verifier.refresh()

    async def test_key_ops_without_use_is_honoured(self) -> None:
        """RFC 7517 lets a key say its purpose with `key_ops` instead."""
        encrypting = NEXT.public_jwk("RS256", kid="enc-only", use=None)
        encrypting["key_ops"] = ["encrypt"]
        signing = CURRENT.public_jwk("RS256", kid="sig-only", use=None)
        signing["key_ops"] = ["verify"]

        verifier = await self.load({"keys": [encrypting, signing]})

        assert verifier.verify(
            issue(CURRENT, "sig-only", "https://any/", aud="my-api")
        ).subject
        with pytest.raises(TokenRejectedError):
            verifier.verify(
                issue(NEXT, "enc-only", "https://any/", aud="my-api")
            )

    async def test_a_key_with_no_use_and_no_key_ops_is_used(self) -> None:
        """Google publishes neither, and those keys are signing keys."""
        document = {"keys": [CURRENT.public_jwk("RS256", kid="bare", use=None)]}

        verifier = await self.load(document)

        assert verifier.verify(
            issue(CURRENT, "bare", "https://any/", aud="my-api")
        ).subject

    async def test_two_live_keys_load(self) -> None:
        """A rotation pair is the normal steady state, not an edge case."""
        verifier = await self.load(google_jwks())
        loaded = verifier._loaded()

        assert len(loaded._cache) == 0
        for kid, signer in (("google-1", CURRENT), ("google-2", NEXT)):
            assert verifier.verify(
                issue(signer, kid, "https://any/", aud="my-api")
            ).subject
        assert len(loaded._cache) == EXPECTED_KEYS
