from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

from grelmicro.log import configure


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Ensure logging is configured during startup
    configure(backend="loguru")
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root() -> dict[str, str]:
    logger.info("This is an info message")
    return {"Hello": "World"}
