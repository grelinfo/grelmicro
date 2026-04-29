from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

from grelmicro.log import configure


@asynccontextmanager
def lifespan_startup():
    # Ensure logging is configured during startup
    configure()
    yield


app = FastAPI()


@app.get("/")
def root():
    logger.info("This is an info message")
    return {"Hello": "World"}
