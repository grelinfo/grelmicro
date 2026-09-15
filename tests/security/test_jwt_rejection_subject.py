"""Tests for the subject a refused token carries, and the reason it is refused.

A refusal names the caller only once the token's signature verified. Every
reason is pinned to what the verifier answered before the registered claim
checks moved into grelmicro's core, so moving them changed no refusal.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from grelmicro.security import JWTKey, JWTVerifier, TokenRejectedError
from grelmicro.security.jwt import _subject_of
from tests.security.jwt_signing import Signer, b64u, b64u_json

AUDIENCE = "grelmicro-api"
ISSUER = "https://auth.grel.info/"
HOUR = 3600
SKEW = 60
SUBJECT = "user-1"
ATTACKER_SECRET = b"attacker-secret-" + b"fedcba9876543210" * 3

SIGNER = Signer()
DROP = object()
"""Leaves a claim out of the token rather than writing it as `null`."""


def sign(payload: object, *, header: dict[str, Any] | None = None) -> str:
    """Return an `HS256` token over `payload`, which may be any JSON value."""
    head = {"alg": "HS256", "typ": "JWT", **(header or {})}
    message = f"{b64u_json(head)}.{b64u_json(payload)}"
    return f"{message}.{b64u(SIGNER.signature('HS256', message.encode()))}"


def forge(payload: object) -> str:
    """Return an `HS256` token over `payload`, signed with a key nobody trusts."""
    message = (
        f"{b64u_json({'alg': 'HS256', 'typ': 'JWT'})}.{b64u_json(payload)}"
    )
    digest = hmac.new(ATTACKER_SECRET, message.encode(), hashlib.sha256)
    return f"{message}.{b64u(digest.digest())}"


def claims(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401
    """Return a valid claim set, with `overrides` applied or dropped."""
    base: dict[str, Any] = {
        "iss": ISSUER,
        "sub": SUBJECT,
        "aud": AUDIENCE,
        "exp": int(time.time()) + HOUR,
    }
    for name, value in overrides.items():
        if value is DROP:
            base.pop(name, None)
        else:
            base[name] = value
    return base


def verifier(**options: Any) -> JWTVerifier:  # noqa: ANN401
    """Return an `HS256` verifier with the suite's policy and no cache."""
    options.setdefault("audience", AUDIENCE)
    options.setdefault("issuer", ISSUER)
    return JWTVerifier.keys(
        JWTKey(algorithm="HS256", key=SIGNER.secret),
        cache_size=0,
        **options,
    )


def refusal(subject: JWTVerifier, token: str) -> tuple[str, str | None]:
    """Return the reason and the subject a token is refused with."""
    with pytest.raises(TokenRejectedError) as caught:
        subject.verify(token)
    return caught.value.reason.value, caught.value.subject


def at_the_start_of_a_second() -> int:
    """Wait for a second to begin, and return it.

    The core reads the clock in whole seconds, so a token built on one side of
    a tick and verified on the other would move a boundary by one.
    """
    while time.time() % 1 > 0.3:  # noqa: PLR2004
        time.sleep(0.05)
    return int(time.time())


NOW = int(time.time())

