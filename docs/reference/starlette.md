# Starlette

- **Start here**: [Frameworks](../frameworks.md)
- **The errors**: [Error Responses](../http/errors.md)

Everything here is pure ASGI, so it works on a plain Starlette app and on
anything built from one. A FastAPI app reaches the same names through
[FastAPI](fastapi.md), which adds the OpenAPI schema and the health router.
`GrelmicroMiddleware` lives in [App](app.md) and `IdempotencyMiddleware` in
[HTTP](http.md), since neither is tied to a framework.

Prefer the polymorphic `micro.install(app)`, which detects the framework and
calls `install` for you.

::: grelmicro.integrations.starlette
    options:
      members:
        - install
        - install_error_responses
        - install_middleware
        - is_bound
        - error_response
