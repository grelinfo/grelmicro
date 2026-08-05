import logging

from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.health import HealthChecks
from grelmicro.integrations.fastapi import health_router
from grelmicro.log import ProbeFilter, configure

health = HealthChecks()
micro = Grelmicro(uses=[health])

app = FastAPI()
micro.install(app)
app.include_router(health_router())

configure()
logging.getLogger("uvicorn.access").addFilter(ProbeFilter())
