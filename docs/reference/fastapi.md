# FastAPI

- **Start here**: [Request handlers and the ambient scope](../architecture/backends.md#request-handlers-and-the-ambient-scope)
- **Common recipes**: `app.add_middleware(GrelmicroMiddleware, micro=micro)` binds the active app inside request handlers, so patterns resolve their backends ambiently without explicit `backend=` wiring. `health_router()` adds the `/livez`, `/readyz`, and `/healthz` endpoints. `app.add_middleware(IdempotencyMiddleware, idempotency=Idempotency("http"))` replays a stored response when a request repeats its `Idempotency-Key`.

::: grelmicro.integrations.fastapi
    options:
      members:
        - GrelmicroMiddleware
        - IdempotencyMiddleware
        - StoredResponse
        - document_idempotency
        - health_router
        - CheckResultResponse
        - HealthzResponse
