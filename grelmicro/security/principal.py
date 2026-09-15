"""The party a request was authenticated as.

Read more in the [JWT](../security/jwt.md) docs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["Principal", "VerifiedToken"]


class VerifiedToken:
    """The bearer token a request presented, once it verified.

    `AuthenticatedRequests` builds one for every request it authenticates,
    and a handler reads it through `CurrentToken` to act for the caller, such
    as exchanging it for a token issued to another API. It cannot be built
    from a string, so a token that never verified cannot pass for one.

    Its `repr` never shows the token.
    """

    __slots__ = ("expires_at", "value")

    value: str
    """The encoded token, exactly as the request presented it."""

    expires_at: int | None
    """When the token expires, in seconds since the epoch, or `None` when its
    verifier does not say."""

    def __init__(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        """Refuse construction: only a token that verified becomes one."""
        msg = (
            "VerifiedToken is built by AuthenticatedRequests for a token that"
            " verified. Read it with CurrentToken, or current_token(request)."
        )
        raise TypeError(msg)

    def __repr__(self) -> str:
        """Return the class and the expiry, without the token."""
        return f"VerifiedToken(expires_at={self.expires_at!r})"


def _verified_token(value: str, expires_at: int | None) -> VerifiedToken:
    """Return `value` as a `VerifiedToken`, for a token that verified."""
    token = object.__new__(VerifiedToken)
    token.value = value
    token.expires_at = expires_at
    return token


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
