from contextlib import asynccontextmanager

from faststream import ContextRepo, FastStream
from faststream.redis import RedisBroker

from grelmicro.task import TaskManager

task = TaskManager()


@asynccontextmanager
async def lifespan(context: ContextRepo):
    async with task:
        yield


broker = RedisBroker()
app = FastStream(broker, lifespan=lifespan)
