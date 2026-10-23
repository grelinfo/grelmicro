from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.health import HealthChecks
from grelmicro.http import OpsServer
from grelmicro.metrics import Metrics

micro = Grelmicro(
    uses=[
        OpsServer(),
        HealthChecks(auto_health=True),
        Metrics(exporter="prometheus"),
    ]
)

app = FastAPI()
micro.install(app)
# Your routes on the app port (8000 with Uvicorn)
# GET /livez, /readyz, /healthz and /metrics on :8080
