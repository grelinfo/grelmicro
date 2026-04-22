from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from grelmicro.health import HealthRegistry
from grelmicro.health.fastapi import health_router


class DatabaseChecker:
    @property
    def name(self) -> str:
        return "database"

    async def check(self) -> dict[str, Any] | None:
        return None


registry = HealthRegistry()
registry.add(DatabaseChecker())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(health_router())
# Endpoints: GET /livez, GET /readyz, GET /healthz
