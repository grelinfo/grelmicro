"""Integrations: optional adapters that wire a `Grelmicro` app into a framework.

Each submodule wires a `Grelmicro` app into one external framework (Starlette,
FastAPI, FastStream), opening its lifecycle and binding the active app per
request or message. Import the submodule you need, like
`from grelmicro.integrations.fastapi import GrelmicroMiddleware`. Prefer the
polymorphic `micro.install(app)`, which detects the framework for you.
"""

__all__: list[str] = []
