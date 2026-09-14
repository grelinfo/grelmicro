"""The party a request was authenticated as.

Read more in the [JWT](../security/jwt.md) docs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["Principal"]


class Principal(Protocol):
    """The party a request was authenticated as, whatever proved it.

    A handler reads this rather than the credential behind it, so the same
    handler serves a bearer token today and another scheme later.
    `JWTClaims` satisfies it.

    Key a caller by `issuer` and `subject` together. A subject is unique only
    within the issuer that assigned it, and an email or a username can be
    reassigned to somebody else.
    """

    @property
    def subject(self) -> str | None:
        """Who the caller is, as the issuer names them."""
        ...  # pragma: no cover

    @property
    def issuer(self) -> str | None:
        """Who vouched for the caller."""
        ...  # pragma: no cover

    @property
    def scopes(self) -> frozenset[str]:
        """What the caller was granted."""
        ...  # pragma: no cover

    @property
    def claims(self) -> Mapping[str, Any]:
        """Everything the credential says about the caller, read-only."""
        ...  # pragma: no cover

    @property
    def is_authenticated(self) -> bool:
        """Whether the caller was authenticated at all."""
        ...  # pragma: no cover
