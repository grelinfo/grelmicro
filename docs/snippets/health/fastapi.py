from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.health import HealthChecks, HealthDetails
from grelmicro.integrations.fastapi import health_router

health = HealthChecks()


@health.check("database")
async def check_database() -> HealthDetails | None:
    return None


micro = Grelmicro(uses=[health])

app = FastAPI()
micro.install(app)
app.include_router(health_router())
# Endpoints: GET /livez, GET /readyz, GET /healthz
