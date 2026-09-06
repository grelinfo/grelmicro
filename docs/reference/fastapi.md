# FastAPI

- **Start here**: [Request handlers and the ambient scope](../architecture/backends.md#request-handlers-and-the-ambient-scope)
- **Common recipes**: `micro.install(app)` wires the lifespan and binds the active app inside request handlers, so patterns resolve their backends ambiently without explicit `backend=` wiring. `health_router()` adds the `/livez`, `/readyz`, and `/healthz` endpoints. `uses=[IdempotentRequests()]` replays a stored response when a request repeats its `Idempotency-Key`, `uses=[ConditionalRequests()]` documents `If-Match` and `If-None-Match` in the schema so Swagger offers them, and `dependencies=[CachedResponse(ttl=60)]` answers a repeated read of one route from the cache.

Everything pure ASGI lives in [Starlette](starlette.md), [App](app.md), and
[HTTP](http.md). This module adds what only FastAPI has: the OpenAPI schema
and the health router.

::: grelmicro.integrations.fastapi
    options:
      members:
        - install
        - install_error_responses
        - install_middleware
        - is_bound
        - error_response
        - CachedResponse
        - Conditional
        - ConditionalRequest
        - document_conditional_requests
        - document_idempotency
        - health_router
        - CheckResultResponse
        - HealthzResponse
