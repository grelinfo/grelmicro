from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.health import HealthChecks
from grelmicro.integrations.fastapi import health_router

health = HealthChecks()
micro = Grelmicro(uses=[health])

app = FastAPI()
micro.install(app)
app.include_router(health_router(prefix="/api/v1"))
# Endpoints: GET /api/v1/livez, GET /api/v1/readyz, GET /api/v1/healthz
