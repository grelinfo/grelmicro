from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.metrics import Metrics, MetricsExporterType, metrics_router

micro = Grelmicro(uses=[Metrics(exporter=MetricsExporterType.PROMETHEUS)])

app = FastAPI()
micro.install(app)
app.include_router(metrics_router())
# Endpoint: GET /metrics
