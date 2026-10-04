"""Tests for `FakeVerifier` and `fake_claims`."""

import math
import time
from http import HTTPStatus
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from grelmicro import Grelmicro
from grelmicro.http import AuthenticatedRequests, ErrorResponses
from grelmicro.integrations.fastapi import Authenticated, CurrentPrincipal
from grelmicro.security import TokenRejectedError, TokenRejectedReason
from grelmicro.testing import FakeVerifier, fake_claims

_EXPIRES_AT = 2_000_000_000
"""An `exp` far in the future, in whole seconds."""

_ISSUED_AT = 1_900_000_000
"""An `iat` before `_EXPIRES_AT`, in whole seconds."""

_YEAR_2100 = 4_102_444_800
"""The default `exp`: 2100-01-01T00:00:00Z."""


def test_fake_claims_sets_subject_and_scopes() -> None:
    """fake_claims builds claims with the subject, the scopes and the extras."""
    # Act
    claims = fake_claims("alice", "orders:read", "orders:write", tenant="acme")

    # Assert
    assert claims.subject == "alice"
    assert claims.scopes == frozenset({"orders:read", "orders:write"})
    assert claims.claims == {
        "sub": "alice",
        "scope": "orders:read orders:write",
        "tenant": "acme",
        "exp": _YEAR_2100,
    }
    assert claims.issuer is None
    assert claims.expires_at == _YEAR_2100


def test_fake_verifier_verify_returns_the_claims_of_a_known_token() -> None:
    """FakeVerifier.verify returns the claims the token stands for."""
    # Arrange
    alice = fake_claims("alice")
    verifier = FakeVerifier(alice=alice)

    # Act / Assert
    assert verifier.verify("alice") is alice


def test_fake_verifier_accepts_a_mapping_of_tokens() -> None:
    """FakeVerifier takes tokens that are not identifiers as a mapping."""
    # Arrange
    bob = fake_claims("bob")
    verifier = FakeVerifier({"token-of-bob": bob})

    # Act / Assert
    assert verifier.verify("token-of-bob") is bob


def test_fake_verifier_verify_rejects_an_unknown_token() -> None:
    """FakeVerifier.verify refuses a token it does not know as invalid."""
    # Arrange
    verifier = FakeVerifier(alice=fake_claims("alice"))

    # Act / Assert
    with pytest.raises(TokenRejectedError) as excinfo:
        verifier.verify("mallory")
    assert excinfo.value.reason is TokenRejectedReason.INVALID


@pytest.mark.parametrize(
    "header", [None, "", "alice", "Basic alice", "Bearer "]
)
def test_fake_verifier_verify_header_rejects_a_header_without_bearer(
    header: str | None,
) -> None:
    """FakeVerifier.verify_header refuses a header with no bearer token."""
    # Arrange
    verifier = FakeVerifier(alice=fake_claims("alice"))

    # Act / Assert
    with pytest.raises(TokenRejectedError) as excinfo:
        verifier.verify_header(header)
    assert excinfo.value.reason is TokenRejectedReason.SCHEME


def test_fake_verifier_verify_header_reads_the_bearer_token() -> None:
    """FakeVerifier.verify_header reads the token behind any-case Bearer."""
    # Arrange
    alice = fake_claims("alice")
    verifier = FakeVerifier(alice=alice)

    # Act / Assert
    assert verifier.verify_header("bearer   alice") is alice


def test_fake_verifier_drives_an_authenticated_app() -> None:
    """An app with a FakeVerifier serves known callers and refuses the rest."""
    # Arrange
    app = FastAPI()

    @app.get("/orders", dependencies=[Authenticated(scopes=["orders:read"])])
    async def orders(principal: CurrentPrincipal) -> dict[str, str | None]:
        return {"subject": principal.subject}

    verifier = FakeVerifier(
        alice=fake_claims("alice", "orders:read"), bob=fake_claims("bob")
    )
    micro = Grelmicro(uses=[ErrorResponses(), AuthenticatedRequests(verifier)])
    micro.install(app)

    # Act
    with TestClient(app) as client:
        anonymous = client.get("/orders")
        alice = client.get("/orders", headers={"Authorization": "Bearer alice"})
        bob = client.get("/orders", headers={"Authorization": "Bearer bob"})

    # Assert
    assert anonymous.status_code == HTTPStatus.UNAUTHORIZED
    assert alice.json() == {"subject": "alice"}
    assert bob.status_code == HTTPStatus.FORBIDDEN


def test_fake_claims_are_read_only_like_verified_claims() -> None:
    """fake_claims freezes the claims, as a verified token's are."""
    # Act
    claims = fake_claims("alice", roles=["admin"], profile={"team": "core"})

    # Assert
    assert claims.claims["roles"] == ("admin",)
    with pytest.raises(TypeError):
        claims.claims["sub"] = "bob"  # type: ignore[index]  # ty: ignore[invalid-assignment]
    with pytest.raises(TypeError):
        claims.claims["profile"]["team"] = "edge"


