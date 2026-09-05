from fastapi import FastAPI

from grelmicro import Grelmicro
from grelmicro.log import AccessLog, Log
from grelmicro.security import ClientAddressMiddleware, TrustedProxies

micro = Grelmicro(uses=[Log(), AccessLog()])

app = FastAPI()
app.add_middleware(
    ClientAddressMiddleware, trusted=TrustedProxies(["10.0.0.0/8"])
)
micro.install(app)
