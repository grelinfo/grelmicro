"""FastAPI authentication: the caller, its token and claims, and the route declarations.

`grelmicro.integrations.fastapi` loads this on first access to one of its
names.
"""

from collections.abc import Sequence
from typing import Annotated, Any

try:
    # FastAPI reads the dependencies' annotations at runtime, from this
    # module, so they are imported here.
    from fastapi import Depends as _Depends
    from fastapi import Security as _Security
    from fastapi.requests import (
        HTTPConnection as _HTTPConnection,
    )
    from fastapi.security import (
        SecurityScopes as _SecurityScopes,
    )

    HAS_FASTAPI = True
except ImportError:  # pragma: no cover - the reimport test walks this
    HAS_FASTAPI = False

from typing_extensions import Doc

from grelmicro._caller import is_authenticated
from grelmicro.http._authentication import declare_anonymous
from grelmicro.http._requirement import (
    Requirement,
    declare_from_request,
    declare_optional,
    requirement_for,
)
from grelmicro.security.jwt import JWTClaims
from grelmicro.security.principal import Principal, VerifiedToken

__all__ = [
    "Anonymous",
    "Authenticated",
    "Claims",
    "CurrentPrincipal",
    "CurrentToken",
    "OptionalPrincipal",
]


async def _current_principal(
    connection: _HTTPConnection, security_scopes: _SecurityScopes
) -> Any:  # noqa: ANN401
    """Return the caller, holding every scope a `Security` around it names."""
    return await _authenticated(connection, security_scopes)


async def _current_claims(
    connection: _HTTPConnection, security_scopes: _SecurityScopes
) -> Any:  # noqa: ANN401
    """Return the verified JWT claims of the caller.

    Raises:
        AuthenticationRequiredError: If the request carried no credential.
        InsufficientScopeError: If the caller lacks a scope a `Security`
            around it names.
        TypeError: If the caller was authenticated by something other than
            a JWT, which is a route asking for what its verifier never
            produces.
    """
    caller = await _authenticated(connection, security_scopes)
    if not isinstance(caller, JWTClaims):
        msg = (
            f"Claims reads a caller authenticated by a JWT, and this one is "
            f"a {type(caller).__name__}. Read CurrentPrincipal instead."
        )
        raise TypeError(msg)
    return caller


async def _optional_principal(connection: _HTTPConnection) -> Any:  # noqa: ANN401
    """Return the authenticated caller, or `None` for a request that sent no token."""
    caller = connection.scope.get("user")
    return caller if is_authenticated(caller) else None


async def _current_token(
    connection: _HTTPConnection, security_scopes: _SecurityScopes
) -> Any:  # noqa: ANN401
    """Return the bearer token the caller presented, once it verified.

    Raises:
        AuthenticationRequiredError: If the request carried no credential,
            or its caller was authenticated by something other than
            `AuthenticatedRequests`.
        InsufficientScopeError: If the caller lacks a scope a `Security`
            around it names.
    """
    return requirement_for(security_scopes.scopes).token(connection.scope)


async def _authenticated(
    connection: _HTTPConnection, security_scopes: _SecurityScopes
) -> Any:  # noqa: ANN401
    """Return the caller, holding every scope this declaration names."""
    return requirement_for(security_scopes.scopes).caller(connection.scope)


declare_from_request(_authenticated)
declare_from_request(_current_principal)
declare_from_request(_current_claims)
declare_from_request(_current_token)
declare_optional(_optional_principal)


CurrentPrincipal = Annotated[
    Principal, _Depends(_current_principal) if HAS_FASTAPI else None
]
"""The authenticated caller, whatever proved who it is.

```python
from grelmicro.integrations.fastapi import CurrentPrincipal


@app.get("/orders")
async def list_orders(principal: CurrentPrincipal) -> list[Order]:
    return await orders_of(principal.issuer, principal.subject)
```

A request that reached the route without a credential is answered `401`.
Read inside `Security(..., scopes=[...])`, it requires those scopes too,
and a caller lacking one is answered `403`. It needs a registered
`AuthenticatedRequests`, which verifies the token before the route runs.
"""

