from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.health import HealthChecks
from grelmicro.integrations.fastapi import health_router

health = HealthChecks()
micro = Grelmicro(uses=[health])

app = FastAPI()
micro.install(app)
app.include_router(health_router(include_in_schema=True))
# GET /livez, GET /readyz, GET /healthz, and all three in /openapi.json
