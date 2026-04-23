from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from grelmicro.health import HealthDetails, HealthRegistry
from grelmicro.health.fastapi import health_router

health = HealthRegistry()


@health.check("database")
async def check_database() -> HealthDetails | None:
    return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(health_router())
# Endpoints: GET /livez, GET /readyz, GET /healthz
