from fastapi import FastAPI

from grelmicro.integrations.fastapi import health_router

app = FastAPI()
app.include_router(health_router(prefix="/api/v1"))
# Endpoints: GET /api/v1/livez, GET /api/v1/readyz, GET /api/v1/healthz
