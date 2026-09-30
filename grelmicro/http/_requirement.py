"""What a route requires of its caller, declared and checked in one place.

Each integration declares the requirement its own way: a FastAPI
`Security`, a Starlette decorator, a Litestar guard. They all build it,
mark it and check it here, so a caller one framework admits is admitted by
every one, and the startup checks and the OpenAPI document find every
declaration the same way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from grelmicro._caller import is_authenticated
from grelmicro.errors import (
    AuthenticationRequiredError,
    InsufficientScopeError,
    _scope_tokens,
)
from grelmicro.security._events import SCOPE_KEY
from grelmicro.security.principal import VerifiedToken

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping, Sequence

    Scope = MutableMapping[str, Any]

__all__ = [
    "AUTHENTICATED",
    "TOKEN_SCOPE_KEY",
    "Requirement",
    "declared_scopes",
    "recorded",
    "requirement_for",
]

TOKEN_SCOPE_KEY: Final = "grelmicro.verified_token"  # noqa: S105
"""Where the middleware leaves the bearer token it verified, as a `VerifiedToken`."""

_MARKER: Final = "__grelmicro_authenticated__"
"""Set on what a route declares a requirement with, so a reader finds it.

Read by attribute rather than by identity, so a declaration made before its
module was imported again is still recognised as one.
"""

_FROM_REQUEST: Final = "from-request"
"""The mark of a declaration whose scopes arrive with the request."""

_OPTIONAL: Final = "__grelmicro_optional_caller__"
"""Set on what reads the caller when there is one, and requires none."""


def recorded[E: BaseException](scope: Scope, error: E) -> E:
    """Return `error`, recorded as a refusal when authentication handled the request.

    For a refusal a route raises after the middleware authenticated the
    request, such as a missing scope. A request authentication left alone
    records nothing.
    """
    record: Callable[[Scope, BaseException], None] | None = scope.get(SCOPE_KEY)
    if record is not None:
        record(scope, error)
    return error


class Requirement:
    """An authenticated caller holding every scope named.

    Built once for the route that declares it, so a request only reads the
    caller and compares its scopes.
    """

    __slots__ = ("_held_by", "scopes")

    def __init__(self, scopes: Sequence[str] = ()) -> None:
        """Require every scope in `scopes`, in the order the route names them.

        Raises:
            TypeError: If `scopes` is a single string.
            ValueError: If a scope is not an OAuth scope token.
        """
        self.scopes = _scope_tokens(scopes)
        self._held_by = frozenset(self.scopes).issubset

    def caller(self, scope: Scope) -> Any:  # noqa: ANN401
        """Return the caller of the request, once it meets the requirement.

        Raises:
            AuthenticationRequiredError: If the request carried no
                authenticated caller.
            InsufficientScopeError: If the caller lacks a scope named.
        """
        caller = scope.get("user")
        if not is_authenticated(caller):
            raise recorded(scope, AuthenticationRequiredError())
        # Starlette's authentication and `AuthenticatedRequests` both grant
        # scopes on `scope["auth"]`. A caller set without credentials there
        # carries its own.
        granted: Any = getattr(scope.get("auth"), "scopes", None)
        if granted is None:
            granted = getattr(caller, "scopes", ())
        if not self._held_by(granted):
            raise recorded(scope, InsufficientScopeError(scopes=self.scopes))
        return caller

    def token(self, scope: Scope) -> VerifiedToken:
        """Return the bearer token the caller presented, once it verified.

        Checks the caller first, so a caller lacking a scope is refused
        for the scope rather than for the token.

        Raises:
            AuthenticationRequiredError: If the request carried no
                authenticated caller, or no token `AuthenticatedRequests`
                verified.
            InsufficientScopeError: If the caller lacks a scope named.
        """
        self.caller(scope)
        token = scope.get(TOKEN_SCOPE_KEY)
        if not isinstance(token, VerifiedToken):
            raise recorded(scope, AuthenticationRequiredError())
        return token

    def declare[T](self, target: T) -> T:
        """Mark `target` as declaring this requirement, and return it."""
        setattr(target, _MARKER, self.scopes)
        return target


AUTHENTICATED: Final = Requirement()
"""An authenticated caller, whatever scopes it holds."""


def declare_from_request[T](target: T) -> T:
    """Mark `target` as declaring a requirement whose scopes arrive with the request.

    For a FastAPI dependency, whose scopes are the `Security` declarations
    around it. A reader takes them from the dependency tree.
    """
    setattr(target, _MARKER, _FROM_REQUEST)
    return target


def declared_scopes(target: object) -> tuple[str, ...] | None:
    """Return the scopes `target` declares, or `None` when it declares none.

    A declaration whose scopes arrive with the request answers no scopes
    of its own.
    """
    marked = getattr(target, _MARKER, None)
    if marked is None:
        return None
    return () if marked == _FROM_REQUEST else tuple(marked)


def declare_optional[T](target: T) -> T:
    """Mark `target` as reading the caller when there is one, requiring none."""
    setattr(target, _OPTIONAL, True)
    return target


def declares_optional(target: object) -> bool:
    """Return whether `target` reads the caller when there is one, requiring none."""
    return bool(getattr(target, _OPTIONAL, False))


_REQUIREMENTS: Final[dict[tuple[str, ...], Requirement]] = {}
"""Every requirement built from scopes that arrive with the request.

Keyed by the scopes, which only the routes declare, so it holds one entry
per distinct set of scopes an app names.
"""


def requirement_for(scopes: Sequence[str]) -> Requirement:
    """Return the requirement for `scopes`, built the first time they are seen.

    For a framework that hands the scopes over on each request rather than
    when the route is declared.
    """
    key = tuple(scopes)
    requirement = _REQUIREMENTS.get(key)
    if requirement is None:
        requirement = _REQUIREMENTS[key] = Requirement(key)
    return requirement
