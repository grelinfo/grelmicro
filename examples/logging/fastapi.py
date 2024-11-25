from contextlib import asynccontextmanager

from fastapi import FastAPI
from grelmicro.logging import configure_logging
from loguru import logger


@asynccontextmanager
def lifespan_startup():
    # Ensure logging is configured during startup
    configure_logging()
    yield


app = FastAPI()


@app.get("/hello")
def hello() -> dict[str, str]:
    logger.info("This is an info message")
