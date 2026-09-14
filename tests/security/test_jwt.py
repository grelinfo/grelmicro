"""Tests for JWT verification.

Tokens are signed by the suite's own signer rather than by a JWT library, so
a test can build the malformed and confused tokens a library refuses to emit.
The adversarial cases live in `test_jwt_security.py`.
"""

from __future__ import annotations

import inspect
import sys
import threading
import time
from collections import deque
from dataclasses import replace
from typing import Any, Literal

import pytest
from pydantic import ValidationError

from grelmicro._config import reconfigure_all
from grelmicro.errors import DependencyNotFoundError, SettingsValidationError
from grelmicro.security import (
    JWTClaims,
    JWTKey,
    JWTKeysConfig,
    JWTVerifier,
    Principal,
    TokenRejectedError,
    TokenRejectedReason,
    unverified_header,
)
from grelmicro.security.jwt import ALGORITHMS, BEARER_PREFIX
from tests.security.jwt_signing import Signer, loaded

AUDIENCE = "grelmicro-api"
ISSUER = "https://auth.grel.info/"
HOUR = 3600
DAY = 86400
CACHE_SIZE = 4
RACE_CACHE_SIZE = 8
RACE_THREADS = 12
RACE_TOKENS = 64
TTL = 300.0
SKEW = 60

SIGNER = Signer()
OTHER = Signer()


