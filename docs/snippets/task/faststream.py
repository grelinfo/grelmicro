from contextlib import asynccontextmanager

from faststream import ContextRepo, FastStream
from faststream.redis import RedisBroker

import grelmicro
import grelmicro.task

manager = grelmicro.task.TaskManager()
grelmicro.task.use_manager(manager)


@asynccontextmanager
async def lifespan(context: ContextRepo):
    async with grelmicro.lifespan():
        yield


broker = RedisBroker()
app = FastStream(broker, lifespan=lifespan)
