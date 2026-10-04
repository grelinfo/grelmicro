from http import HTTPStatus

from fastapi import FastAPI
from fastapi.testclient import TestClient

from grelmicro import Grelmicro
from grelmicro.http import AuthenticatedRequests, ErrorResponses
from grelmicro.integrations.fastapi import CurrentPrincipal
from grelmicro.testing import FakeVerifier, fake_claims

app = FastAPI()


@app.get("/orders")
async def orders(principal: CurrentPrincipal) -> dict[str, str | None]:
    return {"subject": principal.subject}


verifier = FakeVerifier(alice=fake_claims("alice", "orders:read"))
micro = Grelmicro(uses=[ErrorResponses(), AuthenticatedRequests(verifier)])
micro.install(app)


def test_orders_needs_a_token() -> None:
    with TestClient(app) as client:
        assert client.get("/orders").status_code == HTTPStatus.UNAUTHORIZED
        assert client.get(
            "/orders", headers={"Authorization": "Bearer alice"}
        ).json() == {"subject": "alice"}
