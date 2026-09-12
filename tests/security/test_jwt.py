"""Tests for JWT verification.

Tokens are signed by the suite's own signer rather than by a JWT library, so
a test can build the malformed and confused tokens a library refuses to emit.
The adversarial cases live in `test_jwt_security.py`.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from typing import Any

import pytest
from pydantic import ValidationError

from grelmicro.errors import DependencyNotFoundError, SettingsValidationError
from grelmicro.security import (
    JWTClaims,
    JWTConfig,
    JWTKey,
    JWTVerifier,
    TokenRejectedError,
)
from grelmicro.security.jwt import ALGORITHMS, BEARER_PREFIX
from tests.security.jwt_signing import Signer

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
    return JWTVerifier(
        JWTConfig(
            keys=[
                JWTKey(
                    algorithm=algorithm,
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
    assert result.raw["scope"] == "orders:read orders:write"


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
    policy = JWTConfig(
        keys=[JWTKey(algorithm="RS256", key=SIGNER.public_pem("RS256"))],
        audience=[AUDIENCE],
        required=["tenant"],
    )

    assert policy.enforced_claims() == ["exp", "tenant", "aud"]

    with pytest.raises(TokenRejectedError) as caught:
        JWTVerifier(policy).verify(issue(exp=None, tenant="acme"))
    assert caught.value.reason == "missing-claim"


def test_an_empty_required_list_still_requires_an_expiry() -> None:
    """There is no spelling of `required` that turns the expiry check off."""
    policy = JWTConfig(
        keys=[JWTKey(algorithm="RS256", key=SIGNER.public_pem("RS256"))],
        audience=[AUDIENCE],
        required=[],
    )

    assert policy.enforced_claims() == ["exp", "aud"]

    with pytest.raises(TokenRejectedError):
        JWTVerifier(policy).verify(issue(exp=None))


def test_a_required_claim_written_as_null_is_absent() -> None:
    """`null` is not a value, and the registered claims are read that way."""
    verifier = build(required=["tenant"])

    with pytest.raises(TokenRejectedError) as caught:
        verifier.verify(issue(tenant=None))

    assert caught.value.reason == "missing-claim"


def test_a_claim_outside_the_registered_set_can_be_required() -> None:
    """`required` covers any claim, not only the ones RFC 7519 registers."""
    verifier = build(required=["exp", "tenant"])

    assert verifier.verify(issue(tenant="acme")).raw["tenant"] == "acme"

    with pytest.raises(TokenRejectedError) as caught:
        verifier.verify(issue())
    assert caught.value.reason == "missing-claim"


def test_an_audience_is_required_once_the_token_carries_one() -> None:
    """RFC 7519 refuses a token whose `aud` the service does not answer to."""
    verifier = JWTVerifier(
        JWTConfig(
            keys=[JWTKey(algorithm="RS256", key=SIGNER.public_pem("RS256"))]
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

    Leaving `audience` empty is what accepts it, and is what the Cognito
    recipe in the docs does.
    """
    verifier = JWTVerifier(
        JWTConfig(
            keys=[JWTKey(algorithm="RS256", key=SIGNER.public_pem("RS256"))],
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
        verifier = JWTVerifier(
            JWTConfig(
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
        verifier = JWTVerifier(
            JWTConfig(
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
        verifier = JWTVerifier(
            JWTConfig(
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
        key = JWTKey.from_jwk(SIGNER.public_jwk("RS256", kid="cognito-1"))

        assert key.algorithm == "RS256"
        assert key.kid == "cognito-1"
        assert key.format == "jwk"

    def test_a_jwk_without_an_alg_infers_one(self) -> None:
        """Entra ID publishes signing keys with no `alg`."""
        key = JWTKey.from_jwk(
            SIGNER.public_jwk(alg=None, kid="entra-1", x5t="thumbprint")
        )

        assert key.algorithm == "RS256"
        assert key.kid == "entra-1"

    def test_an_explicit_algorithm_fills_the_gap(self) -> None:
        """A caller can pin the algorithm the provider left out."""
        key = JWTKey.from_jwk(
            SIGNER.public_jwk(alg=None, kid="entra-1"), algorithm="PS256"
        )

        assert key.algorithm == "PS256"

    def test_a_jwk_of_an_unknown_type_is_refused(self) -> None:
        """Nothing can be inferred, so the caller has to say."""
        with pytest.raises(SettingsValidationError):
            JWTKey.from_jwk({"kty": "unheard-of", "kid": "x"})

    def test_a_jwks_builds_a_verifier(self) -> None:
        """A fetched JWKS document goes straight into a config."""
        jwks = {"keys": [SIGNER.public_jwk("RS256", kid="signing-1")]}
        verifier = JWTVerifier(
            JWTConfig.from_jwks(jwks, audience=[AUDIENCE], issuer=[ISSUER])
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

        config = JWTConfig.from_jwks(jwks, audience=[AUDIENCE])

        assert [key.kid for key in config.keys] == ["sig-1"]

    def test_a_jwks_with_no_usable_key_is_refused(self) -> None:
        """A verifier with no key can verify nothing."""
        with pytest.raises(ValidationError):
            JWTConfig.from_jwks({"keys": []})


class TestAuthorizationHeader:
    """Reading the bearer token out of an `Authorization` header."""

    def test_a_bearer_header_verifies(self) -> None:
        """The scheme is stripped and the token behind it is verified."""
        assert build().verify_header(f"{BEARER_PREFIX}{issue()}").subject

    @pytest.mark.parametrize(
        "header",
        [None, "", "Basic abc", "bearer lowercase", "Bearer", "BearerX token"],
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
        header = build().unverified_header(issue(header={"kid": "current"}))

        assert header == {"alg": "RS256", "kid": "current"}

    def test_a_malformed_token_is_rejected(self) -> None:
        """There is no header to read."""
        with pytest.raises(TokenRejectedError) as caught:
            build().unverified_header("nonsense")

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
            claims.raw["scope"] = "admin"  # type: ignore[index]  # ty: ignore[invalid-assignment]

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
        deadline = verifier._cache[verifier._key(token)][0]
        assert deadline <= result.expires_at + SKEW

    def test_the_ttl_bounds_a_long_lived_token(self) -> None:
        """A day-long token is held for the TTL, not for the day."""
        verifier = build(cache_ttl=TTL)
        token = issue(exp=int(time.time()) + DAY)

        verifier.verify(token)

        deadline = verifier._cache[verifier._key(token)][0]
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
        assert len(verifier._cache) == 1

    def test_sha256_keys_are_the_default(self) -> None:
        """A verifier holds digests unless it is asked for raw tokens."""
        token = issue()
        default = build()
        default.verify(token)

        assert isinstance(next(iter(default._cache)), bytes)
        assert token not in default._cache

    def test_sha256_keys_keep_the_token_out_of_memory(self) -> None:
        """The entry is keyed by a digest, so no live token is held."""
        verifier = build(cache_key="sha256")
        token = issue()

        first = verifier.verify(token)

        assert verifier.verify(token) is first
        assert token not in verifier._cache
        assert len(verifier._cache) == 1

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
            raw={},
            subject="nobody",
            issuer=None,
            audience=None,
            expires_at=None,
            issued_at=None,
            token_id=None,
        )

        verifier._store("any-key", undated)

        assert verifier._cache == {}

    def test_a_zero_size_cache_holds_nothing(self) -> None:
        """`cache_size=0` turns the cache off."""
        verifier = build(cache_size=0)
        verifier.verify(issue())

        assert verifier._cache == {}

    def test_a_zero_ttl_holds_nothing(self) -> None:
        """A deadline that has already passed is not worth storing."""
        verifier = build(cache_ttl=0)
        verifier.verify(issue())

        assert verifier._cache == {}

    def test_making_room_drains_expired_entries_first(
        self, frozen: list[float]
    ) -> None:
        """Dead entries are reclaimed before a live one is evicted."""
        verifier = build(cache_size=CACHE_SIZE, cache_ttl=TTL)
        for index in range(CACHE_SIZE):
            verifier.verify(
                issue(sub=f"user-{index}", exp=int(time.time()) + DAY)
            )
        assert len(verifier._cache) == CACHE_SIZE

        frozen[0] += TTL + 1
        verifier.verify(issue(sub="fresh", exp=int(time.time()) + DAY))

        assert len(verifier._cache) == 1

    def test_a_full_cache_of_live_entries_evicts_the_oldest(self) -> None:
        """With nothing to drain, the oldest insertion makes room."""
        verifier = build(cache_size=CACHE_SIZE)
        # Addressed through `_key`, because the default cache holds digests.
        tokens = [issue(sub=f"user-{index}") for index in range(CACHE_SIZE)]
        for token in tokens:
            verifier.verify(token)

        verifier.verify(issue(sub="one-more"))

        assert len(verifier._cache) == CACHE_SIZE
        assert verifier._key(tokens[0]) not in verifier._cache


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
        assert len(verifier._cache) <= RACE_CACHE_SIZE

    def test_an_emptied_queue_stops_the_eviction_loop(self) -> None:
        """Another thread can drain the queue while this one is evicting."""

        class Drained(deque):  # type: ignore[type-arg]
            """A queue that is emptied the moment it is read from."""

            def popleft(self) -> object:
                raise IndexError

        verifier = build(cache_size=1)
        verifier.verify(issue(sub="first"))
        verifier._order = Drained(verifier._order)

        verifier.verify(issue(sub="second"))

        assert len(verifier._cache) >= 1


class TestConfiguration:
    """What the configuration refuses, and why."""

    def test_an_unsupported_algorithm_is_refused(self) -> None:
        """`none` is not a signature algorithm."""
        with pytest.raises(ValidationError):
            JWTKey(algorithm="none", key=SIGNER.public_pem("RS256"))

    def test_an_unknown_key_format_is_refused(self) -> None:
        """The core reads PEM and JWK, nothing else."""
        with pytest.raises(ValidationError):
            JWTKey(algorithm="RS256", key=b"x", format="der")

    def test_an_unknown_cache_key_strategy_is_refused(self) -> None:
        """The verifier keys by the token or by a digest, nothing else."""
        with pytest.raises(ValidationError):
            JWTConfig(
                keys=[
                    JWTKey(algorithm="RS256", key=SIGNER.public_pem("RS256"))
                ],
                cache_key="md5",
            )

    def test_an_empty_key_set_is_refused(self) -> None:
        """A verifier with no key can verify nothing."""
        with pytest.raises(ValidationError):
            JWTConfig(keys=[])

    def test_two_keys_claiming_one_kid_are_refused(self) -> None:
        """One of them would silently never be used."""
        with pytest.raises(ValidationError):
            JWTConfig(
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
                ]
            )

    def test_two_keys_without_a_kid_are_refused(self) -> None:
        """Only one key can serve tokens that name none."""
        with pytest.raises(ValidationError):
            JWTConfig(
                keys=[
                    JWTKey(algorithm="RS256", key=SIGNER.public_pem("RS256")),
                    JWTKey(algorithm="ES256", key=SIGNER.public_pem("ES256")),
                ]
            )

    @pytest.mark.parametrize("setting", ["leeway", "cache_size", "cache_ttl"])
    def test_negative_settings_are_refused(self, setting: str) -> None:
        """None of these means anything below zero."""
        policy: dict[str, Any] = {
            "keys": [JWTKey(algorithm="RS256", key=SIGNER.public_pem("RS256"))],
            setting: -1,
        }

        with pytest.raises(ValidationError):
            JWTConfig(**policy)

    def test_key_material_that_is_not_a_key_is_refused(self) -> None:
        """The failure names the setting, not a stack trace from the core."""
        with pytest.raises(SettingsValidationError):
            JWTVerifier(
                JWTConfig(keys=[JWTKey(algorithm="RS256", key=b"not a pem")])
            )

    def test_a_jwk_that_is_not_a_key_is_refused(self) -> None:
        """Same for key material published as JSON."""
        with pytest.raises(SettingsValidationError):
            JWTVerifier(
                JWTConfig(
                    keys=[JWTKey(algorithm="RS256", key=b"{}", format="jwk")]
                )
            )

    def test_every_accepted_algorithm_is_a_signature_algorithm(self) -> None:
        """`ALGORITHMS` is the set the core accepts, and `none` is not in it."""
        assert "none" not in ALGORITHMS
        for algorithm in ALGORITHMS:
            JWTKey(algorithm=algorithm, key=SIGNER.secret)


class TestErrors:
    """The error surface a caller branches on."""

    def test_an_unknown_reason_falls_back_to_a_generic_message(self) -> None:
        """A tag the table does not carry still produces a usable error."""
        error = TokenRejectedError("something-new")

        assert error.reason == "something-new"
        assert str(error) == "The token is not valid."

    def test_a_missing_core_says_what_to_install(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The extra is named, so the fix is in the message."""
        monkeypatch.setitem(sys.modules, "grelmicro_core", None)

        with pytest.raises(DependencyNotFoundError, match="grelmicro-core"):
            build()


class TestClaims:
    """The claim wrapper the verifier hands back."""

    def wrap(self, **raw: Any) -> JWTClaims:  # noqa: ANN401
        """Build a claims object straight from `raw`."""
        return JWTClaims(
            raw=raw,
            subject=None,
            issuer=None,
            audience=None,
            expires_at=None,
            issued_at=None,
            token_id=None,
        )

    def test_scopes_splits_on_whitespace(self) -> None:
        """A scope claim is a space-delimited list, per RFC 6749."""
        assert self.wrap(scope="a  b\tc").scopes == frozenset({"a", "b", "c"})

    @pytest.mark.parametrize("scope", [None, 42, ["a", "b"], ""])
    def test_scopes_is_empty_when_the_claim_is_not_a_string(
        self,
        scope: Any,  # noqa: ANN401
    ) -> None:
        """A claim of the wrong shape grants nothing."""
        raw = {} if scope is None else {"scope": scope}

        assert self.wrap(**raw).scopes == frozenset()
