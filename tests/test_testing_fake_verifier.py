"""Tests for `FakeVerifier` and `fake_claims`."""

from http import HTTPStatus

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from grelmicro import Grelmicro
from grelmicro.http import AuthenticatedRequests, ErrorResponses
from grelmicro.integrations.fastapi import Authenticated, CurrentPrincipal
from grelmicro.security import TokenRejectedError, TokenRejectedReason
from grelmicro.testing import FakeVerifier, fake_claims


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
    }
    assert claims.issuer is None
    assert claims.expires_at is None


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