def claims(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401
    """Return the suite's default claim set, with `overrides` applied."""
    now = int(time.time())
    base = {
        "iss": ISSUER,
        "sub": "user-1",
        "aud": AUDIENCE,
        "exp": now + HOUR,
        "iat": now,
        "jti": "token-1",
        "scope": "orders:read orders:write",
    }
    base.update(overrides)
    return base


def issue(algorithm: str = "RS256", **overrides: Any) -> str:  # noqa: ANN401
    """Return a signed token carrying the default claims."""
    header = overrides.pop("header", None)
    return SIGNER.token(claims(**overrides), algorithm=algorithm, header=header)


def build(
    algorithm: str = "RS256",
    *,
    kid: str | None = None,
    signer: Signer = SIGNER,
    **options: Any,  # noqa: ANN401
) -> JWTVerifier:
    """Return a verifier for `algorithm` with the suite's default policy."""
    options.setdefault("audience", [AUDIENCE])
    options.setdefault("issuer", [ISSUER])
    return JWTVerifier.from_config(
        JWTKeysConfig(
            keys=[
                JWTKey(
                    algorithm=algorithm,  # ty: ignore[invalid-argument-type]
                    key=signer.public_pem(algorithm),
                    kid=kid,
                )
            ],
            **options,
        )
    )


@pytest.fixture
def frozen(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Pin the clock the cache reads, so TTL tests never sleep.

    Only the cache reads this clock. The core keeps its own view of the time,
    so a test that moves this one is testing the cache and nothing else.
    """
    clock = [time.time()]
    monkeypatch.setattr("grelmicro.security.jwt.time", lambda: clock[0])
    return clock


@pytest.mark.parametrize(
    "algorithm",
    [
        "HS256",
        "HS384",
        "HS512",
        "RS256",
        "RS512",
        "PS256",
        "ES256",
        "ES384",
        "EdDSA",
    ],
)
def test_verify_accepts_a_valid_token(algorithm: str) -> None:
    """Every supported algorithm verifies and returns its claims."""
    result = build(algorithm).verify(issue(algorithm))

    assert result.subject == "user-1"
    assert result.issuer == ISSUER
    assert result.audience == AUDIENCE
    assert result.token_id == "token-1"
    assert result.scopes == frozenset({"orders:read", "orders:write"})


def test_verify_lifts_the_registered_claims() -> None:
    """The named fields come from the raw claim set."""
    now = int(time.time())
    result = build().verify(issue(iat=now, exp=now + HOUR))

    assert result.issued_at == now
    assert result.expires_at == now + HOUR
    assert result.claims["scope"] == "orders:read orders:write"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"exp": int(time.time()) - HOUR}, "expired"),
        ({"nbf": int(time.time()) + HOUR}, "not-yet-valid"),
        ({"aud": "somewhere-else"}, "audience"),
        ({"iss": "https://evil.example/"}, "issuer"),
        ({"exp": None}, "missing-claim"),
    ],
)
def test_claim_checks_reject(overrides: dict[str, Any], reason: str) -> None:
    """Each registered claim check refuses the token that violates it."""
    with pytest.raises(TokenRejectedError) as caught:
        build().verify(issue(**overrides))

    assert caught.value.reason == reason


def test_leeway_forgives_a_clock_that_runs_ahead() -> None:
    """A token just past `exp` is accepted inside the configured skew."""
    token = issue(exp=int(time.time()) - 10)

    assert build(leeway=SKEW).verify(token).subject == "user-1"


def test_naming_a_claim_does_not_drop_the_expiry_requirement() -> None:
    """`required` adds to the policy, it never replaces what it already has.

    Reading it the other way would mean an operator tightening a policy by
    naming a claim had quietly turned the expiry check off, and a token
    carrying no `exp` would then be accepted for as long as its key is
    published.
    """
    policy = JWTKeysConfig(
        keys=[JWTKey(algorithm="RS256", key=SIGNER.public_pem("RS256"))],
        audience=[AUDIENCE],
        required=["tenant"],
    )

    assert policy.enforced_claims() == ["exp", "tenant", "aud"]

    with pytest.raises(TokenRejectedError) as caught:
        JWTVerifier.from_config(policy).verify(issue(exp=None, tenant="acme"))
    assert caught.value.reason == "missing-claim"


def test_an_empty_required_list_still_requires_an_expiry() -> None:
    """There is no spelling of `required` that turns the expiry check off."""
    policy = JWTKeysConfig(
        keys=[JWTKey(algorithm="RS256", key=SIGNER.public_pem("RS256"))],
        audience=[AUDIENCE],
        required=[],
    )

    assert policy.enforced_claims() == ["exp", "aud"]

    with pytest.raises(TokenRejectedError):
        JWTVerifier.from_config(policy).verify(issue(exp=None))


def test_a_required_claim_written_as_null_is_absent() -> None:
    """`null` is not a value, and the registered claims are read that way."""
    verifier = build(required=["tenant"])

    with pytest.raises(TokenRejectedError) as caught:
        verifier.verify(issue(tenant=None))

    assert caught.value.reason == "missing-claim"


def test_a_claim_outside_the_registered_set_can_be_required() -> None:
    """`required` covers any claim, not only the ones RFC 7519 registers."""
    verifier = build(required=["exp", "tenant"])

    assert verifier.verify(issue(tenant="acme")).claims["tenant"] == "acme"

    with pytest.raises(TokenRejectedError) as caught:
        verifier.verify(issue())
    assert caught.value.reason == "missing-claim"


def test_an_audience_is_required_once_the_token_carries_one() -> None:
    """RFC 7519 refuses a token whose `aud` the service does not answer to."""
    verifier = JWTVerifier.from_config(
        JWTKeysConfig(
            keys=[JWTKey(algorithm="RS256", key=SIGNER.public_pem("RS256"))],
            audience=None,
        )
    )

    with pytest.raises(TokenRejectedError) as caught:
        verifier.verify(issue())

    assert caught.value.reason == "audience"


def test_declaring_an_audience_requires_the_claim() -> None:
    """A token that omits `aud` must not slip past a configured audience.

    Otherwise naming an audience would check the tokens that carry one and
    silently wave through the tokens that do not, which is the wrong way
    round.
    """
    with pytest.raises(TokenRejectedError) as caught:
        build().verify(issue(aud=None))

    assert caught.value.reason == "missing-claim"


def test_declaring_an_issuer_requires_the_claim() -> None:
    """The same for the issuer."""
    with pytest.raises(TokenRejectedError) as caught:
        build().verify(issue(iss=None))

    assert caught.value.reason == "missing-claim"


def test_a_token_with_no_audience_verifies_when_none_is_declared() -> None:
    """An AWS Cognito access token names the app in `client_id`, not `aud`.

    Setting `audience=None` is what accepts it, and is what the Cognito
    recipe in the docs does.
    """
    verifier = JWTVerifier.from_config(
        JWTKeysConfig(
            keys=[JWTKey(algorithm="RS256", key=SIGNER.public_pem("RS256"))],
            audience=None,
            issuer=[ISSUER],
        )
    )

    assert verifier.verify(issue(aud=None)).audience is None


def test_an_audience_list_matches_on_any_member() -> None:
    """`aud` may be an array, and one match is enough."""
    token = issue(aud=["other-api", AUDIENCE])

    assert build().verify(token).audience == ["other-api", AUDIENCE]


class TestKeySelection:
    """Key selection by `kid`, and the rotation it exists to support."""

    def test_a_kid_selects_its_key(self) -> None:
        """A token names the key it was signed with."""
        verifier = JWTVerifier.from_config(
            JWTKeysConfig(
                keys=[
                    JWTKey(
                        algorithm="RS256",
                        key=SIGNER.public_pem("RS256"),
                        kid="current",
                    ),
                    JWTKey(
                        algorithm="RS256",
                        key=OTHER.public_pem("RS256"),
                        kid="previous",
                    ),
                ],
                audience=[AUDIENCE],
                issuer=[ISSUER],
            )
        )

        assert (
            verifier.verify(issue(header={"kid": "current"})).subject
            == "user-1"
        )

    def test_both_keys_of_a_rotation_stay_live(self) -> None:
        """A rotation does not reject the tokens issued before it."""
        verifier = JWTVerifier.from_config(
            JWTKeysConfig(
                keys=[
                    JWTKey(
                        algorithm="RS256",
                        key=SIGNER.public_pem("RS256"),
                        kid="new",
                    ),
                    JWTKey(
                        algorithm="RS256",
                        key=OTHER.public_pem("RS256"),
                        kid="old",
                    ),
                ],
                audience=[AUDIENCE],
                issuer=[ISSUER],
            )
        )
        old = OTHER.token(
            claims(sub="before"), algorithm="RS256", header={"kid": "old"}
        )

        assert verifier.verify(issue(header={"kid": "new"})).subject == "user-1"
        assert verifier.verify(old).subject == "before"

    def test_an_unknown_kid_is_rejected(self) -> None:
        """No configured key matches, so nothing verifies the token."""
        with pytest.raises(TokenRejectedError) as caught:
            build(kid="current").verify(issue(header={"kid": "rotated-away"}))

        assert caught.value.reason == "unknown-key"

    def test_a_token_without_a_kid_needs_a_default_key(self) -> None:
        """Two keyed keys and no default leaves nothing to try."""
        verifier = JWTVerifier.from_config(
            JWTKeysConfig(
                keys=[
                    JWTKey(
                        algorithm="RS256",
                        key=SIGNER.public_pem("RS256"),
                        kid="a",
                    ),
                    JWTKey(
                        algorithm="RS256",
                        key=OTHER.public_pem("RS256"),
                        kid="b",
                    ),
                ],
                audience=[AUDIENCE],
            )
        )

        with pytest.raises(TokenRejectedError) as caught:
            verifier.verify(issue())

        assert caught.value.reason == "unknown-key"

    def test_a_single_key_serves_a_token_that_names_it(self) -> None:
        """One key and a matching `kid` is the ordinary single-key setup."""
        assert build(kid="only").verify(issue(header={"kid": "only"})).subject


class TestJWKS:
    """Keys as an OIDC provider publishes them."""

    def test_a_jwk_carrying_its_alg_is_used(self) -> None:
        """AWS Cognito publishes `alg` on every key."""
        key = JWTKey.jwk(SIGNER.public_jwk("RS256", kid="cognito-1"))

        assert key.algorithm == "RS256"
        assert key.kid == "cognito-1"
        assert key.format == "jwk"

    def test_a_jwk_without_an_alg_infers_one(self) -> None:
        """Entra ID publishes signing keys with no `alg`."""
        key = JWTKey.jwk(
            SIGNER.public_jwk(alg=None, kid="entra-1", x5t="thumbprint")
        )

        assert key.algorithm == "RS256"
        assert key.kid == "entra-1"

    def test_an_explicit_algorithm_fills_the_gap(self) -> None:
        """A caller can pin the algorithm the provider left out."""
        key = JWTKey.jwk(
            SIGNER.public_jwk(alg=None, kid="entra-1"), algorithm="PS256"
        )

        assert key.algorithm == "PS256"

    def test_a_jwk_of_an_unknown_type_is_refused(self) -> None:
        """Nothing can be inferred, so the caller has to say."""
        with pytest.raises(SettingsValidationError):
            JWTKey.jwk({"kty": "unheard-of", "kid": "x"})

    def test_a_symmetric_jwk_is_refused(self) -> None:
        """A JWK is a published key, so it never carries a shared secret."""
        with pytest.raises(ValidationError):
            JWTKey.jwk({"kty": "oct", "alg": "HS256", "k": "c2VjcmV0"})

    def test_a_jwks_builds_a_verifier(self) -> None:
        """A fetched JWKS document goes straight into a config."""
        jwks = {"keys": [SIGNER.public_jwk("RS256", kid="signing-1")]}
        verifier = JWTVerifier.from_config(
            JWTKeysConfig.from_jwks(jwks, audience=[AUDIENCE], issuer=[ISSUER])
        )

        token = issue(header={"kid": "signing-1"})

        assert verifier.verify(token).subject == "user-1"

    def test_encryption_keys_are_skipped(self) -> None:
        """A signature is never verified with an encryption key."""
        jwks = {
            "keys": [
                SIGNER.public_jwk("RS256", kid="sig-1"),
                SIGNER.public_jwk("RS256", kid="enc-1", use="enc"),
            ]
        }

        config = JWTKeysConfig.from_jwks(jwks, audience=[AUDIENCE])

        assert [key.kid for key in config.keys] == ["sig-1"]

    def test_a_key_that_cannot_be_read_is_skipped(self) -> None:
        """One unreadable key must not take the whole key set down.

        A provider is free to publish a key type this does not read. Failing
        the document would stop authentication over a key nothing was going
        to be verified with anyway.
        """
        jwks = {
            "keys": [
                {"kty": "EC", "use": "sig", "kid": "no-curve"},
                SIGNER.public_jwk("RS256", kid="sig-1"),
            ]
        }

        config = JWTKeysConfig.from_jwks(jwks, audience=[AUDIENCE])

        assert [key.kid for key in config.keys] == ["sig-1"]

    def test_a_jwks_with_no_usable_key_is_refused(self) -> None:
        """A verifier with no key can verify nothing."""
        with pytest.raises(ValidationError):
            JWTKeysConfig.from_jwks({"keys": []})


class TestKeys:
    """Building keys, and the key material a key refuses."""

    def test_a_pem_key_verifies(self) -> None:
        """A PEM given as text is read the same as one given as bytes."""
        key = JWTKey.pem(SIGNER.public_pem("ES256").decode(), algorithm="ES256")
        verifier = JWTVerifier.from_config(
            JWTKeysConfig(keys=[key], audience=[AUDIENCE], issuer=[ISSUER])
        )

        assert key.format == "pem"
        assert verifier.verify(issue("ES256")).subject == "user-1"

    def test_a_secret_key_verifies(self) -> None:
        """A shared secret verifies the tokens it signed."""
        key = JWTKey.secret(SIGNER.secret, algorithm="HS512", kid="shared")
        verifier = JWTVerifier.from_config(
            JWTKeysConfig(keys=[key], audience=[AUDIENCE], issuer=[ISSUER])
        )

        assert key.format == "secret"
        token = issue("HS512", header={"kid": "shared"})
        assert verifier.verify(token).subject == "user-1"

    @pytest.mark.parametrize(
        ("algorithm", "size"), [("HS256", 32), ("HS384", 48), ("HS512", 64)]
    )
    def test_a_secret_as_long_as_its_hash_is_accepted(
        self, algorithm: Literal["HS256", "HS384", "HS512"], size: int
    ) -> None:
        """The hash output is the floor RFC 7518 sets, and it is enough."""
        assert JWTKey.secret(b"s" * size, algorithm=algorithm).kid is None

    @pytest.mark.parametrize(
        ("algorithm", "size"), [("HS256", 32), ("HS384", 48), ("HS512", 64)]
    )
    def test_a_secret_shorter_than_its_hash_is_refused(
        self, algorithm: Literal["HS256", "HS384", "HS512"], size: int
    ) -> None:
        """One byte short is a secret RFC 7518 forbids."""
        with pytest.raises(ValidationError, match="RFC 7518"):
            JWTKey.secret(b"s" * (size - 1), algorithm=algorithm)

    def test_a_pem_is_never_read_as_a_secret(self) -> None:
        """The HMAC confusion, refused where the key is written."""
        with pytest.raises(ValidationError, match="takes a secret"):
            JWTKey(
                algorithm="HS256",
                key=SIGNER.public_pem("RS256"),
                format="pem",
            )

    def test_a_secret_never_verifies_an_asymmetric_algorithm(self) -> None:
        """A secret goes with an `HS*` algorithm and nothing else."""
        with pytest.raises(ValidationError, match="takes a secret"):
            JWTKey(algorithm="RS256", key=SIGNER.secret, format="secret")

    @pytest.mark.parametrize("algorithm", ["HS256", "RS256"])
    def test_the_constructor_reads_the_format_from_the_algorithm(
        self, algorithm: Literal["HS256", "RS256"]
    ) -> None:
        """A key that arrives as data needs no format for the common case."""
        key = JWTKey(algorithm=algorithm, key=SIGNER.public_pem(algorithm))
        verifier = JWTVerifier.from_config(
            JWTKeysConfig(keys=[key], audience=[AUDIENCE])
        )

        assert key.format is None
        assert verifier.verify(issue(algorithm)).subject == "user-1"

    def test_the_key_never_appears_in_a_repr_or_a_dump(self) -> None:
        """Key material is a secret wherever the key is rendered."""
        secret = b"never-print-this-secret-0123456789abcdef"
        key = JWTKey.secret(secret, algorithm="HS256")

        for rendered in (
            repr(key),
            str(key),
            key.model_dump_json(),
            repr(JWTKeysConfig(keys=[key], audience=None)),
        ):
            assert secret.decode() not in rendered

    def test_a_key_cannot_be_changed_once_built(self) -> None:
        """A verified policy never changes under a verifier holding it."""
        key = JWTKey.pem(SIGNER.public_pem("RS256"), algorithm="RS256")

        with pytest.raises(ValidationError):
            key.kid = "other"  # ty: ignore[invalid-assignment]


class TestAuthorizationHeader:
    """Reading the bearer token out of an `Authorization` header."""

    def test_a_bearer_header_verifies(self) -> None:
        """The scheme is stripped and the token behind it is verified."""
        assert build().verify_header(f"{BEARER_PREFIX}{issue()}").subject

    @pytest.mark.parametrize("scheme", ["bearer", "BEARER", "BeArEr"])
    def test_the_scheme_is_read_without_regard_to_case(
        self, scheme: str
    ) -> None:
        """RFC 7235 makes the scheme case-insensitive, and proxies rewrite it."""
        assert build().verify_header(f"{scheme} {issue()}").subject

    @pytest.mark.parametrize(
        "header",
        [None, "", "Basic abc", "Bearer", "BearerX token", "Bearer\ttoken"],
    )
    def test_anything_else_is_rejected(self, header: str | None) -> None:
        """Only `Bearer <token>` carries a token."""
        with pytest.raises(TokenRejectedError) as caught:
            build().verify_header(header)

        assert caught.value.reason == "scheme"


class TestUnverifiedHeader:
    """Reading the routing fields without trusting them."""

    def test_it_returns_alg_and_kid(self) -> None:
        """The header names the algorithm and the key."""
        header = unverified_header(issue(header={"kid": "current"}))

        assert header == {"alg": "RS256", "kid": "current"}

    def test_a_malformed_token_is_rejected(self) -> None:
        """There is no header to read."""
        with pytest.raises(TokenRejectedError) as caught:
            unverified_header("nonsense")

        assert caught.value.reason == "malformed"


class TestCache:
    """The verified-token cache, which must never outlive what it caches."""

    def test_the_claims_handed_back_cannot_be_written_into(self) -> None:
        """A cached claim set is shared, so writing into it would reach others.

        Every request presenting the token gets the same object while it is
        cached, so a caller enriching or narrowing `raw` would change what a
        later request is authorized as.
        """
        verifier = build()
        token = issue()
        claims = verifier.verify(token)

        with pytest.raises(TypeError):
            # Both checkers refuse this, which is the point: the claim set
            # is read-only in the types as well as at runtime. The runtime
            # check stays, because a caller without a type checker is
            # exactly who this protects.
            claims.claims["scope"] = "admin"  # type: ignore[index]  # ty: ignore[invalid-assignment]

        assert verifier.verify(token).scopes == {
            "orders:read",
            "orders:write",
        }

    def test_a_repeated_token_is_served_from_the_cache(self) -> None:
        """The second verification returns the very same claims object."""
        verifier = build()
        token = issue()

        assert verifier.verify(token) is verifier.verify(token)

    def test_an_entry_never_outlives_the_token(self) -> None:
        """The deadline is bounded by the token's own expiry plus the skew."""
        verifier = build(leeway=SKEW, cache_ttl=DAY)
        token = issue()

        result = verifier.verify(token)

        assert result.expires_at is not None
        deadline = loaded(verifier).cache[verifier._key(token)][0]
        assert deadline <= result.expires_at + SKEW

    def test_the_ttl_bounds_a_long_lived_token(self) -> None:
        """A day-long token is held for the TTL, not for the day."""
        verifier = build(cache_ttl=TTL)
        token = issue(exp=int(time.time()) + DAY)

        verifier.verify(token)

        deadline = loaded(verifier).cache[verifier._key(token)][0]
        assert deadline <= time.time() + TTL
        assert deadline < int(time.time()) + DAY

    def test_an_expired_entry_is_dropped_and_verified_again(
        self, frozen: list[float]
    ) -> None:
        """Past its deadline the entry goes, and the core answers instead."""
        verifier = build(cache_ttl=TTL)
        token = issue(exp=int(time.time()) + DAY)
        first = verifier.verify(token)

        frozen[0] += TTL + 1
        second = verifier.verify(token)

        assert first is not second
        assert len(loaded(verifier).cache) == 1

    def test_sha256_keys_are_the_default(self) -> None:
        """A verifier holds digests unless it is asked for raw tokens."""
        token = issue()
        default = build()
        default.verify(token)

        assert isinstance(next(iter(loaded(default).cache)), bytes)
        assert token not in loaded(default).cache

    def test_sha256_keys_keep_the_token_out_of_memory(self) -> None:
        """The entry is keyed by a digest, so no live token is held."""
        verifier = build(cache_key="sha256")
        token = issue()

        first = verifier.verify(token)

        assert verifier.verify(token) is first
        assert token not in loaded(verifier).cache
        assert len(loaded(verifier).cache) == 1

    def test_sha256_keys_still_expire(self, frozen: list[float]) -> None:
        """The deadline applies whichever key strategy is in use."""
        verifier = build(cache_key="sha256", cache_ttl=TTL)
        token = issue(exp=int(time.time()) + DAY)
        first = verifier.verify(token)

        frozen[0] += TTL + 1

        assert verifier.verify(token) is not first

    def test_two_tokens_never_share_a_digest_entry(self) -> None:
        """Distinct tokens hash to distinct keys."""
        verifier = build(cache_key="sha256")

        assert verifier.verify(issue(sub="alice")).subject == "alice"
        assert verifier.verify(issue(sub="bob")).subject == "bob"

    def test_a_claim_set_without_an_expiry_is_never_cached(self) -> None:
        """Nothing would bound how long the entry stays valid.

        No token reaches this now, because `exp` is always enforced. The
        guard stays because it is what makes the entry deadline safe, so it
        is checked directly rather than left to be true by accident.
        """
        verifier = build()
        undated = JWTClaims(
            claims={},
            subject="nobody",
            issuer=None,
            audience=None,
            expires_at=None,
            issued_at=None,
            token_id=None,
        )

        verifier._store(loaded(verifier), "any-key", undated)

        assert loaded(verifier).cache == {}

    def test_a_zero_size_cache_holds_nothing(self) -> None:
        """`cache_size=0` turns the cache off."""
        verifier = build(cache_size=0)
        verifier.verify(issue())

        assert loaded(verifier).cache == {}

    def test_a_zero_ttl_holds_nothing(self) -> None:
        """A deadline that has already passed is not worth storing."""
        verifier = build(cache_ttl=0)
        verifier.verify(issue())

        assert loaded(verifier).cache == {}

    def test_making_room_drains_expired_entries_first(
        self, frozen: list[float]
    ) -> None:
        """Dead entries are reclaimed before a live one is evicted."""
        verifier = build(cache_size=CACHE_SIZE, cache_ttl=TTL)
        for index in range(CACHE_SIZE):
            verifier.verify(
                issue(sub=f"user-{index}", exp=int(time.time()) + DAY)
            )
        assert len(loaded(verifier).cache) == CACHE_SIZE

        frozen[0] += TTL + 1
        verifier.verify(issue(sub="fresh", exp=int(time.time()) + DAY))

        assert len(loaded(verifier).cache) == 1

    def test_a_full_cache_of_live_entries_evicts_the_oldest(self) -> None:
        """With nothing to drain, the oldest insertion makes room."""
        verifier = build(cache_size=CACHE_SIZE)
        # Addressed through `_key`, because the default cache holds digests.
        tokens = [issue(sub=f"user-{index}") for index in range(CACHE_SIZE)]
        for token in tokens:
            verifier.verify(token)

        verifier.verify(issue(sub="one-more"))

        assert len(loaded(verifier).cache) == CACHE_SIZE
        assert verifier._key(tokens[0]) not in loaded(verifier).cache


class TestCacheUnderThreads:
    """The cache is shared across a thread pool, so it must never raise.

    A `dict` cannot be walked while another thread writes to it, which is why
    the eviction order lives in a deque beside the cache and every operation
    on either is one the interpreter applies whole.
    """

    def test_concurrent_verification_never_raises(self) -> None:
        """Twelve threads, a cache too small to hold them, nothing escapes."""
        verifier = build(cache_size=RACE_CACHE_SIZE, cache_ttl=0.002)
        tokens = [
            issue(sub=f"user-{index}").encode() for index in range(RACE_TOKENS)
        ]
        escaped: list[str] = []
        stop = threading.Event()

        def hammer() -> None:
            while not stop.is_set():
                for raw in tokens:
                    try:
                        verifier.verify(raw.decode())
                    except TokenRejectedError:
                        continue
                    except Exception as error:  # noqa: BLE001
                        escaped.append(type(error).__name__)
                        return

        workers = [threading.Thread(target=hammer) for _ in range(RACE_THREADS)]
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-9)
        try:
            for worker in workers:
                worker.start()
            time.sleep(0.8)
            stop.set()
            for worker in workers:
                worker.join(timeout=5)
        finally:
            sys.setswitchinterval(previous)

        assert escaped == []
        assert len(loaded(verifier).cache) <= RACE_CACHE_SIZE

    def test_an_emptied_queue_stops_the_eviction_loop(self) -> None:
        """Another thread can drain the queue while this one is evicting."""

        class Drained(deque):  # type: ignore[type-arg]
            """A queue that is emptied the moment it is read from."""

            def popleft(self) -> object:
                raise IndexError

        verifier = build(cache_size=1)
        verifier.verify(issue(sub="first"))
        keys = loaded(verifier)
        verifier._keys = replace(keys, order=Drained(keys.order))

        verifier.verify(issue(sub="second"))

        assert len(loaded(verifier).cache) >= 1


class TestConfiguration:
    """What the configuration refuses, and why."""

    def test_an_unsupported_algorithm_is_refused(self) -> None:
        """`none` is not a signature algorithm."""
        with pytest.raises(ValidationError):
            JWTKey(algorithm="none", key=SIGNER.public_pem("RS256"))  # ty: ignore[invalid-argument-type]

    def test_an_unknown_key_format_is_refused(self) -> None:
        """The core reads PEM and JWK, nothing else."""
        with pytest.raises(ValidationError):
            JWTKey(algorithm="RS256", key=b"x", format="der")  # ty: ignore[invalid-argument-type]

    def test_an_unknown_cache_key_strategy_is_refused(self) -> None:
        """The verifier keys by the token or by a digest, nothing else."""
        with pytest.raises(ValidationError):
            JWTKeysConfig(
                keys=[
                    JWTKey(algorithm="RS256", key=SIGNER.public_pem("RS256"))
                ],
                audience=AUDIENCE,
                cache_key="md5",
            )

    def test_an_empty_key_set_is_refused(self) -> None:
        """A verifier with no key can verify nothing."""
        with pytest.raises(ValidationError):
            JWTKeysConfig(keys=[], audience=AUDIENCE)

    def test_two_keys_claiming_one_kid_are_refused(self) -> None:
        """One of them would silently never be used."""
        with pytest.raises(ValidationError):
            JWTKeysConfig(
                keys=[
                    JWTKey(
                        algorithm="RS256",
                        key=SIGNER.public_pem("RS256"),
                        kid="same",
                    ),
                    JWTKey(
                        algorithm="RS256",
                        key=OTHER.public_pem("RS256"),
                        kid="same",
                    ),
                ],
                audience=AUDIENCE,
            )

    def test_two_keys_without_a_kid_are_refused(self) -> None:
        """Only one key can serve tokens that name none."""
        with pytest.raises(ValidationError):
            JWTKeysConfig(
                keys=[
                    JWTKey(algorithm="RS256", key=SIGNER.public_pem("RS256")),
                    JWTKey(algorithm="ES256", key=SIGNER.public_pem("ES256")),
                ],
                audience=AUDIENCE,
            )

    @pytest.mark.parametrize("setting", ["leeway", "cache_size", "cache_ttl"])
    def test_negative_settings_are_refused(self, setting: str) -> None:
        """None of these means anything below zero."""
        policy: dict[str, Any] = {
            "keys": [JWTKey(algorithm="RS256", key=SIGNER.public_pem("RS256"))],
            "audience": AUDIENCE,
            setting: -1,
        }

        with pytest.raises(ValidationError):
            JWTKeysConfig(**policy)

    def test_key_material_that_is_not_a_key_is_refused(self) -> None:
        """The failure names the setting, not a stack trace from the core."""
        with pytest.raises(SettingsValidationError):
            JWTVerifier.from_config(
                JWTKeysConfig(
                    keys=[JWTKey(algorithm="RS256", key=b"not a pem")],
                    audience=AUDIENCE,
                )
            )

    def test_a_jwk_that_is_not_a_key_is_refused(self) -> None:
        """Same for key material published as JSON."""
        with pytest.raises(SettingsValidationError):
            JWTVerifier.from_config(
                JWTKeysConfig(
                    keys=[JWTKey(algorithm="RS256", key=b"{}", format="jwk")],
                    audience=AUDIENCE,
                )
            )

    def test_every_accepted_algorithm_is_a_signature_algorithm(self) -> None:
        """`ALGORITHMS` is the set the core accepts, and `none` is not in it."""
        assert "none" not in ALGORITHMS
        for algorithm in ALGORITHMS:
            JWTKey(algorithm=algorithm, key=SIGNER.secret)


class TestConstruction:
    """How a verifier is built, and what building one insists on."""

    @staticmethod
    def key() -> JWTKey:
        """Return the suite's RSA key, built the way a caller builds one."""
        return JWTKey.pem(SIGNER.public_pem("RS256"), algorithm="RS256")

    def test_there_is_no_bare_constructor(self) -> None:
        """The factory names where the keys come from, so none is assumed."""
        with pytest.raises(TypeError, match=r"JWTVerifier\.keys"):
            JWTVerifier()

    def test_keys_takes_a_single_audience_and_issuer(self) -> None:
        """The common case needs no list around one value."""
        verifier = JWTVerifier.keys(
            self.key(), audience=AUDIENCE, issuer=ISSUER
        )

        assert verifier.verify(issue()).subject == "user-1"

    def test_keys_takes_several_audiences(self) -> None:
        """A token naming any accepted audience passes."""
        verifier = JWTVerifier.keys(
            self.key(), audience=["other-api", AUDIENCE]
        )

        assert verifier.verify(issue()).subject == "user-1"

    def test_from_config_takes_a_config_as_it_is(self) -> None:
        """A config assembled elsewhere, from YAML or a vault, verifies."""
        config = JWTKeysConfig(
            keys=[self.key()], audience=AUDIENCE, issuer=ISSUER
        )

        assert config.audience == [AUDIENCE]
        assert config.issuer == [ISSUER]
        assert JWTVerifier.from_config(config).verify(issue()).subject == (
            "user-1"
        )

    def test_an_audience_must_be_given(self) -> None:
        """A resource server has to say which tokens were issued for it."""
        with pytest.raises(ValidationError, match="audience"):
            JWTKeysConfig(keys=[self.key()])  # ty: ignore[missing-argument]

    def test_an_empty_audience_is_refused(self) -> None:
        """An empty list says neither which audience nor none at all."""
        with pytest.raises(ValidationError, match="None to answer"):
            JWTKeysConfig(keys=[self.key()], audience=[])

    def test_none_answers_to_no_audience(self) -> None:
        """RFC 7519 refuses a token naming an audience the service is not."""
        verifier = JWTVerifier.keys(self.key(), audience=None)

        with pytest.raises(TokenRejectedError) as caught:
            verifier.verify(issue())

        assert caught.value.reason == "audience"
        assert verifier.verify(issue(aud=None)).subject == "user-1"


class TestErrors:
    """The error surface a caller branches on."""

    def test_an_unknown_reason_reads_as_invalid(self) -> None:
        """A tag this module does not know is never passed on unchecked."""
        error = TokenRejectedError("something-new")

        assert error.reason is TokenRejectedReason.INVALID
        assert str(error) == "The token is not valid."

    def test_a_reason_compares_equal_to_its_tag(self) -> None:
        """Code that branched on the string keeps working."""
        error = TokenRejectedError("expired")

        assert error.reason is TokenRejectedReason.EXPIRED
        assert error.reason == "expired"

    @pytest.mark.parametrize("reason", list(TokenRejectedReason))
    def test_every_reason_has_its_own_message(
        self, reason: TokenRejectedReason
    ) -> None:
        """No reason borrows the sentence of the generic one."""
        message = str(TokenRejectedError(reason))

        assert message.endswith(".")
        assert (reason is TokenRejectedReason.INVALID) == (
            message == "The token is not valid."
        )

    def test_a_missing_core_says_what_to_install(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The extra is named, so the fix is in the message."""
        monkeypatch.setitem(sys.modules, "grelmicro_core", None)

        with pytest.raises(DependencyNotFoundError, match="grelmicro-core"):
            build()


class TestClaims:
    """The claims the verifier hands back, and the scopes they grant."""

    def test_scopes_split_a_string_on_whitespace(self) -> None:
        """A scope claim is a space-delimited list, per RFC 6749."""
        claims = build().verify(issue(scope="a  b\tc"))

        assert claims.scopes == frozenset({"a", "b", "c"})

    def test_scopes_read_an_array_of_strings(self) -> None:
        """Okta writes `scp` as an array."""
        claims = build().verify(issue(scope=None, scp=["a", "b"]))

        assert claims.scopes == frozenset({"a", "b"})

    def test_scp_is_read_when_scope_is_absent(self) -> None:
        """Microsoft Entra ID writes `scp` as a string."""
        claims = build().verify(issue(scope=None, scp="a b"))

        assert claims.scopes == frozenset({"a", "b"})

    def test_the_first_claim_present_decides(self) -> None:
        """A `scope` of the wrong shape grants nothing, even beside `scp`."""
        claims = build().verify(issue(scope=42, scp="a"))

        assert claims.scopes == frozenset()

    @pytest.mark.parametrize("scope", [42, True, ["a", 1], {"a": "b"}])
    def test_a_claim_of_the_wrong_shape_grants_nothing(
        self,
        scope: Any,  # noqa: ANN401
    ) -> None:
        """Only a string or an array of strings grants a scope."""
        assert build().verify(issue(scope=scope)).scopes == frozenset()

    def test_no_scope_claim_grants_nothing(self) -> None:
        """A token that carries no scope claim is granted none."""
        assert build().verify(issue(scope=None)).scopes == frozenset()

    def test_the_claims_to_read_can_be_chosen(self) -> None:
        """A provider that writes `permissions` is read from there."""
        verifier = build(scope_claims=["permissions"])

        claims = verifier.verify(issue(permissions=["orders:read"]))

        assert claims.scopes == frozenset({"orders:read"})

    def test_a_single_claim_name_needs_no_list(self) -> None:
        """The factory takes one name the way it takes one audience."""
        verifier = JWTVerifier.keys(
            JWTKey.pem(SIGNER.public_pem("RS256"), algorithm="RS256"),
            audience=AUDIENCE,
            scope_claims="permissions",
        )

        claims = verifier.verify(issue(permissions="orders:write"))

        assert claims.scopes == frozenset({"orders:write"})

    def test_an_empty_list_of_scope_claims_is_refused(self) -> None:
        """A policy that reads no claim would grant no scope to anyone."""
        with pytest.raises(ValidationError, match="scope_claims"):
            JWTKeysConfig(
                keys=[
                    JWTKey.pem(SIGNER.public_pem("RS256"), algorithm="RS256")
                ],
                audience=AUDIENCE,
                scope_claims=[],
            )

    def test_the_claims_answer_for_the_caller(self) -> None:
        """A verified token is a `Principal`, and reads as Starlette's user."""
        claims = build().verify(issue())
        principal: Principal = claims

        assert principal.is_authenticated is True
        assert principal.subject == "user-1"
        assert principal.claims["jti"] == "token-1"
        assert claims.identity == claims.display_name == "user-1"

    def test_a_token_with_no_subject_has_an_empty_identity(self) -> None:
        """Starlette reads these as strings, so a missing subject is empty."""
        claims = build().verify(issue(sub=None))

        assert claims.identity == ""
        assert claims.display_name == ""


class TestEnvironment:
    """Settings a deployment supplies, and the ones only code may choose."""

    @staticmethod
    def key() -> JWTKey:
        """Return the suite's RSA key, built the way a caller builds one."""
        return JWTKey.pem(SIGNER.public_pem("RS256"), algorithm="RS256")

    def test_the_environment_says_whose_tokens_to_trust(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deployment names the audience and the issuer."""
        monkeypatch.setenv("GREL_ENV_LOAD", "1")
        monkeypatch.setenv("GREL_JWTVERIFIER_AUDIENCE", AUDIENCE)
        monkeypatch.setenv("GREL_JWTVERIFIER_ISSUER", ISSUER)

        verifier = JWTVerifier.keys(self.key())

        assert verifier.config.audience == [AUDIENCE]
        assert verifier.config.issuer == [ISSUER]
        assert verifier.verify(issue()).subject == "user-1"

    def test_a_list_is_written_with_commas_or_as_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both forms an operator writes by hand read the same."""
        monkeypatch.setenv("GREL_ENV_LOAD", "1")
        monkeypatch.setenv(
            "GREL_JWTVERIFIER_AUDIENCE", f'["other-api", "{AUDIENCE}"]'
        )
        monkeypatch.setenv("GREL_JWTVERIFIER_REQUIRED", "exp, jti")

        verifier = JWTVerifier.keys(self.key())

        assert verifier.config.audience == ["other-api", AUDIENCE]
        assert verifier.config.required == ["exp", "jti"]

    def test_a_keyword_wins_over_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What the code says is what runs."""
        monkeypatch.setenv("GREL_ENV_LOAD", "1")
        monkeypatch.setenv("GREL_JWTVERIFIER_AUDIENCE", "other-api")

        verifier = JWTVerifier.keys(self.key(), audience=AUDIENCE)

        assert verifier.config.audience == [AUDIENCE]

    def test_an_audience_nobody_supplies_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Left to the environment, the audience is still required."""
        monkeypatch.setenv("GREL_ENV_LOAD", "1")

        with pytest.raises(SettingsValidationError, match="audience"):
            JWTVerifier.keys(self.key())

    def test_only_code_answers_to_no_audience(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`None` in code wins over a variable naming an audience."""
        monkeypatch.setenv("GREL_ENV_LOAD", "1")
        monkeypatch.setenv("GREL_JWTVERIFIER_AUDIENCE", AUDIENCE)

        verifier = JWTVerifier.keys(self.key(), audience=None)

        assert verifier.config.audience is None

    def test_no_variable_turns_the_audience_check_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty variable is refused rather than read as no audience."""
        monkeypatch.setenv("GREL_ENV_LOAD", "1")
        monkeypatch.setenv("GREL_JWTVERIFIER_AUDIENCE", "")

        with pytest.raises(SettingsValidationError, match="audience"):
            JWTVerifier.keys(self.key())

    def test_a_named_verifier_reads_its_own_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second verifier is addressed by its name."""
        monkeypatch.setenv("GREL_ENV_LOAD", "1")
        monkeypatch.setenv("GREL_JWTVERIFIER_AUDIENCE", "default-api")
        monkeypatch.setenv("GREL_JWTVERIFIER_PARTNER_AUDIENCE", AUDIENCE)

        partner = JWTVerifier.keys(self.key(), name="partner")

        assert partner.config.audience == [AUDIENCE]

    def test_a_named_verifier_never_reads_the_kind_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One verifier's trust settings never reach another."""
        monkeypatch.setenv("GREL_ENV_LOAD", "1")
        monkeypatch.setenv("GREL_JWTVERIFIER_AUDIENCE", AUDIENCE)

        with pytest.raises(SettingsValidationError, match="audience"):
            JWTVerifier.keys(self.key(), name="partner")

    def test_env_load_false_reads_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The per-call switch wins over the process-wide flag."""
        monkeypatch.setenv("GREL_ENV_LOAD", "1")
        monkeypatch.setenv("GREL_JWTVERIFIER_LEEWAY", "30")

        verifier = JWTVerifier.keys(
            self.key(), audience=AUDIENCE, env_load=False
        )

        assert verifier.config.leeway == 0

    async def test_a_mounted_file_resizes_the_cache_and_nothing_else(
        self,
    ) -> None:
        """A file tunes what a verification costs, never whose tokens pass."""
        verifier = JWTVerifier.keys(
            self.key(), audience=AUDIENCE, name="reloaded"
        )

        await reconfigure_all(
            {
                "GREL_JWTVERIFIER_RELOADED_CACHE_SIZE": "7",
                "GREL_JWTVERIFIER_RELOADED_AUDIENCE": "other-api",
                "GREL_JWTVERIFIER_RELOADED_LEEWAY": "600",
            }
        )

        assert verifier.config.cache_size == 7  # noqa: PLR2004
        assert verifier._cache_size == 7  # noqa: PLR2004
        assert verifier.config.audience == [AUDIENCE]
        assert verifier.config.leeway == 0

    def test_a_verifier_from_a_config_is_never_reloaded(self) -> None:
        """The declarative door is the whole truth, so no file reaches it."""
        verifier = JWTVerifier.from_config(
            JWTKeysConfig(keys=[self.key()], audience=AUDIENCE)
        )

        assert verifier._env_prefix is None

    def test_the_signature_says_the_audience_is_left_unset(self) -> None:
        """The default an editor and the API reference show reads as a name."""
        parameters = inspect.signature(JWTVerifier.jwks).parameters

        assert repr(parameters["audience"].default) == "UNSET"

    async def test_a_reload_never_changes_whose_tokens_pass(self) -> None:
        """A config handed to reconfigure with a new audience is refused."""
        verifier = JWTVerifier.keys(self.key(), audience=AUDIENCE)
        changed = verifier.config.model_copy(update={"audience": ["other-api"]})

        with pytest.raises(ValueError, match="audience"):
            await verifier.reconfigure(changed)

        assert verifier.config.audience == [AUDIENCE]
