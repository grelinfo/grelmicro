from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.integrations.fastapi import metrics_router
from grelmicro.metrics import Metrics, MetricsExporterType

micro = Grelmicro(uses=[Metrics(exporter=MetricsExporterType.PROMETHEUS)])

app = FastAPI()
micro.install(app)
app.include_router(metrics_router())
# Endpoint: GET /metrics
