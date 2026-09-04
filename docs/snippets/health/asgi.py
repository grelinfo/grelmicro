from starlette.applications import Starlette
from starlette.routing import Mount

from grelmicro import Grelmicro
from grelmicro.health import HealthChecks, HealthDetails, health_asgi

health = HealthChecks()
micro = Grelmicro(uses=[health])


@health.check("database")
async def check_database() -> HealthDetails | None:
    return None


app = Starlette(routes=[Mount("", app=health_asgi())])
micro.install(app)
# Endpoints: GET /livez, GET /readyz, GET /healthz
