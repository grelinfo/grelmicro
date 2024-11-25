from contextlib import asynccontextmanager

from fastapi import FastAPI
from grelmicro.task import TaskManager

task = TaskManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with task:
        yield


app = FastAPI(lifespan=lifespan)
