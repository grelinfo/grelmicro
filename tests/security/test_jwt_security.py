"""Adversarial tests for JWT verification.

Each case is a token an attacker can build and a check that has to refuse it.
The classes below enumerate the surface rather than sampling it: a guard
written against one spelling of an attack leaves the other spellings open.

The property tests at the end state the two invariants the whole module rests
on. No input produces claims without a valid signature, and no input escapes
as an exception a caller is not told to catch.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from grelmicro.security import (
    JWTConfig,
    JWTKey,
    JWTVerifier,
    TokenRejectedError,
)
from tests.security.jwt_signing import Signer, b64u, b64u_json

AUDIENCE = "grelmicro-api"
ISSUER = "https://auth.grel.info/"
HOUR = 3600
LONG = 4096

SIGNER = Signer()
ATTACKER = Signer()


def claims(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401
    """Return a claim set that would be accepted if properly signed."""
    now = int(time.time())
    base = {
        "iss": ISSUER,
        "sub": "victim",
        "aud": AUDIENCE,
        "exp": now + HOUR,
        "iat": now,
    }
    base.update(overrides)
    return base


def verifier(algorithm: str = "RS256", **options: Any) -> JWTVerifier:  # noqa: ANN401
    """Return a verifier pinned to `algorithm`."""
    options.setdefault("audience", [AUDIENCE])
    options.setdefault("issuer", [ISSUER])
    return JWTVerifier(
        JWTConfig(
            keys=[
                JWTKey(algorithm=algorithm, key=SIGNER.public_pem(algorithm))
            ],
            **options,
        )
    )


def refuses(token: str, subject: JWTVerifier | None = None) -> str:
    """Assert `token` is refused, and return the reason it was refused for."""
    with pytest.raises(TokenRejectedError) as caught:
        (subject or verifier()).verify(token)
    return caught.value.reason


class TestUnsignedTokens:
    """The `alg: none` family, in every spelling it is written."""

    @pytest.mark.parametrize(
        "spelling", ["none", "None", "NONE", "nOnE", "nOnE ", " none", "NoNe"]
    )
    def test_every_spelling_of_none_is_refused(self, spelling: str) -> None:
        """Case folding is not a defence, so the algorithm is pinned instead."""
        assert refuses(SIGNER.unsigned(claims(), algorithm=spelling))

    def test_none_with_a_forged_signature_is_refused(self) -> None:
        """Carrying bytes in the signature segment changes nothing."""
        assert refuses(
            SIGNER.unsigned(claims(), algorithm="none", signature="ZmFrZQ")
        )

    def test_a_stripped_signature_is_refused(self) -> None:
        """Taking a valid token and dropping its signature is refused."""
        token = SIGNER.token(claims())
        head, payload, _ = token.split(".")

        assert refuses(f"{head}.{payload}.")

    def test_a_header_without_an_alg_is_refused(self) -> None:
        """There is no default algorithm."""
        header = b64u_json({"typ": "JWT"})
        payload = b64u_json(claims())

        assert refuses(f"{header}.{payload}.")


class TestAlgorithmConfusion:
    """Making the verifier use a key in a way it was not issued for."""

    def test_an_rsa_public_key_is_not_an_hmac_secret(self) -> None:
        """The classic confusion: sign HS256 with the RSA public key as secret.

        A verifier that read the algorithm from the token would verify this
        against material the attacker also holds. The algorithm is pinned to
        the key instead, so the token is refused.
        """
        public = SIGNER.public_pem("RS256")
        forged = SIGNER.token(
            claims(sub="attacker"),
            algorithm="HS256",
            sign_with="HS256",
            secret=public,
        )

        assert refuses(forged) == "algorithm"

    @pytest.mark.parametrize(
        ("pinned", "presented"),
        [
            ("RS256", "RS512"),
            ("RS512", "RS256"),
            ("RS256", "PS256"),
            ("PS256", "RS256"),
            ("ES256", "ES384"),
            ("RS256", "HS256"),
            ("EdDSA", "RS256"),
        ],
    )
    def test_a_different_algorithm_is_refused(
        self, pinned: str, presented: str
    ) -> None:
        """A verifier accepts one algorithm per key, and only that one."""
        token = SIGNER.token(claims(), algorithm=presented)

        assert refuses(token, verifier(pinned))

    def test_a_header_that_lies_about_its_algorithm_is_refused(self) -> None:
        """The header says RS256, the bytes were signed some other way."""
        forged = SIGNER.token(claims(), algorithm="RS256", sign_with="PS256")

        assert refuses(forged) == "signature"


class TestKeyConfusion:
    """Presenting a token the configured keys did not sign."""

    def test_another_keypair_is_refused(self) -> None:
        """A perfectly valid signature from the wrong key is still wrong."""
        assert refuses(ATTACKER.token(claims())) == "signature"

    def test_a_kid_cannot_select_a_key_that_is_not_configured(self) -> None:
        """Key selection is a lookup in a fixed map, not a path."""
        subject = JWTVerifier(
            JWTConfig(
                keys=[
                    JWTKey(
                        algorithm="RS256",
                        key=SIGNER.public_pem("RS256"),
                        kid="real",
                    )
                ],
                audience=[AUDIENCE],
                issuer=[ISSUER],
            )
        )

        for kid in (
            "../../etc/passwd",
            "real\x00evil",
            "' OR 1=1 --",
            "real/../real",
            "REAL",
            "x" * LONG,
            "",
        ):
            token = SIGNER.token(claims(), header={"kid": kid})
            assert refuses(token, subject) == "unknown-key"

    @pytest.mark.parametrize("kid", [1, 1.5, True, ["real"], {"k": "real"}])
    def test_a_kid_that_is_not_a_string_is_refused(self, kid: Any) -> None:  # noqa: ANN401
        """A `kid` of the wrong type never reaches the key map."""
        token = SIGNER.token(claims(), header={"kid": kid})

        assert refuses(token)


class TestMalformedTokens:
    """Anything that is not a well-formed JWS."""

    @pytest.mark.parametrize(
        "token",
        [
            "",
            ".",
            "..",
            "...",
            "....",
            "a",
            "a.b",
            "a.b.c.d",
            "a.b.c.d.e",
            "  ",
            "\n",
            "Bearer token",
            "null",
            "{}",
        ],
    )
    def test_a_token_of_the_wrong_shape_is_refused(self, token: str) -> None:
        """A JWS has three base64url segments and nothing else."""
        assert refuses(token)

    def test_a_five_segment_token_is_refused(self) -> None:
        """A JWE is not a JWS, and this module verifies signatures."""
        parts = [b64u_json({"alg": "RSA-OAEP", "enc": "A256GCM"})] + [
            b64u(b"x") for _ in range(4)
        ]

        assert refuses(".".join(parts))

    def test_standard_base64_is_not_base64url(self) -> None:
        """A signature re-encoded with `+` and `/` is not the same signature."""
        token = SIGNER.token(claims(sub="a+b/c" * 20))
        head, payload, signature = token.split(".")
        standard = signature.replace("-", "+").replace("_", "/")
        if standard == signature:
            pytest.skip("this signature has no url-safe characters to swap")

        assert refuses(f"{head}.{payload}.{standard}")

    def test_a_payload_that_is_not_json_is_refused(self) -> None:
        """The claims have to be a JSON object."""
        head = b64u_json({"alg": "RS256", "typ": "JWT"})
        for payload in (
            b64u(b"not json"),
            b64u(b"[]"),
            b64u(b'"a"'),
            b64u(b"1"),
        ):
            message = f"{head}.{payload}".encode()
            signature = b64u(SIGNER.signature("RS256", message))
            assert refuses(f"{head}.{payload}.{signature}")


class TestClaimTypeConfusion:
    """Claims of the right name and the wrong type."""

    @pytest.mark.parametrize(
        "exp", ["9999999999", None, True, [9999999999], {"v": 1}, "soon"]
    )
    def test_an_expiry_that_is_not_a_number_is_refused(self, exp: Any) -> None:  # noqa: ANN401
        """A token whose `exp` cannot be compared has no expiry."""
        assert refuses(SIGNER.token(claims(exp=exp)))

    @pytest.mark.parametrize("aud", [1, True, {"aud": AUDIENCE}, [], [1, 2]])
    def test_an_audience_of_the_wrong_type_never_matches(
        self,
        aud: Any,  # noqa: ANN401
    ) -> None:
        """Only a string or a list of strings can name this service."""
        assert refuses(SIGNER.token(claims(aud=aud)))

    @pytest.mark.parametrize("iss", [1, True, [ISSUER], {"iss": ISSUER}])
    def test_an_issuer_of_the_wrong_type_never_matches(self, iss: Any) -> None:  # noqa: ANN401
        """Same for the issuer."""
        assert refuses(SIGNER.token(claims(iss=iss)))

    def test_an_audience_list_that_excludes_us_is_refused(self) -> None:
        """Being in someone else's audience is not being in ours."""
        token = SIGNER.token(claims(aud=["other-api", "third-api"]))

        assert refuses(token) == "audience"

    @pytest.mark.parametrize(
        "aud", [1, True, {"aud": AUDIENCE}, [], [1, 2], "somewhere-else"]
    )
    def test_an_audience_is_refused_when_we_answer_to_none(
        self,
        aud: Any,  # noqa: ANN401
    ) -> None:
        """RFC 7519 refuses a token whose `aud` we do not identify with.

        Naming no audience means identifying with none, so every `aud` is
        somebody else's. A wrongly typed one reads as absent to the crate, so
        this is the case where it would otherwise be waved through.
        """
        open_ = verifier(audience=[], required=["exp"])

        assert refuses(SIGNER.token(claims(aud=aud)), open_) == "audience"

    def test_a_token_without_an_audience_passes_when_we_answer_to_none(
        self,
    ) -> None:
        """An AWS Cognito access token carries no `aud`, and is not forged."""
        open_ = verifier(audience=[], required=["exp"])
        token = SIGNER.token({**claims(), "aud": None})

        assert open_.verify(token).subject == "victim"

    def test_a_far_future_expiry_is_still_checked(self) -> None:
        """A huge `exp` is accepted, but every other claim still applies."""
        token = SIGNER.token(claims(exp=2**53, aud="somewhere-else"))

        assert refuses(token) == "audience"


