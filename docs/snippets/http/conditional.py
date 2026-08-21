from fastapi import FastAPI
from pydantic import BaseModel

from grelmicro import Grelmicro
from grelmicro.http import (
    ConditionalRequests,
    ErrorResponses,
    check_freshness,
    check_precondition,
)

micro = Grelmicro(uses=[ErrorResponses(), ConditionalRequests()])
app = FastAPI()

micro.install(app)


class Cart(BaseModel):
    id: int
    items: list[str]
    version: int


class CartIn(BaseModel):
    items: list[str]


@app.get("/carts/{cart_id}")
async def read(cart_id: int) -> Cart:
    cart = await load(cart_id)
    # The response carries an ETag, so the client can send it back.
    check_freshness(cart.version)
    return cart


@app.patch("/carts/{cart_id}")
async def update(cart_id: int, body: CartIn) -> Cart:
    cart = await load(cart_id)
    # 412 when another writer landed, 428 when the client sent no If-Match.
    check_precondition(cart.version)
    return await save(cart_id, body.items, expected=cart.version)


async def load(cart_id: int) -> Cart:
    return Cart(id=cart_id, items=["apple"], version=3)


async def save(cart_id: int, items: list[str], *, expected: int) -> Cart:
    return Cart(id=cart_id, items=items, version=expected + 1)
