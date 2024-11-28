from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

from grelmicro.logging import configure_logging


@asynccontextmanager
def lifespan_startup():
    # Ensure logging is configured during startup
    configure_logging()
    yield


app = FastAPI()


@app.get("/")
def root():
    logger.info("This is an info message")
    return {"Hello": "World"}