REFUSED = [
    ("expired", {}, claims(exp=NOW - HOUR), "expired", SUBJECT),
    ("exp-string", {}, claims(exp="soon"), "missing-claim", SUBJECT),
    ("exp-negative", {}, claims(exp=-1), "missing-claim", SUBJECT),
    ("exp-too-large", {}, claims(exp=1e30), "missing-claim", SUBJECT),
    ("exp-null", {}, claims(exp=None), "missing-claim", SUBJECT),
    ("exp-missing", {}, claims(exp=DROP), "missing-claim", SUBJECT),
    ("exp-bool", {}, claims(exp=True), "missing-claim", SUBJECT),
    ("nbf-future", {}, claims(nbf=NOW + HOUR), "not-yet-valid", SUBJECT),
    ("nbf-string", {}, claims(nbf="later"), "invalid", SUBJECT),
    ("nbf-null", {}, claims(nbf=None), "invalid", SUBJECT),
    ("nbf-negative", {}, claims(nbf=-5), "invalid", SUBJECT),
    ("aud-wrong", {}, claims(aud="elsewhere"), "audience", SUBJECT),
    ("aud-int", {}, claims(aud=1), "missing-claim", SUBJECT),
    ("aud-int-list", {}, claims(aud=[1]), "missing-claim", SUBJECT),
    ("aud-empty-list", {}, claims(aud=[]), "audience", SUBJECT),
    ("aud-list-miss", {}, claims(aud=["a", "b"]), "audience", SUBJECT),
    ("aud-null", {}, claims(aud=None), "missing-claim", SUBJECT),
    ("aud-missing", {}, claims(aud=DROP), "missing-claim", SUBJECT),
    ("no-aud-declared", {"audience": None}, claims(), "audience", SUBJECT),
    (
        "no-aud-declared-empty-list",
        {"audience": None},
        claims(aud=[]),
        "audience",
        SUBJECT,
    ),
    ("iss-wrong", {}, claims(iss="https://evil/"), "issuer", SUBJECT),
    ("iss-list", {}, claims(iss=[ISSUER]), "issuer", SUBJECT),
    ("iss-int", {}, claims(iss=1), "missing-claim", SUBJECT),
    ("iss-null", {}, claims(iss=None), "missing-claim", SUBJECT),
    ("iss-missing", {}, claims(iss=DROP), "missing-claim", SUBJECT),
    (
        "no-iss-declared-list",
        {"issuer": None},
        claims(iss=[ISSUER]),
        "issuer",
        SUBJECT,
    ),
    ("sub-missing", {}, claims(sub=DROP), "missing-claim", None),
    ("sub-int", {}, claims(sub=5), "missing-claim", None),
    ("sub-null", {}, claims(sub=None), "missing-claim", None),
    ("sub-empty", {}, claims(sub="", exp=NOW - HOUR), "expired", None),
    (
        "required-missing",
        {"required": ["tenant"]},
        claims(),
        "missing-claim",
        SUBJECT,
    ),
    (
        "required-null",
        {"required": ["tenant"]},
        claims(tenant=None),
        "missing-claim",
        SUBJECT,
    ),
    (
        "required-nbf-missing",
        {"required": ["nbf"]},
        claims(),
        "missing-claim",
        SUBJECT,
    ),
    ("cnf", {}, claims(cnf={"jkt": "x"}), "binding", SUBJECT),
    ("iat-future", {}, claims(iat=NOW + HOUR), "not-yet-valid", SUBJECT),
    ("iat-string", {}, claims(iat="x"), "invalid", SUBJECT),
    ("jti-int", {}, claims(jti=5), "malformed", SUBJECT),
    ("jti-int-empty-sub", {}, claims(jti=5, sub=""), "malformed", None),
    (
        "expired-and-aud",
        {},
        claims(exp=NOW - HOUR, aud="x"),
        "expired",
        SUBJECT,
    ),
    (
        "expired-and-nbf",
        {},
        claims(exp=NOW - HOUR, nbf=NOW + HOUR),
        "expired",
        SUBJECT,
    ),
    (
        "exp-string-and-sub-missing",
        {},
        claims(exp="x", sub=DROP),
        "missing-claim",
        None,
    ),
    ("aud-and-iss", {}, claims(aud="x", iss="y"), "issuer", SUBJECT),
    ("cnf-and-aud", {}, claims(cnf={"a": 1}, aud="x"), "audience", SUBJECT),
    (
        "cnf-and-required",
        {"required": ["tenant"]},
        claims(cnf={"a": 1}),
        "binding",
        SUBJECT,
    ),
    (
        "nbf-string-and-exp-string",
        {},
        claims(nbf="x", exp="y"),
        "missing-claim",
        SUBJECT,
    ),
    (
        "nbf-future-and-iss",
        {},
        claims(nbf=NOW + HOUR, iss="y"),
        "not-yet-valid",
        SUBJECT,
    ),
]


@pytest.mark.parametrize(
    ("options", "payload", "reason", "subject"),
    [pytest.param(*case[1:], id=case[0]) for case in REFUSED],
)
def test_a_verified_signature_names_the_refused_subject(
    options: dict[str, Any],
    payload: dict[str, Any],
    reason: str,
    subject: str | None,
) -> None:
    """A claim that fails after the signature verified keeps its reason and names `sub`."""
    assert refusal(verifier(**options), sign(payload)) == (reason, subject)


