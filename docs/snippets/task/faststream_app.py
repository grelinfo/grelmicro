from contextlib import asynccontextmanager

from faststream import ContextRepo, FastStream
from faststream.redis import RedisBroker

from grelmicro.task import Tasks

tasks = Tasks()


@asynccontextmanager
async def lifespan(context: ContextRepo):
    async with tasks:
        yield


broker = RedisBroker()
app = FastStream(broker, lifespan=lifespan)
