"""What a route requires of its caller, checked the same way on every framework.

Each integration declares the requirement its own way: a FastAPI
`Security`, a Starlette decorator, a Litestar guard. They all check it
here, so a caller one framework admits is admitted by every one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from grelmicro._caller import is_authenticated
from grelmicro.errors import AuthenticationRequiredError, InsufficientScopeError
from grelmicro.http._authentication import TOKEN_SCOPE_KEY, recorded
from grelmicro.security.principal import VerifiedToken

if TYPE_CHECKING:
    from collections.abc import MutableMapping, Sequence

    Scope = MutableMapping[str, Any]

__all__ = ["Requirement", "requirement_for", "verified_token"]


class Requirement:
    """An authenticated caller holding every scope named.

    Built once for the route that declares it, so a request only reads the
    caller and compares its scopes.
    """

    __slots__ = ("_held_by", "scopes")

    def __init__(self, scopes: tuple[str, ...]) -> None:
        """Require every scope in `scopes`, in the order the route named them."""
        self.scopes = scopes
        self._held_by = frozenset(scopes).issubset

    def caller(self, scope: Scope) -> Any:  # noqa: ANN401
        """Return the caller of the request, once it meets the requirement.

        Raises:
            AuthenticationRequiredError: If the request carried no
                authenticated caller.
            InsufficientScopeError: If the caller lacks a scope named.
        """
        caller = scope.get("user")
        if not is_authenticated(caller):
            raise recorded(
                scope, AuthenticationRequiredError(scopes=self.scopes)
            )
        if not self._held_by(getattr(caller, "scopes", ())):
            raise recorded(scope, InsufficientScopeError(scopes=self.scopes))
        return caller


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


def verified_token(scope: Scope, scopes: tuple[str, ...] = ()) -> VerifiedToken:
    """Return the bearer token the request presented, once it verified.

    Raises:
        AuthenticationRequiredError: If the request carried no token that
            `AuthenticatedRequests` verified. It names `scopes`.
    """
    token = scope.get(TOKEN_SCOPE_KEY)
    if not isinstance(token, VerifiedToken):
        raise recorded(scope, AuthenticationRequiredError(scopes=scopes))
    return token
