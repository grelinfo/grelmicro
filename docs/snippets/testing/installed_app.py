from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from grelmicro import Grelmicro
from grelmicro.coordination import Lock
from grelmicro.providers.postgres import PostgresProvider

# app.py
micro = Grelmicro(uses=[PostgresProvider("postgresql://app:secret@db/app")])
app = FastAPI()
micro.install(app)


@app.post("/checkout")
async def checkout() -> dict[str, str]:
    async with Lock("cart"):
        return {"status": "done"}


# conftest.py
@pytest.fixture
def client() -> Iterator[TestClient]:
    with micro.fake(), TestClient(app) as client:
        yield client


# test_checkout.py
def test_checkout(client: TestClient) -> None:
    assert client.post("/checkout").json() == {"status": "done"}
