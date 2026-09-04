from fastapi import FastAPI

from grelmicro.integrations.fastapi import health_router

app = FastAPI()
app.include_router(health_router(include_in_schema=True))
# GET /livez, GET /readyz, GET /healthz, and all three in /openapi.json
