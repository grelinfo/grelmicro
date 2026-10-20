# Third-party integration

A worked integration for `tinyweb`, a minimal ASGI framework, published as a
fake `grelmicro-tinyweb` package. See the [Plugins](https://grelmicro.grel.info/architecture/plugins/)
docs for the full contract.

- `tinyweb.py` is the framework. It routes by exact path and method, runs a
  lifespan and a middleware stack, and answers `404` to any other request.
  Each request's scope names the app serving it under `app`, as Starlette
  does, which is where the route gates read the app's policy.
- `grelmicro_tinyweb.py` is the integration. It implements `install`,
  `is_bound`, `install_middleware`, `route_declarations` and
  `install_route_gate`.

## What makes it discoverable

`pyproject.toml` declares the entry point. Nothing else is needed:

```toml
[project.entry-points."grelmicro.integrations"]
tinyweb = "grelmicro_tinyweb"
```

The key is the top-level module of the app's class. Once the package is
installed alongside grelmicro, `micro.install(app)` on a `TinyWeb` app imports
`grelmicro_tinyweb` and nothing else.

## Declare the routes

A `tinyweb` route says what it requires: `anonymous`, `scopes`, `cache` and
`own_checks`. One `_declare` function turns a route into a `RouteDeclaration`.
`install_route_gate` wraps each route with the gate built from it, and
`route_declarations` lists the same declarations, so a route added after
`install` stops the app from starting.

## Use it

```python
from grelmicro import Grelmicro
from grelmicro.http import AuthenticatedRequests
from tinyweb import Route, TinyWeb


async def catalog(scope):
    return "catalog"


async def orders(scope):
    return "orders"


app = TinyWeb(
    Route("/catalog", catalog, anonymous=True),
    Route("/orders", orders, scopes=frozenset({"orders:read"})),
)
micro = Grelmicro(uses=[AuthenticatedRequests(verifier)])
micro.install(app)
```

`verifier` is the app's `JWTVerifier`. `GET /catalog` is served without a
credential. `GET /orders` answers `401` without one and `403` without the
`orders:read` scope. An unknown URL answers `401` without a credential.

`tests/test_third_party_integration.py` drives this example through
`micro.install(app)` and an ASGI client.
