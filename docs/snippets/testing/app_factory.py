from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from grelmicro import Grelmicro
from grelmicro.providers.redis import RedisProvider


# app.py
def create_app() -> tuple[FastAPI, Grelmicro]:
    micro = Grelmicro(uses=[RedisProvider("redis://cache:6379/0")])
    app = FastAPI()
    micro.install(app)
    return app, micro


# conftest.py
@pytest.fixture
def client() -> Iterator[TestClient]:
    app, micro = create_app()
    with micro.fake(), TestClient(app) as client:
        yield client


# test_two_clients.py
def test_two_clients_at_once(client: TestClient) -> None:
    other, micro = create_app()
    with micro.fake(), TestClient(other) as second:
        assert (
            client.get("/docs").status_code == second.get("/docs").status_code
        )