class TestNoLeakage:
    """What the failure path is allowed to say."""

    def test_the_message_never_quotes_the_token(self) -> None:
        """The message reaches logs, and the token is a live credential."""
        token = SIGNER.token(claims(aud="somewhere-else"))
        _, payload, signature = token.split(".")

        with pytest.raises(TokenRejectedError) as caught:
            verifier().verify(token)

        rendered = f"{caught.value!s} {caught.value.args!r}"
        assert token not in rendered
        assert signature not in rendered
        assert payload not in rendered

    def test_the_message_never_quotes_the_key(self) -> None:
        """A configuration failure must not print the key material either."""
        secret = b"super-secret-hmac-key-do-not-log"
        subject = JWTVerifier(
            JWTConfig(keys=[JWTKey(algorithm="HS256", key=secret)])
        )

        with pytest.raises(TokenRejectedError) as caught:
            subject.verify("nonsense")

        assert secret.decode() not in f"{caught.value!s} {caught.value.args!r}"


class TestCacheIsNotAnOracle:
    """The cache must never turn into a way around the checks."""

    def test_a_rejected_token_is_never_cached(self) -> None:
        """A refusal leaves nothing behind for a later request to find."""
        subject = verifier()
        for token in (
            ATTACKER.token(claims()),
            SIGNER.token(claims(aud="elsewhere")),
            SIGNER.unsigned(claims()),
            "nonsense",
        ):
            with pytest.raises(TokenRejectedError):
                subject.verify(token)

        assert subject._cache == {}

    def test_two_tokens_never_share_an_entry(self) -> None:
        """Entries are keyed by the whole token, so no two tokens collide."""
        subject = verifier()
        first = SIGNER.token(claims(sub="alice"))
        second = SIGNER.token(claims(sub="bob"))

        assert subject.verify(first).subject == "alice"
        assert subject.verify(second).subject == "bob"
        assert subject.verify(first).subject == "alice"

    def test_a_cached_token_is_not_served_to_another_verifier(self) -> None:
        """Each verifier holds its own cache, so policy is never shared."""
        lenient = verifier()
        strict = verifier(audience=["different-api"])
        token = SIGNER.token(claims())
        lenient.verify(token)

        assert refuses(token, strict) == "audience"


