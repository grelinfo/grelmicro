import httpx
from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.http import AuthenticatedRequests
from grelmicro.integrations.fastapi import CurrentToken
from grelmicro.security import (
    ClientAuth,
    JWTVerifier,
    OAuthClient,
    TokenExchange,
)

micro = Grelmicro(
    uses=[
        # Verifies the token each request presents.
        AuthenticatedRequests(
            JWTVerifier.discover(
                "https://auth.example.com/", audience="orders-api"
            )
        ),
        # Exchanges it for tokens issued to the APIs the service calls.
        OAuthClient.discover(
            "https://auth.example.com/",
            client_id="orders-api",
            client_auth=ClientAuth.secret(),
        ),
    ]
)

payments_for_user = TokenExchange("payments-api", audience="payments-api")
payments = httpx.AsyncClient(base_url="https://payments.internal")

app = FastAPI()


@app.post("/orders")
async def create_order(token: CurrentToken) -> dict[str, str]:
    # The token issued for payments-api still names the caller.
    response = await payments.post(
        "/charges",
        json={"amount": 100},
        auth=payments_for_user.auth(token),
    )
    return response.json()


micro.install(app)
