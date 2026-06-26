from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.trace import Trace

micro = Grelmicro(
    uses=[
        Trace(service_name="payments-api"),  # instrument=True is the default
        RedisProvider("redis://cache"),
        PostgresProvider("postgresql://db/payments"),
    ]
)

app = FastAPI()
micro.install(app)  # the FastAPI app is instrumented too