@pytest.mark.parametrize(
    ("options", "payload"),
    [
        pytest.param({}, claims(), id="valid"),
        pytest.param({}, claims(exp=float(NOW + HOUR) + 0.4), id="exp-float"),
        pytest.param({}, claims(aud=["a", AUDIENCE]), id="aud-list-hit"),
        pytest.param(
            {"audience": None}, claims(aud=DROP), id="no-aud-declared-missing"
        ),
        pytest.param(
            {"issuer": None}, claims(iss="x"), id="no-iss-declared-other"
        ),
        pytest.param(
            {"required": ["tenant"]}, claims(tenant="t"), id="required-present"
        ),
        pytest.param({}, claims(cnf=None), id="cnf-null"),
    ],
)
def test_the_claims_the_crate_accepted_still_pass(
    options: dict[str, Any], payload: dict[str, Any]
) -> None:
    """Every token the claim checks accepted before is still accepted."""
    assert verifier(**options).verify(sign(payload)).subject == SUBJECT


def test_leeway_is_inclusive_at_both_edges() -> None:
    """A token exactly at the edge passes, and one second past it is refused."""
    skewed = verifier(leeway=SKEW)
    now = at_the_start_of_a_second()

    assert skewed.verify(sign(claims(exp=now - SKEW))).subject == SUBJECT
    assert skewed.verify(sign(claims(nbf=now + SKEW))).subject == SUBJECT
    assert refusal(skewed, sign(claims(exp=now - SKEW - 1))) == (
        "expired",
        SUBJECT,
    )
    assert refusal(skewed, sign(claims(nbf=now + SKEW + 1))) == (
        "not-yet-valid",
        SUBJECT,
    )


def test_a_token_expiring_this_second_still_passes() -> None:
    """`exp` equal to now is valid, and one second before now is expired."""
    strict = verifier()
    now = at_the_start_of_a_second()

    assert strict.verify(sign(claims(exp=now))).subject == SUBJECT
    assert refusal(strict, sign(claims(exp=now - 1))) == ("expired", SUBJECT)


@pytest.mark.parametrize(
    ("token", "reason"),
    [
        pytest.param(forge(claims()), "signature", id="forged"),
        pytest.param(
            forge(claims(exp=NOW - HOUR)), "signature", id="forged-expired"
        ),
        pytest.param(forge(claims(aud="x")), "signature", id="forged-audience"),
        pytest.param(
            sign(claims(), header={"typ": "dpop+jwt"}), "type", id="type"
        ),
        pytest.param(
            sign(claims(), header={"kid": "nope"}), "unknown-key", id="kid"
        ),
        pytest.param(
            sign(claims(), header={"alg": "HS512"}), "algorithm", id="algorithm"
        ),
        pytest.param("not.a.token", "malformed", id="malformed"),
        pytest.param(sign([SUBJECT]), "malformed", id="payload-not-object"),
    ],
)
def test_a_token_refused_at_its_signature_names_nobody(
    token: str, reason: str
) -> None:
    """A claim from a token whose signature never verified is not read."""
    assert refusal(verifier(), token) == (reason, None)


def test_a_negative_zero_expiry_reads_as_the_epoch() -> None:
    """`-0` is a float zero to the crate, so the token is expired, not malformed."""
    header = b64u_json({"alg": "HS256", "typ": "JWT"})
    payload = json.dumps(claims(exp=12345)).replace("12345", "-0")
    message = f"{header}.{b64u(payload.encode())}"
    token = f"{message}.{b64u(SIGNER.signature('HS256', message.encode()))}"

    assert refusal(verifier(), token) == ("expired", SUBJECT)


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    subject=st.text(min_size=1, max_size=40),
    expiry=st.integers(min_value=-HOUR, max_value=HOUR),
    audience=st.sampled_from([AUDIENCE, "elsewhere", DROP]),
)
def test_a_forged_token_never_names_anyone(
    subject: str, expiry: int, audience: object
) -> None:
    """Whatever a forged token claims, its refusal carries no subject."""
    payload = claims(sub=subject, exp=int(time.time()) + expiry, aud=audience)

    assert refusal(verifier(), forge(payload)) == ("signature", None)


def test_the_error_takes_a_subject_only_by_keyword() -> None:
    """A subject is named, never passed by position."""
    error = TokenRejectedError("expired", subject=SUBJECT)

    assert error.subject == SUBJECT
    assert TokenRejectedError("expired").subject is None
    with pytest.raises(TypeError):
        TokenRejectedError("expired", SUBJECT)  # type: ignore[call-arg]  # ty: ignore[too-many-positional-arguments]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"sub": SUBJECT}, SUBJECT),
        ({"sub": ""}, None),
        ({"sub": 5}, None),
        ({}, None),
    ],
)
def test_only_a_non_empty_string_subject_is_named(
    raw: dict[str, Any], expected: str | None
) -> None:
    """A subject that is not a non-empty string names nobody."""
    assert _subject_of(raw) == expected
