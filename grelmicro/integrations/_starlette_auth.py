"""The authentication declarations of the Starlette integration.

`grelmicro.integrations.starlette` loads this module when `Authenticated`
or `current_token` is first read.
"""

from __future__ import annotations

import functools
import inspect
from typing import TYPE_CHECKING, Annotated, Any

from typing_extensions import Doc

from grelmicro._wrapping import refuse_registered
from grelmicro.http._requirement import (
    AUTHENTICATED,
    Requirement,
    declared_scopes,
)
from grelmicro.security.principal import VerifiedToken

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from starlette.requests import HTTPConnection

__all__ = ["Authenticated", "current_token"]


def Authenticated(  # noqa: N802
    *,
    scopes: Annotated[
        Sequence[str],
        Doc("Scopes the caller must hold, every one of them."),
    ] = (),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Require an authenticated caller holding every scope named.

    A decorator for a Starlette endpoint: a function, sync or async, a
    method of an `HTTPEndpoint`, or a websocket endpoint.

    ```python
    from grelmicro.integrations.starlette import Authenticated


    @Authenticated(scopes=["orders:write"])
    async def cancel(request: Request) -> JSONResponse: ...
    ```

    A caller with no credential is answered `401`, and one lacking a scope
    `403`, whose `WWW-Authenticate` challenge names the scopes. It needs a
    registered `AuthenticatedRequests`, which verifies the token before the
    endpoint runs, and whose gate on the route refuses the caller first.

    Read more in the [Authentication](../http/authentication.md) docs.

    Raises:
        TypeError: If `scopes` is a single string, or the endpoint takes no
            `request` or `websocket` argument.
        ValueError: If a scope is not an OAuth scope token.
    """
    own = Requirement(scopes).scopes

    def decorate(endpoint: Callable[..., Any]) -> Callable[..., Any]:
        refuse_registered(endpoint, "@Authenticated")
        name, position = _connection_argument(endpoint)
        # Stacked on another `@Authenticated`, it requires the scopes of both.
        inner = declared_scopes(endpoint) or ()
        requirement = Requirement(tuple(dict.fromkeys((*own, *inner))))

        def check(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
            connection = kwargs[name] if name in kwargs else args[position]
            requirement.caller(connection.scope)

        if _is_async(endpoint):

            @functools.wraps(endpoint)
            async def asynchronous(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                check(args, kwargs)
                return await endpoint(*args, **kwargs)

            wrapper: Callable[..., Any] = asynchronous
        else:

            @functools.wraps(endpoint)
            def synchronous(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
                check(args, kwargs)
                return endpoint(*args, **kwargs)

            wrapper = synchronous
        return requirement.declare(wrapper)

    return decorate


def current_token(
    connection: Annotated[
        HTTPConnection,
        Doc("The request or websocket the endpoint was handed."),
    ],
) -> VerifiedToken:
    """Return the bearer token the request presented, once it verified.

    ```python
    from grelmicro.integrations.starlette import current_token


    async def create_order(request: Request) -> JSONResponse:
        token = current_token(request)
        response = await payments.post(
            "/charges", auth=payments_for_user.auth(token)
        )
        return JSONResponse(response.json())
    ```

    `AuthenticatedRequests` leaves it on every request it authenticates.
    Read it to act for the caller, such as exchanging it with a
    `TokenExchange`. Its `repr` never shows the token.

    Read more in the [Authentication](../http/authentication.md) docs.

    Raises:
        AuthenticationRequiredError: If the request carried no token that
            `AuthenticatedRequests` verified, such as one on an excluded
            path.
    """
    return AUTHENTICATED.token(connection.scope)


def _connection_argument(endpoint: Callable[..., Any]) -> tuple[str, int]:
    """Return the name and position of the argument the connection arrives in.

    Raises:
        TypeError: If the endpoint takes neither a `request` nor a
            `websocket` argument.
    """
    parameters = inspect.signature(endpoint).parameters.values()
    for position, parameter in enumerate(parameters):
        if parameter.name in {"request", "websocket"}:
            return parameter.name, position
    msg = (
        f"Authenticated() decorates an endpoint taking a request or a "
        f"websocket argument, and {getattr(endpoint, '__qualname__', endpoint)!s} takes neither."
    )
    raise TypeError(msg)


def _is_async(endpoint: Callable[..., Any]) -> bool:
    """Return whether calling the endpoint returns a coroutine.

    A function declared `async`, or an object whose `__call__` is, which
    Starlette awaits the same way.
    """
    call = getattr(endpoint, "__call__", None)  # noqa: B004
    return inspect.iscoroutinefunction(endpoint) or inspect.iscoroutinefunction(
        call
    )


Authenticated.__module__ = "grelmicro.integrations.starlette"
current_token.__module__ = "grelmicro.integrations.starlette"
