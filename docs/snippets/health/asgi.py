from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

from grelmicro import Grelmicro
from grelmicro.health import HealthChecks, HealthDetails, health_asgi

health = HealthChecks()
micro = Grelmicro(uses=[health])


@health.check("database")
async def check_database() -> HealthDetails | None:
    return None


async def home(request: object) -> PlainTextResponse:
    return PlainTextResponse("hello")


app = Starlette(
    routes=[
        Route("/", home),
        # Last: mounted at "" it matches every path, so a route after it
        # would never be reached.
        Mount("", app=health_asgi()),
    ]
)
micro.install(app)
# Endpoints: GET /livez, GET /readyz, GET /healthz
