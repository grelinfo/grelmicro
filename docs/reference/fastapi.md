# FastAPI

- **Start here**: [Request handlers and the ambient scope](../architecture/backends.md#request-handlers-and-the-ambient-scope)
- **Common recipes**: `app.add_middleware(GrelmicroMiddleware, micro=micro)` binds the active app inside request handlers, so patterns resolve their backends ambiently without explicit `backend=` wiring. `health_router()` adds the `/livez`, `/readyz`, and `/healthz` endpoints.

::: grelmicro.integrations.fastapi
    options:
      members:
        - GrelmicroMiddleware
        - health_router
        - CheckResultResponse
        - HealthzResponse
