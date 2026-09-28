"""What each route requires of a request, as the framework integration declares it."""

from __future__ import annotations

import math
from dataclasses import KW_ONLY, dataclass
from typing import Annotated, Final

from typing_extensions import Doc

from grelmicro.errors import _scope_tokens

__all__ = ["RouteDeclaration", "refuse_impossible", "route_name"]

_READS: Final = frozenset({"GET", "HEAD"})
"""The methods whose responses may be cached."""


@dataclass(frozen=True, slots=True)
class RouteDeclaration:
    """What one route requires of a request, as the integration declares it.

    An integration lists one per route, or one per method set where the
    methods of a route declare differently, such as a public `GET` beside a
    protected `POST`. A declaration with nothing but a path is an
    authenticated route.

    ```python
    from grelmicro.http import RouteDeclaration

    RouteDeclaration(
        "/orders/{order_id}",
        methods=frozenset({"DELETE"}),
        scopes=frozenset({"orders:write"}),
    )
    ```

    Read more in the [Plugins](../architecture/plugins.md#declare-the-routes)
    docs.
    """

    path: Annotated[
        str,
        Doc(
            "The path template, as the router matches it, such as "
            "`/orders/{order_id}`."
        ),
    ]
    _: KW_ONLY
    methods: Annotated[
        frozenset[str] | None,
        Doc(
            "The methods the router answers on this route, in capitals. "
            "`None` is every request the route takes, as a mount or a "
            "websocket route takes them."
        ),
    ] = None
    anonymous: Annotated[
        bool,
        Doc("The route serves a caller with no credential."),
    ] = False
    scopes: Annotated[
        frozenset[str],
        Doc("The scopes the caller must hold, every one of them."),
    ] = frozenset()
    own_checks: Annotated[
        bool,
        Doc(
            "The route runs checks of its own before the handler, so its "
            "answer can depend on the caller. Its response is never cached "
            "or replayed across callers."
        ),
    ] = False
    cache: Annotated[
        bool | float,
        Doc(
            "`CachedResponses` may store the route's response. `True` keeps "
            "it for the TTL the component is configured with, and a number "
            "for that many seconds."
        ),
    ] = False

    def __post_init__(self) -> None:
        """Hold `methods` and `scopes` as frozen sets, whatever set was passed.

        Raises:
            TypeError: If `methods` or `scopes` is a single string, which
                would read as one entry per character.
        """
        for name in ("methods", "scopes"):
            value = getattr(self, name)
            if value is None or isinstance(value, frozenset):
                continue
            if isinstance(value, str):
                msg = (
                    f"RouteDeclaration({self.path!r}) takes a set of {name}, "
                    f"not a single string. Write {name}=frozenset({{{value!r}}})."
                )
                raise TypeError(msg)
            object.__setattr__(self, name, frozenset(value))


def refuse_impossible(declaration: RouteDeclaration) -> None:
    """Refuse a declaration that cannot hold, naming its route.

    Raises:
        TypeError: If `cache` is neither a boolean nor a number.
        ValueError: If `methods` is empty or holds a method in lower case,
            the route is anonymous and requires scopes, it caches and runs
            checks of its own, it caches a method other than `GET` or
            `HEAD`, its cache TTL is not a positive number of seconds, or
            a scope is not an OAuth scope token.
    """
    path = declaration.path
    methods = declaration.methods
    if methods is not None:
        if not methods:
            msg = (
                f"{path} declares methods=frozenset(), which no request "
                f"matches. Pass None for every request the route takes, or "
                f"the methods its router answers."
            )
            raise ValueError(msg)
        for method in methods:
            if not isinstance(method, str) or method != method.upper():
                msg = (
                    f"{path} declares the method {method!r}, and a request "
                    f"carries its method as a name in capitals, such as 'GET'."
                )
                raise ValueError(msg)
    route = route_name(declaration)
    try:
        _scope_tokens(tuple(declaration.scopes))
    except ValueError as error:
        msg = f"{route} declares a scope that is not an OAuth scope token: {error}"
        raise ValueError(msg) from None
    if declaration.anonymous and declaration.scopes:
        msg = (
            f"{route} declares anonymous=True and scopes "
            f"{sorted(declaration.scopes)}. A route served without a "
            f"credential cannot require a scope. Keep one of them."
        )
        raise ValueError(msg)
    _refuse_impossible_cache(declaration, route)


def _refuse_impossible_cache(declaration: RouteDeclaration, route: str) -> None:
    """Refuse a `cache` the route cannot honor.

    Raises:
        TypeError: If `cache` is neither a boolean nor a number.
        ValueError: If the route caches and runs checks of its own, caches
            a method other than `GET` or `HEAD`, or its TTL is not a
            positive number of seconds.
    """
    cache = declaration.cache
    if cache is False:
        return
    if cache is not True:
        if isinstance(cache, bool) or not isinstance(cache, int | float):
            msg = (
                f"{route} declares cache={cache!r}. Pass True for the TTL the "
                f"component is configured with, or a number of seconds."
            )
            raise TypeError(msg)
        if not math.isfinite(cache) or cache <= 0:
            msg = (
                f"{route} declares cache={cache!r}, which keeps nothing. Pass "
                f"a number of seconds above zero, or True for the TTL the "
                f"component is configured with."
            )
            raise ValueError(msg)
    if declaration.own_checks:
        msg = (
            f"{route} declares cache and own_checks=True, so one caller's "
            f"response would be served to another. Drop cache."
        )
        raise ValueError(msg)
    methods = declaration.methods
    if methods is None or not methods <= _READS:
        answered = "every method" if methods is None else sorted(methods)
        msg = (
            f"{route} declares cache and answers {answered}. Only a GET or "
            f"HEAD response is cached, so declare cache on the reads alone."
        )
        raise ValueError(msg)


def route_name(declaration: RouteDeclaration) -> str:
    """Return the route as a refusal names it: its methods, then its path."""
    methods = declaration.methods
    if methods is None:
        return declaration.path
    return f"{' '.join(sorted(methods))} {declaration.path}"
