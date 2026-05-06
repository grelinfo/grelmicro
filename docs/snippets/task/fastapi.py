from contextlib import asynccontextmanager

from fastapi import FastAPI

import grelmicro
import grelmicro.task

manager = grelmicro.task.TaskManager()
grelmicro.task.use_manager(manager)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with grelmicro.lifespan():
        yield


app = FastAPI(lifespan=lifespan)
