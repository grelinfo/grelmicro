"""What each route requires of a request, as the framework integration declares it."""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Any, Final, Protocol

from typing_extensions import Doc

from grelmicro._duration import in_range
from grelmicro.errors import _scope_tokens

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping

    Scope = MutableMapping[str, Any]
    Message = MutableMapping[str, Any]
    Receive = Callable[[], Awaitable[Message]]
    Send = Callable[[Message], Awaitable[None]]
    ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

__all__ = ["Gate", "RouteDeclaration", "refuse_impossible", "route_name"]

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
    checked_above: Annotated[
        bool,
        Doc(
            "Checks run before the route that are not its own, such as "
            "middleware on a mount around it, so its answer can depend on "
            "the caller. Its response is never replayed across callers, "
            "and a `cache` it declares still holds."
        ),
    ] = False
    cache: Annotated[
        bool | timedelta,
        Doc(
            "`CachedResponses` may store the route's response. `True` keeps "
            "it for the TTL the component is configured with, a "
            "`timedelta` keeps it that long, and a number is refused."
        ),
    ] = False
    shared: Annotated[
        bool,
        Doc(
            "The route requires a caller and answers every caller it admits "
            "the same, so `CachedResponses` serves one stored response to "
            "all of them, a credential included. Without it, a request "
            "carrying a credential is answered by the handler."
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
        TypeError: If `cache` is neither a boolean nor a `timedelta`.
        ValueError: If `methods` is empty or holds a method in lower case,
            the route is anonymous and requires scopes, it caches and runs
            checks of its own, it caches a method other than `GET` or
            `HEAD`, its cache TTL is not greater than zero or is over 100
            years, a scope is not an OAuth scope token, or it is `shared`
            without a `cache` or while anonymous.
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
    _refuse_impossible_sharing(declaration, route)


def _refuse_impossible_sharing(
    declaration: RouteDeclaration, route: str
) -> None:
    """Refuse `shared` on a route that caches nothing or requires no caller.

    Raises:
        ValueError: If the route is `shared` without a `cache`, or while
            anonymous.
    """
    if not declaration.shared:
        return
    if declaration.cache is False:
        msg = (
            f"{route} declares shared=True and no cache, so nothing is "
            f"shared. Declare a cache, or drop shared=True."
        )
        raise ValueError(msg)
    if declaration.anonymous:
        msg = (
            f"{route} declares anonymous=True and shared=True. A request "
            f"carrying a credential to a public route is answered by its "
            f"handler, so nothing is shared. Drop shared=True."
        )
        raise ValueError(msg)


def _refuse_impossible_cache(declaration: RouteDeclaration, route: str) -> None:
    """Refuse a `cache` the route cannot honor.

    Raises:
        TypeError: If `cache` is neither a boolean nor a `timedelta`.
        ValueError: If the route caches and runs checks of its own, caches
            a method other than `GET` or `HEAD`, or its TTL is not greater
            than zero or is over 100 years.
    """
    cache = declaration.cache
    if cache is False:
        return
    if cache is not True:
        if not isinstance(cache, timedelta):
            msg = (
                f"{route} declares cache={cache!r}. Pass True for the TTL the "
                f"component is configured with, or a timedelta."
            )
            raise TypeError(msg)
        try:
            in_range(cache, f"{route} cache")
        except ValueError as error:
            msg = (
                f"{error}. Pass a timedelta, or True for the TTL the "
                f"component is configured with."
            )
            raise ValueError(msg) from None
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


class Gate(Protocol):
    """What an integration's `install_route_gate(app, gate)` is handed.

    Called with what the router dispatches to and the route's
    declarations, it returns the app to dispatch to in its place. That app
    refuses a request the declaration of its method does not admit, and
    serves the one it admits.

    ```python
    def install_route_gate(app, gate: Gate) -> None:
        for route in app.routes:
            route.app = gate(route.app, declare(route))
    ```

    Read more in the [Plugins](../architecture/plugins.md#declare-the-routes)
    docs.
    """

    def __call__(
        self,
        app: ASGIApp,
        /,
        *declarations: RouteDeclaration,
        name: Callable[[Scope], str] | None = None,
        door: bool = False,
    ) -> ASGIApp:
        """Return the app to dispatch to in place of `app`.

        `name`, called with the request's scope, names a refusal. With
        `door`, `app` is the entrance of a subtree whose routes carry gates
        of their own.
        """
        ...  # pragma: no cover