CurrentToken = Annotated[
    VerifiedToken, _Depends(_current_token) if HAS_FASTAPI else None
]
"""The bearer token the caller presented, once `AuthenticatedRequests` verified it.

```python
from grelmicro.integrations.fastapi import CurrentToken


@app.post("/orders")
async def create_order(token: CurrentToken) -> dict[str, str]:
    response = await payments.post(
        "/charges", auth=payments_for_user.auth(token)
    )
    return response.json()
```

Read it to act for the caller, such as exchanging it with a `TokenExchange`.
`token.value` is the token and `token.expires_at` when it expires, and its
`repr` never shows the token. A request that reached the route without a
credential is answered `401`, as `CurrentPrincipal` answers it.
"""

Claims = Annotated[
    JWTClaims, _Depends(_current_claims) if HAS_FASTAPI else None
]
"""The verified JWT claims of the caller, typed.

```python
from grelmicro.integrations.fastapi import Claims


@app.get("/me")
async def me(claims: Claims) -> dict[str, str]:
    return {"tenant": claims.claims["tenant"]}
```

Read `CurrentPrincipal` instead in a handler that should not care how the
caller was authenticated.
"""

OptionalPrincipal = Annotated[
    Principal | None, _Depends(_optional_principal) if HAS_FASTAPI else None
]
"""The caller when the request sent a valid token, and `None` when it sent none.

```python
from grelmicro.integrations.fastapi import Anonymous, OptionalPrincipal


@app.get("/catalog", dependencies=[Anonymous()])
async def catalog(principal: OptionalPrincipal) -> list[Product]:
    if principal is None:
        return await public_catalog()
    return await catalog_for(principal.subject)
```

Read on a route declaring `Anonymous()`, where a credential is optional. A
token that does not verify never reaches it: the request is answered `401`.
"""


def Authenticated(  # noqa: N802
    *,
    scopes: Annotated[
        Sequence[str],
        Doc(
            "Scopes the caller must hold, every one of them. Declared on "
            "a router and on its route, each applies its own."
        ),
    ] = (),
) -> Any:  # noqa: ANN401
    """Require an authenticated caller holding every scope named.

    ```python
    from grelmicro.integrations.fastapi import Authenticated


    @app.delete(
        "/orders/{order_id}",
        dependencies=[Authenticated(scopes=["orders:write"])],
    )
    async def cancel(order_id: int) -> None: ...
    ```

    Built on `fastapi.Security`, so the scopes reach `SecurityScopes`, and
    a handler can take the caller from it:

    ```python
    @app.get("/orders")
    async def list_orders(
        principal: Annotated[Principal, Authenticated(scopes=["orders:read"])],
    ) -> list[Order]: ...
    ```

    A caller with no credential is answered `401`, and one lacking a scope
    `403`, whose `WWW-Authenticate` challenge names the scopes.

    It needs a registered `AuthenticatedRequests`, which verifies the token
    before the route runs. Without one, every call is answered `401`.

    Read more in the [Authentication](../http/authentication.md) docs.

    Raises:
        TypeError: If `scopes` is a single string.
        ValueError: If a scope is not an OAuth scope token.
    """
    if not HAS_FASTAPI:  # pragma: no cover - the reimport test walks this
        from grelmicro.errors import (  # noqa: PLC0415
            DependencyNotFoundError,
        )

        raise DependencyNotFoundError(module="fastapi")
    return _Security(_authenticated, scopes=list(Requirement(scopes).scopes))


def Anonymous() -> Any:  # noqa: N802, ANN401
    """Serve this route without a credential.

    Every route is authenticated once `AuthenticatedRequests` is registered.
    Declare this on the one that is public:

    ```python
    from grelmicro.integrations.fastapi import Anonymous


    @app.get("/catalog", dependencies=[Anonymous()])
    async def catalog() -> list[Product]: ...
    ```

    `micro.install(app)` reads it off the dependency tree, and off what the
    router holding the route was included with. It applies per method, so a
    path serving a public read keeps its writes authenticated. It is read off
    the route FastAPI dispatches the request to, so a declaration never opens
    a route it was not written on. A credential is optional there. A request
    sending none is served with a caller that is not authenticated, and a
    bearer token that is sent is verified, so a valid one hands the route its
    caller through `OptionalPrincipal` and one that does not verify is
    answered `401`.

    Read more in the [Authentication](../http/authentication.md) docs.
    """
    if not HAS_FASTAPI:  # pragma: no cover - the reimport test walks this
        from grelmicro.errors import (  # noqa: PLC0415
            DependencyNotFoundError,
        )

        raise DependencyNotFoundError(module="fastapi")
    return _Depends(declare_anonymous())
