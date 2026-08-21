# App

- **Start here**: [Wiring an App](../wiring.md)
- **The frameworks**: [Frameworks](../frameworks.md)

`Grelmicro` is the container. `uses=[...]` registers what the service runs on,
`async with micro:` opens it, and `micro.install(app)` wires the framework you
picked.

`GrelmicroMiddleware` is the same per-request binding as pure ASGI, for a
framework `install` does not know.

::: grelmicro
    options:
      members:
        - Grelmicro
        - GrelmicroMiddleware
        - Component
        - Usable
