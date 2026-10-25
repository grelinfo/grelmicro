"""The errors security raises, importable without the verifier.

`grelmicro.security.jwt` and `grelmicro.security.bans` re-export each class,
so every import path returns the same class.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Final

from typing_extensions import Doc

from grelmicro.errors import AdmissionError, GrelmicroError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["ClientBannedError", "TokenRejectedError", "TokenRejectedReason"]


class TokenRejectedReason(StrEnum):
    """Why a token was rejected.

    A stable tag a caller branches on rather than message text. Each member
    compares equal to the string it names, so `error.reason == "expired"`
    reads the same as `error.reason is TokenRejectedReason.EXPIRED`.
    """

    ALGORITHM = "algorithm"
    """The token asks for an algorithm its key does not verify."""

    AUDIENCE = "audience"
    """The token was issued for another audience."""

    BINDING = "binding"
    """The token is bound to a key, and no proof of possession is checked."""

    EXPIRED = "expired"
    """The token is past its `exp`."""

    INVALID = "invalid"
    """The token failed for a reason with no tag of its own."""

    ISSUER = "issuer"
    """The token was issued by another issuer."""

    MALFORMED = "malformed"
    """The token is not a well-formed JWS."""

    MISSING_CLAIM = "missing-claim"
    """The token lacks a claim the policy requires."""

    NOT_YET_VALID = "not-yet-valid"
    """The token is before its `nbf`, or claims an `iat` still to come."""

    REVOKED = "revoked"
    """The token verified, and the service no longer accepts it."""

    SCHEME = "scheme"
    """The `Authorization` header carries no bearer token."""

    SIGNATURE = "signature"
    """The signature does not match the key."""

    TYPE = "type"
    """The token declares a type other than an access token."""

    UNKNOWN_KEY = "unknown-key"
    """No loaded key matches the token's `kid`."""


_MESSAGES: Final[Mapping[TokenRejectedReason, str]] = {
    TokenRejectedReason.ALGORITHM: (
        "The token uses an algorithm this verifier does not accept."
    ),
    TokenRejectedReason.AUDIENCE: "The token was issued for another audience.",
    TokenRejectedReason.BINDING: (
        "The token is bound to a key this service does not check."
    ),
    TokenRejectedReason.EXPIRED: "The token has expired.",
    TokenRejectedReason.INVALID: "The token is not valid.",
    TokenRejectedReason.ISSUER: "The token was issued by another issuer.",
    TokenRejectedReason.MALFORMED: "The token is not a well-formed JWS.",
    TokenRejectedReason.MISSING_CLAIM: "The token is missing a required claim.",
    TokenRejectedReason.NOT_YET_VALID: "The token is not valid yet.",
    TokenRejectedReason.REVOKED: "The token is no longer accepted.",
    TokenRejectedReason.SCHEME: (
        "The Authorization header does not carry a bearer token."
    ),
    TokenRejectedReason.SIGNATURE: "The signature does not match the key.",
    TokenRejectedReason.TYPE: (
        "The token is not an access token of an accepted type."
    ),
    TokenRejectedReason.UNKNOWN_KEY: "No configured key matches the token.",
}
"""What each rejection says. No message quotes the token or the key."""


class TokenRejectedError(GrelmicroError, ValueError):
    """A token failed verification.

    `reason` names what failed, so a caller branches on it rather than on
    message text. Neither the reason nor the message quotes the token: it is
    a live credential and the message reaches logs and error responses.
    """

    resource_metadata: str | None = None
    """The URL of the protected resource metadata its challenge points at.

    Set by `AuthenticatedRequestsMiddleware` on a refusal a route raised,
    when the service publishes the metadata, so the challenge names it
    wherever the refusal is rendered.
    """

    subject: str | None = None
    """The `sub` of a token whose signature verified before a claim failed.

    Set for a token that is expired, not yet valid, issued by or for someone
    else, missing a claim, or bound to a key. `None` when the token failed
    before or at its signature, or carries no non-empty string `sub`, so a
    claim from a token nobody vouched for never reaches it.
    """

    def __init__(
        self,
        reason: Annotated[
            TokenRejectedReason | str,
            Doc(
                "What failed. A tag this module does not know reads as invalid."
            ),
        ],
        *,
        subject: Annotated[
            str | None,
            Doc("The `sub` of the token, only once its signature verified."),
        ] = None,
    ) -> None:
        """Initialize the error."""
        try:
            known = TokenRejectedReason(reason)
        except ValueError:
            known = TokenRejectedReason.INVALID
        self.reason: TokenRejectedReason = known
        self.subject = subject
        super().__init__(_MESSAGES[known])


class ClientBannedError(AdmissionError, RuntimeError):
    """The caller is banned, so its token was not looked at.

    Distinct from `TokenRejectedError` because it says nothing about the
    token. Answer it with `429`, not `401`: the caller is being refused for
    what it did before this request, and a fresh token would not change it.
    `retry_after` says how long the ban has left.
    """

    def __init__(
        self,
        *,
        retry_after: Annotated[
            float, Doc("Seconds the ban has left, `0.0` when not known.")
        ] = 0.0,
    ) -> None:
        """Initialize the error."""
        self.retry_after = retry_after
        super().__init__("Too many rejected tokens from this client.")


# Each class reports the public module it is imported from.
TokenRejectedReason.__module__ = "grelmicro.security.jwt"
TokenRejectedError.__module__ = "grelmicro.security.jwt"
ClientBannedError.__module__ = "grelmicro.security.bans"
