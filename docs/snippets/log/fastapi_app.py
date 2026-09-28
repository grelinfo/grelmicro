from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

from grelmicro.log import LogBackendType, configure


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Ensure logging is configured during startup
    configure(backend=LogBackendType.LOGURU)
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root() -> dict[str, str]:
    logger.info("This is an info message")
    return {"Hello": "World"}