@pytest.mark.parametrize(
    "name", ["sub", "scope", "scp", "iss", "aud", "exp", "iat", "jti"]
)
def test_fake_claims_rejects_a_registered_claim_as_extra(name: str) -> None:
    """fake_claims refuses a registered claim passed as an extra."""
    # Arrange
    extras: dict[str, Any] = {name: "x"}

    # Act / Assert
    with pytest.raises(TypeError, match=name):
        fake_claims("alice", **extras)


@pytest.mark.parametrize("scope", ["orders:read orders:write", "", "a\tb"])
def test_fake_claims_rejects_a_scope_a_token_would_split(scope: str) -> None:
    """fake_claims refuses a scope that is empty or holds whitespace."""
    # Act / Assert
    with pytest.raises(ValueError, match="scope"):
        fake_claims("alice", scope)


def test_fake_claims_rejects_an_empty_subject() -> None:
    """fake_claims refuses an empty subject, which a verified token never has."""
    # Act / Assert
    with pytest.raises(ValueError, match="subject"):
        fake_claims("")


def test_fake_claims_rejects_scopes_as_a_keyword() -> None:
    """fake_claims refuses scopes passed as a keyword list."""
    # Act / Assert
    with pytest.raises(TypeError, match="scopes"):
        fake_claims("alice", scopes=["orders:read"])


def test_fake_verifier_rejects_a_value_that_is_not_claims() -> None:
    """FakeVerifier refuses a token whose value is not built by fake_claims."""
    # Act / Assert
    with pytest.raises(TypeError, match="tokens"):
        FakeVerifier(tokens={"tok-1": fake_claims("alice")})  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_fake_claims_rejects_a_scope_that_is_not_a_string() -> None:
    """fake_claims refuses a scope that is not a string."""
    # Act / Assert
    with pytest.raises(TypeError, match="scope"):
        fake_claims("alice", 5)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize("value", [({"k": 1},), {"a"}, b"raw", object()])
def test_fake_claims_rejects_a_claim_a_token_cannot_carry(
    value: object,
) -> None:
    """fake_claims refuses an extra claim that is not a JSON value."""
    # Act / Assert
    with pytest.raises(TypeError, match="JSON"):
        fake_claims("alice", extra=value)


def test_fake_claims_registered_claim_error_names_the_claim() -> None:
    """The refusal of a registered claim says it is not an extra."""
    # Act / Assert
    with pytest.raises(TypeError, match="exp is a registered claim"):
        fake_claims("alice", exp=1)


def test_fake_claims_registered_claims_error_names_every_claim() -> None:
    """The refusal of several registered claims names them all."""
    # Act / Assert
    with pytest.raises(TypeError, match="exp, iss are registered claims"):
        fake_claims("alice", iss="https://auth.example.com/", exp=1)


def test_fake_claims_sets_the_registered_claims_it_is_given() -> None:
    """fake_claims carries issuer, audience, expiry, issue time and token id."""
    # Act
    claims = fake_claims(
        "alice",
        issuer="https://auth.example.com/",
        audience=("orders-api", "billing-api"),
        expires_at=_EXPIRES_AT,
        issued_at=_ISSUED_AT,
        token_id="t-1",
    )

    # Assert
    assert claims.issuer == "https://auth.example.com/"
    assert claims.audience == ("orders-api", "billing-api")
    assert claims.expires_at == _EXPIRES_AT
    assert claims.issued_at == _ISSUED_AT
    assert claims.token_id == "t-1"
    assert claims.claims == {
        "sub": "alice",
        "iss": "https://auth.example.com/",
        "aud": ("orders-api", "billing-api"),
        "exp": _EXPIRES_AT,
        "iat": _ISSUED_AT,
        "jti": "t-1",
    }


def test_fake_claims_rejects_a_subject_that_is_not_a_string() -> None:
    """fake_claims refuses a subject that is not a string."""
    # Act / Assert
    with pytest.raises(TypeError, match="subject"):
        fake_claims(42)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_fake_claims_freezes_an_audience_list() -> None:
    """fake_claims stores a list audience as a tuple in both places."""
    # Act
    claims = fake_claims("alice", audience=["orders-api", "billing-api"])

    # Assert
    assert claims.audience == ("orders-api", "billing-api")
    assert claims.claims["aud"] == ("orders-api", "billing-api")


def test_fake_claims_rounds_a_fractional_expiry_down() -> None:
    """fake_claims rounds expires_at and issued_at down, as the verifier does."""
    # Act
    claims = fake_claims(
        "alice", expires_at=_EXPIRES_AT + 0.9, issued_at=_ISSUED_AT + 0.5
    )

    # Assert
    assert claims.expires_at == _EXPIRES_AT
    assert claims.issued_at == _ISSUED_AT
    assert claims.claims["exp"] == _EXPIRES_AT + 0.9


