# FastStream

- **Start here**: [Request handlers and the ambient scope](../architecture/backends.md#request-handlers-and-the-ambient-scope)
- **Common recipes**: `micro.install(app)` opens the app on startup and binds the active app inside subscriber handlers, so patterns resolve their backends ambiently without explicit `backend=` wiring.

::: grelmicro.integrations.faststream
    options:
      members:
        - install
