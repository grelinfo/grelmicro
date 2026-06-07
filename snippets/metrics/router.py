from contextlib import asynccontextmanager

from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.metrics import Metrics, MetricsExporterType, metrics_router

micro = Grelmicro(uses=[Metrics(exporter=MetricsExporterType.PROMETHEUS)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with micro:
        yield


app = FastAPI(lifespan=lifespan)
app.include_router(metrics_router())
