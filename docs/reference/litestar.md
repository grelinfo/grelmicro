# Litestar

- **Start here**: [Frameworks](../frameworks.md#litestar)
- **Common recipes**: `micro.install(app)` opens the app on startup and binds the active app inside route handlers, so patterns resolve their backends ambiently without explicit `backend=` wiring. Call it after `Litestar(...)`, which builds the middleware stack at construction.

::: grelmicro.integrations.litestar
    options:
      members:
        - install
        - install_error_responses
        - is_bound
        - GrelmicroMiddleware
        - error_response
