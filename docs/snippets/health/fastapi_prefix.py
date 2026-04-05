from fastapi import FastAPI

from grelmicro.health.fastapi import health_router

app = FastAPI()
app.include_router(health_router(prefix="/api/v1"))
# Endpoints: GET /api/v1/health/live, GET /api/v1/health/ready