@pytest.mark.parametrize(
    "arguments",
    [
        {"expires_at": True},
        {"issued_at": "soon"},
        {"audience": ("a", 1)},
        {"issuer": 5},
        {"token_id": 5},
        {"not_before": "tomorrow"},
        {"not_before": True},
        {"expires_at": None},
    ],
)
def test_fake_claims_rejects_a_registered_claim_of_the_wrong_type(
    arguments: dict[str, Any],
) -> None:
    """fake_claims refuses a registered claim a token could not carry."""
    # Act / Assert
    with pytest.raises(TypeError):
        fake_claims("alice", **arguments)


@pytest.mark.parametrize(
    ("claim", "hint"),
    [
        ("exp", "expires_at="),
        ("iss", "issuer="),
        ("aud", "audience="),
        ("iat", "issued_at="),
        ("jti", "token_id="),
        ("nbf", "not_before="),
        ("sub", "first argument"),
        ("scope", "positional"),
    ],
)
def test_fake_claims_registered_claim_error_names_the_parameter(
    claim: str, hint: str
) -> None:
    """The refusal of a registered claim names the parameter that sets it."""
    # Arrange
    extras: dict[str, Any] = {claim: "x"}

    # Act / Assert
    with pytest.raises(TypeError, match=hint):
        fake_claims("alice", **extras)


def test_fake_claims_sets_not_before() -> None:
    """fake_claims carries not_before as the raw nbf claim."""
    # Act
    claims = fake_claims("alice", not_before=_ISSUED_AT)

    # Assert
    assert claims.claims["nbf"] == _ISSUED_AT


@pytest.mark.parametrize(
    "value", [-5, 2**64, 10**400, math.inf, -math.inf, math.nan]
)
@pytest.mark.parametrize("name", ["expires_at", "issued_at", "not_before"])
def test_fake_claims_rejects_a_time_out_of_range(
    name: str, value: float
) -> None:
    """fake_claims refuses a time that is negative, too large or not finite."""
    # Arrange
    arguments: dict[str, Any] = {name: value}

    # Act / Assert
    with pytest.raises(ValueError, match=name):
        fake_claims("alice", **arguments)


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_fake_claims_rejects_a_non_finite_extra_claim(value: float) -> None:
    """fake_claims refuses inf and nan as an extra claim, which JSON cannot hold."""
    # Act / Assert
    with pytest.raises(TypeError, match="JSON"):
        fake_claims("alice", extra=value)


def test_fake_verifier_refuses_an_expired_token() -> None:
    """FakeVerifier refuses a token past its expiry, as the real verifier does."""
    # Arrange
    verifier = FakeVerifier(
        old=fake_claims("alice", expires_at=time.time() - 60)
    )

    # Act / Assert
    with pytest.raises(TokenRejectedError) as excinfo:
        verifier.verify("old")
    assert excinfo.value.reason is TokenRejectedReason.EXPIRED


@pytest.mark.parametrize("claim", ["nbf", "issued_at"])
def test_fake_verifier_refuses_a_token_not_yet_valid(claim: str) -> None:
    """FakeVerifier refuses a token whose nbf or iat is still to come."""
    # Arrange
    later = int(time.time()) + 60
    claims = (
        fake_claims("alice", not_before=later)
        if claim == "nbf"
        else fake_claims("alice", issued_at=later)
    )
    verifier = FakeVerifier(early=claims)

    # Act / Assert
    with pytest.raises(TokenRejectedError) as excinfo:
        verifier.verify("early")
    assert excinfo.value.reason is TokenRejectedReason.NOT_YET_VALID


def test_fake_verifier_accepts_a_token_within_its_lifetime() -> None:
    """FakeVerifier accepts a token issued in the past that expires later."""
    # Arrange
    now = int(time.time())
    alice = fake_claims("alice", issued_at=now - 60, expires_at=now + 60)
    verifier = FakeVerifier(alice=alice)

    # Act / Assert
    assert verifier.verify("alice") is alice


_NOW = 1_950_000_000.5
"""A frozen current time, half a second into its second."""


@pytest.mark.parametrize(
    ("arguments", "outcome"),
    [
        ({"not_before": _NOW - 0.3}, "accepted"),
        ({"not_before": _NOW + 0.6}, "refused"),
        ({"expires_at": _NOW - 0.9}, "accepted"),
        ({"expires_at": _NOW - 1.2}, "refused"),
        ({"issued_at": _NOW - 0.1}, "accepted"),
        ({"issued_at": _NOW + 0.1}, "refused"),
    ],
)
def test_fake_verifier_times_tokens_like_the_real_verifier(
    monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any], outcome: str
) -> None:
    """FakeVerifier rounds exp and nbf to the second, and compares iat exactly."""
    # Arrange
    monkeypatch.setattr("grelmicro.testing.time", lambda: _NOW)
    verifier = FakeVerifier(token=fake_claims("alice", **arguments))

    # Act / Assert
    if outcome == "refused":
        with pytest.raises(TokenRejectedError):
            verifier.verify("token")
    else:
        assert verifier.verify("token").subject == "alice"
