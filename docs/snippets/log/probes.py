from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.health import HealthChecks
from grelmicro.integrations.fastapi import health_router
from grelmicro.log import configure, silence_probe_access_logs

health = HealthChecks()
micro = Grelmicro(uses=[health])

app = FastAPI()
micro.install(app)
app.include_router(health_router())

configure()
silence_probe_access_logs()
