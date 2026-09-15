import httpx
from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.security import ClientAuth, ClientCredentials, OAuthClient

# The service is registered with its authorization server once. The secret is
# read from GREL_OAUTHCLIENT_CLIENT_SECRET.
micro = Grelmicro(
    uses=[
        OAuthClient.discover(
            "https://auth.example.com/",
            client_id="orders-api",
            client_auth=ClientAuth.secret(),
        ),
    ]
)

# One token for the payments API, fetched on first use and kept fresh.
payments_token = ClientCredentials("payments-api", audience="payments-api")
payments = httpx.AsyncClient(
    base_url="https://payments.internal", auth=payments_token.auth()
)

app = FastAPI()


@app.post("/orders")
async def create_order() -> dict[str, str]:
    response = await payments.post("/charges", json={"amount": 100})
    return response.json()


micro.install(app)
