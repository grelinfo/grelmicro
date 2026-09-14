from http import HTTPStatus

from fastapi import FastAPI
from fastapi.testclient import TestClient

from grelmicro import Grelmicro
from grelmicro.http import AuthenticatedRequests, ErrorResponses
from grelmicro.integrations.fastapi import CurrentPrincipal
from grelmicro.security import (
    JWTClaims,
    TokenRejectedError,
    TokenRejectedReason,
)


class Callers:
    """A verifier for tests: each token is the name of the caller it stands for."""

    def __init__(self, **callers: JWTClaims) -> None:
        self._callers = callers

    def verify(self, token: str) -> JWTClaims:
        try:
            return self._callers[token]
        except KeyError:
            raise TokenRejectedError(TokenRejectedReason.INVALID) from None

    def verify_header(self, header: str | None) -> JWTClaims:
        return self.verify((header or "").removeprefix("Bearer "))


def caller(subject: str, *scopes: str) -> JWTClaims:
    return JWTClaims(
        claims={"sub": subject},
        subject=subject,
        issuer="https://auth.example.com/",
        audience="orders-api",
        expires_at=None,
        issued_at=None,
        token_id=None,
        scopes=frozenset(scopes),
    )


app = FastAPI()


@app.get("/orders")
async def orders(principal: CurrentPrincipal) -> dict[str, str | None]:
    return {"subject": principal.subject}


verifier = Callers(alice=caller("alice", "orders:read"))
micro = Grelmicro(uses=[ErrorResponses(), AuthenticatedRequests(verifier)])
micro.install(app)


def test_orders_needs_a_token() -> None:
    client = TestClient(app)

    assert client.get("/orders").status_code == HTTPStatus.UNAUTHORIZED
    assert client.get(
        "/orders", headers={"Authorization": "Bearer alice"}
    ).json() == {"subject": "alice"}
