from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from grelmicro.health import HealthRegistry
from grelmicro.health.fastapi import health_router


# Define a health checker
class DatabaseChecker:
    @property
    def name(self) -> str:
        return "database"

    async def check(self) -> dict[str, Any] | None:
        return None


# Setup
registry = HealthRegistry()
registry.add(DatabaseChecker())


@asynccontextmanager
async def lifespan(app):
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(health_router())
