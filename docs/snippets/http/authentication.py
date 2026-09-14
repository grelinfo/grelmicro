from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.http import AuthenticatedRequests, ErrorResponses
from grelmicro.integrations.fastapi import (
    Anonymous,
    Authenticated,
    CurrentPrincipal,
)
from grelmicro.security import JWTVerifier

micro = Grelmicro(
    uses=[
        ErrorResponses(),
        AuthenticatedRequests(
            JWTVerifier.discover(
                "https://auth.example.com/", audience="orders-api"
            ),
            exclude=("/livez", "/readyz"),
        ),
    ]
)
app = FastAPI()


@app.get("/orders")
async def list_orders(principal: CurrentPrincipal) -> dict[str, str | None]:
    return {"caller": principal.subject}


@app.delete(
    "/orders/{order_id}",
    dependencies=[Authenticated(scopes=["orders:write"])],
)
async def cancel(order_id: int) -> dict[str, int]:
    return {"cancelled": order_id}


@app.get("/catalog", dependencies=[Anonymous()])
async def catalog() -> list[str]:
    return ["tea", "coffee"]


micro.install(app)