VALID = SIGNER.token(claims())


class TestInvariants:
    """The two properties the module rests on, stated over arbitrary input."""

    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    @given(st.text(max_size=512))
    def test_arbitrary_text_never_yields_claims(self, token: str) -> None:
        """Only a validly signed token produces claims. Everything else raises."""
        try:
            verifier().verify(token)
        except TokenRejectedError:
            return
        # Reached only if the input happened to be a token we issued.
        assert token == VALID

    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    @given(st.binary(max_size=256))
    def test_arbitrary_bytes_raise_only_the_documented_error(
        self, raw: bytes
    ) -> None:
        """A caller is told to catch one error, so nothing else may escape."""
        token = raw.decode("latin-1")
        try:
            verifier().verify(token)
        except TokenRejectedError:
            return
        except Exception as error:  # noqa: BLE001
            pytest.fail(f"undocumented error {type(error).__name__}: {error}")

    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    @given(st.integers(min_value=0, max_value=len(VALID) - 1))
    def test_mutating_any_byte_breaks_the_token(self, index: int) -> None:
        """No single-character edit of a valid token survives verification."""
        original = VALID[index]
        replacement = "A" if original != "A" else "B"
        mutated = VALID[:index] + replacement + VALID[index + 1 :]

        try:
            claims_out = verifier().verify(mutated)
        except TokenRejectedError:
            return
        # A mutation inside the signature's unused trailing bits can decode to
        # the same signature, which is a property of base64 and not a defect.
        assert claims_out.raw == verifier().verify(VALID).raw

    @settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
    @given(
        st.dictionaries(
            st.text(min_size=1, max_size=12),
            st.one_of(st.text(max_size=20), st.integers(), st.booleans()),
            max_size=6,
        )
    )
    def test_extra_claims_never_change_the_verdict(
        self, extra: dict[str, Any]
    ) -> None:
        """Unknown claims are carried, never interpreted."""
        payload = claims()
        payload.update(
            {
                name: value
                for name, value in extra.items()
                if name not in payload
            }
        )
        token = SIGNER.token(payload)

        assert verifier().verify(token).subject == "victim"
