from contextlib import asynccontextmanager

from fastapi import FastAPI

from grelmicro.task import Tasks

tasks = Tasks()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with tasks:
        yield


app = FastAPI(lifespan=lifespan)
