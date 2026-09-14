"""JWT verification for inbound requests.

grelmicro checks the token a caller presents, it never issues one.
`JWTVerifier` resolves its keys and claim policy once, then answers every
request from a compiled core: key selection, signature check, `exp`, `nbf`,
`aud` and `iss` checks and claim decoding happen in one call.

Read more in the [JWT](../security/jwt.md) docs.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from logging import getLogger
from time import monotonic, time
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Final,
    Literal,
    Protocol,
    Self,
    get_args,
)

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    SecretBytes,
    field_validator,
    model_validator,
)
from pydantic_settings import NoDecode
from typing_extensions import Doc

from grelmicro._config import (
    Reconfigurable,
    env_prefixes,
    parse_csv_or_json,
    resolve_config,
)
from grelmicro.errors import (
    DependencyNotFoundError,
    GrelmicroError,
    SettingsValidationError,
)
from grelmicro.security.jwks import (
    SigningKeysUnavailableError,
    fetch_with_httpx,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import TracebackType

    from grelmicro.security.jwks import Fetcher

__all__ = [
    "ALGORITHMS",
    "JWKSConfig",
    "JWTClaims",
    "JWTKey",
    "JWTKeysConfig",
    "JWTPolicy",
    "JWTVerifier",
    "TokenRejectedError",
    "TokenRejectedReason",
    "TokenVerifier",
    "unverified_header",
]

logger = getLogger("grelmicro.security.jwt")

_JWK_ALGORITHM: Final[Mapping[tuple[str, str], str]] = {
    ("RSA", ""): "RS256",
    ("EC", "P-256"): "ES256",
    ("EC", "P-384"): "ES384",
    ("OKP", "Ed25519"): "EdDSA",
}
"""Algorithm assumed for a JWK that names none, by key type and curve.

Entra ID publishes signing keys without an `alg`. The verifier pins one
algorithm per key either way, so a token asking for a different one is
refused whether the algorithm was published or inferred.
"""

_SecretAlgorithm = Literal["HS256", "HS384", "HS512"]
"""Algorithms that verify with a shared secret."""

_AsymmetricAlgorithm = Literal[
    "EdDSA",
    "ES256",
    "ES384",
    "PS256",
    "PS384",
    "PS512",
    "RS256",
    "RS384",
    "RS512",
]
"""Algorithms that verify with a public key."""

_Algorithm = Literal[_SecretAlgorithm, _AsymmetricAlgorithm]
"""Every algorithm a key may verify."""

ALGORITHMS: Final = frozenset(get_args(_Algorithm))
"""Signature algorithms a verifier accepts. `none` is not one of them."""

_SECRET_BYTES: Final[Mapping[str, int]] = {
    "HS256": 32,
    "HS384": 48,
    "HS512": 64,
}
"""Shortest secret each `HS*` algorithm accepts, its hash output in bytes.

RFC 7518 section 3.2 requires a key at least as long as the hash. A shorter
secret can be recovered by brute force from a single token signed with it.
"""

BEARER_PREFIX: Final = "Bearer "
"""Scheme an `Authorization` header must carry, per RFC 6750.

Matched without regard to case, because RFC 7235 makes the scheme token
case-insensitive and clients and proxies do normalise it.
"""

_BEARER_LOWER: Final = BEARER_PREFIX.lower()
_BEARER_LENGTH: Final = len(BEARER_PREFIX)

_ONE_MIB: Final = 1_048_576

_NOT_LOADED: Final = (
    "No key set has been loaded. Open the verifier with `async with`, or"
    " await refresh(), before verifying."
)

_ASYMMETRIC_KEY_TYPES: Final = frozenset({"EC", "OKP", "RSA"})
"""Key types a JWKS may publish for verifying a signature.

A JWKS is served to anyone who asks, so a symmetric key in one is a secret
that is not secret: whoever can read the document can mint tokens with it.
A key of any other type is skipped. Configure a shared secret with
`JWTKey.secret(...)`, from somewhere that is not published.
"""


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
    """The token is before its `nbf`."""

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

    def __init__(
        self,
        reason: Annotated[
            TokenRejectedReason | str,
            Doc(
                "What failed. A tag this module does not know reads as invalid."
            ),
        ],
    ) -> None:
        """Initialize the error."""
        try:
            known = TokenRejectedReason(reason)
        except ValueError:
            known = TokenRejectedReason.INVALID
        self.reason: TokenRejectedReason = known
        super().__init__(_MESSAGES[known])


class JWTKey(BaseModel, frozen=True):
    """One verification key and the algorithm it verifies.

    Build one with `pem`, `secret` or `jwk`. The constructor takes the same
    fields, for a key that arrives as data, such as from a settings file.

    The key material is held as a secret, so it never appears in a `repr`, a
    log line or a dumped configuration.

    Example:
        ```python
        JWTKey.pem(public_pem, algorithm="RS256", kid="2026-09")
        ```
    """

    algorithm: Annotated[
        _Algorithm,
        Doc("Signature algorithm this key verifies, such as `RS256`."),
    ]
    key: Annotated[
        SecretBytes,
        Doc(
            "The key material: a PEM public key, an `HS*` secret, or one JWK"
            " written as JSON."
        ),
    ]
    kid: Annotated[
        str | None,
        Doc("Key id this key answers to. `None` serves tokens with no `kid`."),
    ] = None
    format: Annotated[
        Literal["jwk", "pem", "secret"] | None,
        Doc(
            "How `key` is written. `None` reads a secret for an `HS*`"
            " algorithm and a PEM for every other one."
        ),
    ] = None

    @classmethod
    def pem(
        cls,
        key: Annotated[bytes | str, Doc("The PEM public key.")],
        *,
        algorithm: Annotated[
            _AsymmetricAlgorithm,
            Doc("Signature algorithm this key verifies, such as `RS256`."),
        ],
        kid: Annotated[
            str | None,
            Doc("Key id this key answers to. `None` serves tokens with none."),
        ] = None,
    ) -> Self:
        """Build a key from a PEM public key."""
        return cls(
            algorithm=algorithm, key=_material(key), kid=kid, format="pem"
        )

    @classmethod
    def secret(
        cls,
        key: Annotated[bytes | str, Doc("The shared secret.")],
        *,
        algorithm: Annotated[
            _SecretAlgorithm,
            Doc("`HS256`, `HS384` or `HS512`."),
        ],
        kid: Annotated[
            str | None,
            Doc("Key id this key answers to. `None` serves tokens with none."),
        ] = None,
    ) -> Self:
        """Build a key from a shared secret, for tokens your own service signs.

        The secret must be at least as long as the algorithm's hash: 32 bytes
        for `HS256`, 48 for `HS384` and 64 for `HS512`, as RFC 7518 requires.
        Load it from a secret store or a mounted file, never from a document
        that is published, which is why a JWKS never supplies one.
        """
        return cls(
            algorithm=algorithm, key=_material(key), kid=kid, format="secret"
        )

    @classmethod
    def jwk(
        cls,
        jwk: Annotated[Mapping[str, Any], Doc("One key from a JWKS document.")],
        *,
        algorithm: Annotated[
            _AsymmetricAlgorithm | None,
            Doc("Algorithm to pin when the JWK names none."),
        ] = None,
    ) -> Self:
        """Build a key from one JWK, as published at a JWKS endpoint.

        The `kid` and `alg` are read from the JWK. A provider that publishes
        no `alg`, as Entra ID does, needs one here or gets the algorithm its
        key type implies. A symmetric JWK is refused, because a published
        key cannot be a shared secret.
        """
        named = jwk.get("alg") or algorithm
        if not named:
            key_type = str(jwk.get("kty", ""))
            curve = str(jwk.get("crv", "")) if key_type in {"EC", "OKP"} else ""
            named = _JWK_ALGORITHM.get((key_type, curve))
        if not named:
            msg = (
                "the JWK names no alg and none can be inferred from its kty,"
                " so pass algorithm= to pin one"
            )
            raise SettingsValidationError(msg)
        return cls(
            algorithm=str(named),  # ty: ignore[invalid-argument-type]
            key=json.dumps(dict(jwk)).encode(),
            kid=jwk.get("kid"),
            format="jwk",
        )

    @model_validator(mode="after")
    def _check_material(self) -> Self:
        """Refuse key material that does not fit its algorithm.

        A secret goes with an `HS*` algorithm and nothing else, so a PEM can
        never be read as an HMAC secret, and a secret must be as long as the
        hash RFC 7518 sizes it by.
        """
        secret = self.algorithm in _SECRET_BYTES
        written = self.format or ("secret" if secret else "pem")
        if secret != (written == "secret"):
            msg = (
                "an HS* algorithm takes a secret, and a secret takes only an"
                " HS* algorithm"
            )
            raise ValueError(msg)
        if secret and (
            len(self.key.get_secret_value()) < _SECRET_BYTES[self.algorithm]
        ):
            msg = (
                "the secret is shorter than the hash output of its algorithm,"
                " which RFC 7518 forbids"
            )
            raise ValueError(msg)
        return self


def _material(key: bytes | str) -> bytes:
    """Return key material as bytes, encoding a PEM or secret given as text."""
    return key.encode() if isinstance(key, str) else key


class JWTPolicy(BaseModel, frozen=True):
    """What a verifier checks, and what it remembers.

    Everything here is independent of where the keys came from, so a verifier
    built from a PEM and one built from a JWKS endpoint enforce it the same
    way.
    """

    audience: Annotated[
        list[str] | None,
        NoDecode,
        BeforeValidator(parse_csv_or_json),
        Doc(
            "Accepted `aud` values, one or several. Required, because a"
            " resource server has to check that a token was issued for it."
            " `None` says the service identifies with no audience, and RFC"
            " 7519 then requires refusing any token that carries an `aud`"
            " claim, which fits a provider whose access tokens carry none,"
            " such as AWS Cognito."
        ),
    ]
    issuer: Annotated[
        list[str],
        NoDecode,
        BeforeValidator(parse_csv_or_json),
        Doc("Accepted `iss` values. Empty leaves the issuer unchecked."),
    ] = Field(default_factory=list)
    leeway: Annotated[
        int,
        Doc("Seconds of clock skew allowed on `exp` and `nbf`."),
    ] = 0
    token_type: Annotated[
        Literal["at+jwt"] | None,
        Doc(
            "Type every token must declare in its `typ` header. `None` accepts"
            " a token that declares none, `JWT`, `JOSE` or `at+jwt`, and"
            " refuses any other type, such as a DPoP proof or a logout token."
            " `at+jwt` requires the access token type of RFC 9068, which"
            " Microsoft Entra ID does not send and Keycloak sends only when a"
            " client asks for it."
        ),
    ] = None
    scope_claims: Annotated[
        list[str],
        NoDecode,
        BeforeValidator(parse_csv_or_json),
        Doc(
            "Claims the granted scopes are read from, in order. The first one"
            " the token carries decides, as a space-separated string or an"
            " array of strings. Microsoft Entra ID writes `scp` as a string"
            " and Okta writes it as an array. A claim of any other shape"
            " grants nothing."
        ),
    ] = Field(default_factory=lambda: ["scope", "scp"])
    required: Annotated[
        list[str],
        NoDecode,
        BeforeValidator(parse_csv_or_json),
        Doc(
            "Further claims that must be present. `exp` is always required"
            " and does not need naming, and naming an `audience` or an"
            " `issuer` requires that claim too, so this only ever adds to"
            " what is enforced."
        ),
    ] = Field(default_factory=lambda: ["exp"])
    cache_size: Annotated[
        int,
        Doc(
            "Verified tokens held in memory. A client resends one token until"
            " it expires, so a hit answers without repeating the signature"
            " check. Zero turns the cache off."
        ),
    ] = 1024
    cache_key: Annotated[
        str,
        Doc(
            "What the cache holds as its key. `sha256` keeps a digest, so a"
            " live bearer token is not held in memory for the lifetime of"
            " the entry. It is computed in the core and costs around 80 ns"
            " on a hit, under 1% of a verification. `token` keeps the"
            " encoded token instead, which is what an in-process cache"
            " normally does and is the faster of the two."
        ),
    ] = "sha256"
    cache_ttl: Annotated[
        float,
        Doc(
            "Seconds a verified token stays cached. An entry also never"
            " outlives the token's own `exp`, so this is the bound that"
            " matters for a long-lived token: it caps how long a token"
            " withdrawn upstream keeps being accepted from memory."
        ),
    ] = 300.0

    @field_validator("audience")
    @classmethod
    def _check_audience(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse an empty audience, which says neither which one nor none."""
        if value is not None and not value:
            msg = (
                "audience must name at least one value, or be None to answer"
                " to no audience"
            )
            raise ValueError(msg)
        return value

    @field_validator("scope_claims")
    @classmethod
    def _check_scope_claims(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse an empty list, which would grant no scope to any token."""
        if not value:
            msg = "scope_claims must name at least one claim"
            raise ValueError(msg)
        return value

    @field_validator("cache_key")
    @classmethod
    def _check_cache_key(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse a key strategy the verifier does not implement."""
        if value not in {"sha256", "token"}:
            msg = "cache_key must be 'token' or 'sha256'"
            raise ValueError(msg)
        return value

    @field_validator("leeway", "cache_size", "cache_ttl")
    @classmethod
    def _check_not_negative(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse a negative count of seconds or entries."""
        if value < 0:
            msg = "value must not be negative"
            raise ValueError(msg)
        return value

    def enforced_claims(self) -> list[str]:
        """Return the claims a token must carry under this policy.

        Naming an `audience` or an `issuer` requires the matching claim. A
        token that simply omits `aud` satisfies an audience check otherwise,
        which turns a configured check into one that silently does not apply
        to the tokens most worth checking. Set `audience=None` for a
        provider whose tokens carry none, such as an AWS Cognito access
        token.
        """
        # `exp` is seeded rather than defaulted, so naming any other claim
        # adds to the policy instead of replacing it. Without this,
        # `required=["tenant"]` would read as tightening the policy and
        # would in fact drop the expiry check, and a token carrying no
        # `exp` would then be accepted for as long as its key is published.
        enforced = ["exp"]
        enforced += [claim for claim in self.required if claim != "exp"]
        for claim, configured in (("aud", self.audience), ("iss", self.issuer)):
            if configured and claim not in enforced:
                enforced.append(claim)
        return enforced


class JWTKeysConfig(JWTPolicy):
    """A claim policy together with the keys that verify against it."""

    keys: Annotated[
        list[JWTKey],
        Doc("Keys this verifier accepts, selected by the token's `kid`."),
    ]

    @classmethod
    def from_jwks(
        cls,
        jwks: Annotated[
            Mapping[str, Any],
            Doc("A JWKS document, as an OIDC provider serves it."),
        ],
        *,
        algorithm: Annotated[
            _AsymmetricAlgorithm | None,
            Doc("Algorithm to pin for keys that name none."),
        ] = None,
        **policy: Annotated[Any, Doc("Any other `JWTKeysConfig` setting.")],  # noqa: ANN401
    ) -> JWTKeysConfig:
        """Build a config from a JWKS document.

        Keys marked for encryption are skipped, and so are symmetric keys: a
        signature is never verified with the first, and the second would be a
        shared secret published to anyone who can read the document.
        Everything else in `policy` is passed through.

        Example:
            ```python
            jwks = httpx.get(f"{issuer}/.well-known/jwks.json").json()
            config = JWTKeysConfig.from_jwks(
                jwks, audience="my-api", issuer=issuer
            )
            ```
        """
        keys = []
        for jwk in jwks.get("keys", []):
            if not _usable_for_signatures(jwk):
                continue
            try:
                keys.append(JWTKey.jwk(jwk, algorithm=algorithm))
            except (SettingsValidationError, ValueError):
                # A provider is free to publish a key type this does not read.
                # Skipping it keeps the keys that do work, where failing the
                # whole document would take authentication down over a key
                # nothing was going to be verified with.
                continue
        return cls(keys=keys, **policy)

    @field_validator("keys")
    @classmethod
    def _check_keys(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse an empty key set, and two keys claiming one `kid`."""
        if not value:
            msg = "keys must name at least one key"
            raise ValueError(msg)
        seen = [key.kid for key in value]
        if len(set(seen)) != len(seen):
            msg = "each kid must appear once, and only one key may omit it"
            raise ValueError(msg)
        return value


class JWKSConfig(JWTPolicy):
    """Where the keys are published, and the claim policy they enforce.

    Carries every `JWTPolicy` setting, so a verifier fed from a JWKS endpoint
    checks exactly what one built from a PEM checks.
    """

    url: Annotated[
        str,
        Doc(
            "The JWKS endpoint. Must be `https`, because the keys it serves"
            " decide who is believed."
        ),
    ]
    ttl: Annotated[
        float,
        Doc("Seconds a fetched document is treated as current."),
    ] = 3600.0
    retry_interval: Annotated[
        float,
        Doc(
            "Least time between two fetches. This is what stops a caller"
            " presenting invented `kid` values from making the service fetch"
            " on demand."
        ),
    ] = 60.0
    timeout: Annotated[
        float,
        Doc("Seconds to wait for the endpoint before giving up."),
    ] = 5.0
    max_bytes: Annotated[
        int,
        Doc(
            "Largest document accepted, so a hostile endpoint cannot exhaust"
            " memory."
        ),
    ] = _ONE_MIB
    max_keys: Annotated[
        int,
        Doc("Most keys accepted from one document."),
    ] = 32
    algorithm: Annotated[
        _AsymmetricAlgorithm | None,
        Doc("Algorithm to pin for keys that publish none, as Entra ID does."),
    ] = None

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse a URL that is not `https`."""
        if not str(value).startswith("https://"):
            msg = "url must be an https URL"
            raise ValueError(msg)
        return value

    @field_validator("ttl", "retry_interval", "timeout")
    @classmethod
    def _check_positive(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse a duration that is zero or below."""
        if value <= 0:
            msg = "value must be greater than zero"
            raise ValueError(msg)
        return value

    @field_validator("max_bytes", "max_keys")
    @classmethod
    def _check_limit(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse a limit that would accept nothing."""
        if value < 1:
            msg = "value must be at least one"
            raise ValueError(msg)
        return value


_PUBLISHING_FIELDS: Final = frozenset(JWKSConfig.model_fields) - frozenset(
    JWTPolicy.model_fields
)
"""Fields that say where keys are published, never how a token is checked.

Read off the two classes rather than listed, so a field added to either one
lands on the right side without anybody remembering to add it here.
"""


@dataclass(frozen=True, slots=True)
class JWTClaims:
    """The claims of a verified token.

    `claims` holds every claim as it arrived. The named fields are the
    registered claims lifted out of it, so the common path needs no dict
    lookups. It satisfies `Principal`, and answers what Starlette reads off
    `request.user`, so it can stand for the caller in a request scope.
    """

    claims: Annotated[
        Mapping[str, Any],
        Doc(
            "Every claim the token carries, read-only. A verified claim set"
            " is shared by every request presenting that token while it is"
            " cached, so writing into it would change what a later request"
            " is authorized as."
        ),
    ]
    subject: Annotated[str | None, Doc("The `sub` claim.")]
    issuer: Annotated[str | None, Doc("The `iss` claim.")]
    audience: Annotated[str | list[str] | None, Doc("The `aud` claim.")]
    expires_at: Annotated[int | None, Doc("The `exp` claim, in seconds.")]
    issued_at: Annotated[int | None, Doc("The `iat` claim, in seconds.")]
    token_id: Annotated[str | None, Doc("The `jti` claim.")]
    scopes: Annotated[
        frozenset[str],
        Doc("What the token grants, read from the policy's `scope_claims`."),
    ] = frozenset()

    @property
    def is_authenticated(self) -> bool:
        """Always true: a verified token authenticates its caller."""
        return True

    @property
    def identity(self) -> str:
        """The subject, or an empty string for a token that names none."""
        return self.subject or ""

    @property
    def display_name(self) -> str:
        """The subject, which is what a token names its caller by."""
        return self.subject or ""


def _usable_for_signatures(
    jwk: Annotated[object, Doc("One entry from a JWKS document's `keys`.")],
) -> bool:
    """Whether a JWK is published for verifying signatures.

    A key says so with `use`, with `key_ops`, or by saying nothing at all,
    which RFC 7517 leaves open and every provider uses for a signing key.
    A key that names another purpose is not one a signature is checked with.

    A symmetric key is refused whatever it says about itself, because a JWKS
    is a public document and a shared secret in one is not a secret.

    An entry that is not a JSON object, or names its `kty` with anything but
    a string, is not a key. The document comes from outside this process, so
    its shape is checked before anything is read from it.
    """
    if not isinstance(jwk, dict):
        return False
    key_type = jwk.get("kty")
    if not isinstance(key_type, str) or key_type not in _ASYMMETRIC_KEY_TYPES:
        return False
    use = jwk.get("use")
    if use is not None:
        return bool(use == "sig")
    operations = jwk.get("key_ops")
    if operations is None:
        return True
    # A provider serving this as a string rather than the array RFC 7517
    # asks for would otherwise match on a substring.
    return isinstance(operations, list) and "verify" in operations


def _claims_of(raw: dict[str, Any], scope_claims: tuple[str, ...]) -> JWTClaims:
    """Wrap a verified claim set, lifting the registered claims out of it."""
    get = raw.get
    return JWTClaims(
        claims=MappingProxyType(raw),
        subject=get("sub"),
        issuer=get("iss"),
        audience=get("aud"),
        expires_at=get("exp"),
        issued_at=get("iat"),
        token_id=get("jti"),
        scopes=_scopes_of(raw, scope_claims),
    )


def _scopes_of(
    raw: Mapping[str, Any], names: tuple[str, ...]
) -> frozenset[str]:
    """Return the scopes the first of `names` the token carries grants.

    A space-separated string or an array of strings. The first claim present
    decides, so a claim of the wrong shape grants nothing rather than passing
    the question on to the next one.
    """
    for name in names:
        value = raw.get(name)
        if value is None:
            continue
        if isinstance(value, str):
            return frozenset(value.split())
        if isinstance(value, list) and all(
            isinstance(item, str) for item in value
        ):
            return frozenset(value)
        return frozenset()
    return frozenset()


@dataclass(frozen=True, slots=True)
class _KeySet:
    """One loaded key set, and the tokens verified against it.

    Held together and replaced whole. A request reads the keys and the cache
    that belongs to them in one attribute read, so a token verified under a
    key the provider has since withdrawn never survives into the next set.

    The cache is a plain `dict` with its insertion order kept in a `deque`
    beside it. A verifier is shared across a thread pool, so nothing walks
    the `dict`: every operation on either is one the interpreter applies
    whole.
    """

    verify: Callable[[str], dict[str, Any]]
    cache: dict[str | bytes, tuple[float, JWTClaims]] = field(
        default_factory=dict
    )
    order: deque[str | bytes] = field(default_factory=deque)


class TokenVerifier(Protocol):
    """What every verifier in grelmicro answers.

    `JWTVerifier` satisfies it whether its keys are held in code or fetched
    from an endpoint, so a dependency written against this takes either, and
    a verifier of your own too.
    """

    def verify(
        self,
        token: Annotated[str, Doc("The encoded JWT, with no scheme prefix.")],
    ) -> JWTClaims:
        """Return the claims of `token`, or raise `TokenRejectedError`."""
        ...  # pragma: no cover

    def verify_header(
        self,
        header: Annotated[
            str | None, Doc("The `Authorization` header value, or `None`.")
        ],
    ) -> JWTClaims:
        """Return the claims of the bearer token in `header`."""
        ...  # pragma: no cover


_LIVE_FIELDS: Final = frozenset(
    {"cache_size", "retry_interval", "timeout", "ttl"}
)
"""Fields a mounted file may change while the service runs.

Each one tunes what a verification costs: how many tokens stay cached, and
how the key set is fetched. None of them decides whose tokens are trusted or
how long a withdrawn token keeps being accepted.
"""


class _Unset:
    """Stands for an audience the caller did not pass.

    `None` cannot: it is what `audience` means by "answer to no audience", so
    a caller writing it has said something, and `resolve_config` reads a
    `None` keyword as one nobody passed.
    """

    def __repr__(self) -> str:
        """Return the name it is published under."""
        return "UNSET"


_UNSET: Final = _Unset()
"""The one instance of `_Unset`, so a caller can be told apart from a default."""

_NO_AUDIENCE: Final = "\x00no-audience"
"""Stands in for the audience while the environment is read.

Code that answers to no audience says so with `None`, which the resolver
would otherwise read as "not given" and fill from a variable. The stand-in
keeps the variable out, and `None` replaces it once resolution is done.
"""

_ONE_OR_MANY: Final = ("audience", "issuer", "scope_claims")
"""Settings a keyword may pass as a single string."""


class JWTVerifier(Reconfigurable[JWTKeysConfig | JWKSConfig]):
    """Verifies inbound JWTs against a key set and a claim policy.

    Build one with a factory that names where the keys come from:
    `JWTVerifier.keys` for keys you hold, `JWTVerifier.jwks` for keys a
    provider publishes, or `JWTVerifier.from_config` for a config assembled
    elsewhere. There is no bare constructor, because no key source is a safe
    default.

    Keys are parsed once, when they are loaded. Verification runs in the
    compiled core with the GIL released, so a thread pool verifies in
    parallel. Keys a provider publishes are fetched by `refresh`, a
    coroutine, and never on the request path.

    `keys` and `jwks` also read the environment once `GREL_ENV_LOAD` is set,
    under `GREL_JWTVERIFIER_`, or `GREL_JWTVERIFIER_{NAME}_` for a named
    verifier, and a keyword always wins. The environment may say whose tokens
    to trust, such as the issuer or the audience. It never chooses the key
    source, the algorithm or the key material, and a mounted file changes
    only what a verification costs while the service runs.

    Example:
        ```python
        verifier = JWTVerifier.keys(
            JWTKey.pem(public_pem, algorithm="RS256"),
            audience="grelmicro-api",
            issuer="https://auth.example.com/",
        )
        claims = verifier.verify(token)
        ```
    """

    _IMMUTABLE_RECONFIGURE_FIELDS: ClassVar[frozenset[str]] = (
        frozenset(JWTKeysConfig.model_fields)
        | frozenset(JWKSConfig.model_fields)
    ) - _LIVE_FIELDS
    """Every field but the ones that tune what a verification costs.

    Read off the two configs rather than listed, so a field added to either
    one starts out fixed at startup instead of becoming live by omission.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        """Refuse construction: a verifier has no default key source."""
        msg = (
            "JWTVerifier has no default key source, so it cannot be built from"
            " a bare constructor. Use JWTVerifier.keys(key, ..., audience=...),"
            " JWTVerifier.jwks(url, audience=...) or"
            " JWTVerifier.from_config(config)."
        )
        raise TypeError(msg)

    @classmethod
    def keys(  # noqa: PLR0913
        cls,
        *keys: Annotated[
            JWTKey,
            Doc("Keys this verifier accepts, selected by the token's `kid`."),
        ],
        audience: Annotated[
            str | Sequence[str] | _Unset | None,
            Doc(
                "Accepted `aud` values. `None` answers to no audience, which"
                " refuses any token that names one, and only code can say it."
                " Left out, it is read from the environment, where it is"
                " required."
            ),
        ] = _UNSET,
        issuer: Annotated[
            str | Sequence[str] | None,
            Doc("Accepted `iss` values. Left out, nothing checks the issuer."),
        ] = None,
        leeway: Annotated[
            int | None,
            Doc("Seconds of clock skew allowed on `exp` and `nbf`."),
        ] = None,
        token_type: Annotated[
            Literal["at+jwt"] | None,
            Doc("Type every token must declare in its `typ` header."),
        ] = None,
        scope_claims: Annotated[
            str | Sequence[str] | None,
            Doc("Claims the granted scopes are read from, in order."),
        ] = None,
        required: Annotated[
            Sequence[str] | None,
            Doc("Further claims that must be present. `exp` always is."),
        ] = None,
        cache_size: Annotated[
            int | None,
            Doc("Verified tokens held in memory. Zero turns the cache off."),
        ] = None,
        cache_key: Annotated[
            Literal["sha256", "token"] | None,
            Doc("What the cache keys on: a digest, or the encoded token."),
        ] = None,
        cache_ttl: Annotated[
            float | None,
            Doc("Seconds a verified token stays cached."),
        ] = None,
        name: Annotated[
            str,
            Doc(
                "Instance name, which is the environment namespace:"
                " `GREL_JWTVERIFIER_{NAME}_`. The default instance reads"
                " `GREL_JWTVERIFIER_`."
            ),
        ] = "default",
        env_load: Annotated[
            bool | None,
            Doc(
                "Whether to read environment variables. `None` follows the"
                " process-wide `GREL_ENV_LOAD` flag."
            ),
        ] = None,
    ) -> Self:
        """Build a verifier from keys you hold, such as a PEM or a secret.

        A setting left out is read from the environment, then takes the
        default `JWTKeysConfig` gives it. Key material only ever comes from
        code.

        Raises:
            SettingsValidationError: If a key or a setting is refused.
        """
        config, env_prefix = cls._resolve(
            JWTKeysConfig,
            name,
            audience=audience,
            env_load=env_load,
            keys=list(keys),
            issuer=issuer,
            leeway=leeway,
            token_type=token_type,
            scope_claims=scope_claims,
            required=required,
            cache_size=cache_size,
            cache_key=cache_key,
            cache_ttl=cache_ttl,
        )
        instance = cls.from_config(config)
        instance._track_reconfigure(env_prefix)  # noqa: SLF001
        return instance

    @classmethod
    def jwks(  # noqa: PLR0913
        cls,
        url: Annotated[
            str | None,
            Doc(
                "The JWKS endpoint. Must be `https`. Left out, it is read from"
                " the environment."
            ),
        ] = None,
        *,
        audience: Annotated[
            str | Sequence[str] | _Unset | None,
            Doc(
                "Accepted `aud` values. `None` answers to no audience, which"
                " refuses any token that names one, and only code can say it."
                " Left out, it is read from the environment, where it is"
                " required."
            ),
        ] = _UNSET,
        issuer: Annotated[
            str | Sequence[str] | None,
            Doc("Accepted `iss` values. Left out, nothing checks the issuer."),
        ] = None,
        algorithm: Annotated[
            _AsymmetricAlgorithm | None,
            Doc(
                "Algorithm to pin for keys that publish none, as Entra ID"
                " does. Only code chooses it."
            ),
        ] = None,
        ttl: Annotated[
            float | None,
            Doc("Seconds a fetched document is treated as current."),
        ] = None,
        retry_interval: Annotated[
            float | None,
            Doc("Least time between two fetches."),
        ] = None,
        timeout: Annotated[
            float | None,
            Doc("Seconds to wait for the endpoint before giving up."),
        ] = None,
        max_bytes: Annotated[
            int | None,
            Doc("Largest document accepted."),
        ] = None,
        max_keys: Annotated[
            int | None,
            Doc("Most keys accepted from one document."),
        ] = None,
        leeway: Annotated[
            int | None,
            Doc("Seconds of clock skew allowed on `exp` and `nbf`."),
        ] = None,
        token_type: Annotated[
            Literal["at+jwt"] | None,
            Doc("Type every token must declare in its `typ` header."),
        ] = None,
        scope_claims: Annotated[
            str | Sequence[str] | None,
            Doc("Claims the granted scopes are read from, in order."),
        ] = None,
        required: Annotated[
            Sequence[str] | None,
            Doc("Further claims that must be present. `exp` always is."),
        ] = None,
        cache_size: Annotated[
            int | None,
            Doc("Verified tokens held in memory. Zero turns the cache off."),
        ] = None,
        cache_key: Annotated[
            Literal["sha256", "token"] | None,
            Doc("What the cache keys on: a digest, or the encoded token."),
        ] = None,
        cache_ttl: Annotated[
            float | None,
            Doc("Seconds a verified token stays cached."),
        ] = None,
        fetch: Annotated[
            Fetcher | None,
            Doc("Fetcher to use. Defaults to one built on `httpx`."),
        ] = None,
        name: Annotated[
            str,
            Doc(
                "Instance name, which is the environment namespace:"
                " `GREL_JWTVERIFIER_{NAME}_`. The default instance reads"
                " `GREL_JWTVERIFIER_`."
            ),
        ] = "default",
        env_load: Annotated[
            bool | None,
            Doc(
                "Whether to read environment variables. `None` follows the"
                " process-wide `GREL_ENV_LOAD` flag."
            ),
        ] = None,
    ) -> Self:
        """Build a verifier from keys a provider publishes at a JWKS endpoint.

        Nothing is fetched here. Open the verifier with `async with`, or
        await `refresh`, before the first request. A setting left out is read
        from the environment, then takes the default `JWKSConfig` gives it.

        Raises:
            SettingsValidationError: If a setting is refused, or a variable
                names the algorithm.
        """
        config, env_prefix = cls._resolve(
            JWKSConfig,
            name,
            audience=audience,
            env_load=env_load,
            url=url,
            issuer=issuer,
            algorithm=algorithm,
            ttl=ttl,
            retry_interval=retry_interval,
            timeout=timeout,
            max_bytes=max_bytes,
            max_keys=max_keys,
            leeway=leeway,
            token_type=token_type,
            scope_claims=scope_claims,
            required=required,
            cache_size=cache_size,
            cache_key=cache_key,
            cache_ttl=cache_ttl,
        )
        instance = cls.from_config(config, fetch=fetch)
        instance._track_reconfigure(env_prefix)  # noqa: SLF001
        return instance

    @staticmethod
    def _resolve[C: (JWTKeysConfig, JWKSConfig)](
        config_cls: type[C],
        name: str,
        *,
        audience: str | Sequence[str] | _Unset | None,
        env_load: bool | None,
        **settings: object,
    ) -> tuple[C, str]:
        """Resolve a config from keywords and this verifier's own variables.

        Only the instance prefix is read, never the kind-wide one, so a
        variable meant for one verifier never retunes another.

        A single string passed in code is one value, never split on commas.
        `audience=None` is applied once the variables are read, so no
        variable can turn the audience check off. A variable naming the
        algorithm is refused rather than applied.

        Raises:
            SettingsValidationError: If a value is refused, or a variable
                names a setting only code chooses.
        """
        env_prefix, _ = env_prefixes("JWTVERIFIER", name)
        kwargs: dict[str, object] = dict(settings)
        if audience is None:
            kwargs["audience"] = [_NO_AUDIENCE]
        elif not isinstance(audience, _Unset):
            kwargs["audience"] = audience
        for field_name in _ONE_OR_MANY:
            value = kwargs.get(field_name)
            if isinstance(value, str):
                kwargs[field_name] = [value]
        config = resolve_config(
            config_cls,
            explicit=None,
            kwargs=kwargs,
            env_prefix=env_prefix,
            env_load=env_load,
        )
        if (
            isinstance(config, JWKSConfig)
            and settings.get("algorithm") is None
            and config.algorithm is not None
        ):
            msg = (
                f"{env_prefix}ALGORITHM names a signature algorithm, which only"
                " code chooses. Pass algorithm= instead."
            )
            raise SettingsValidationError(msg)
        if audience is None:
            config = config.model_copy(update={"audience": None})
        return config, env_prefix

    @classmethod
    def from_config(
        cls,
        config: Annotated[
            JWTKeysConfig | JWKSConfig,
            Doc(
                "The keys, or where they are published, and the claim policy"
                " to enforce, taken as they are."
            ),
        ],
        *,
        fetch: Annotated[
            Fetcher | None,
            Doc(
                "Fetcher for a `JWKSConfig`. Defaults to one built on `httpx`,"
                " and is refused for keys held in code."
            ),
        ] = None,
    ) -> Self:
        """Build a verifier from a configuration that is already whole.

        The one declarative door. What you pass is what runs: no environment
        variable is read, and the verifier is not registered for live reload.

        Raises:
            SettingsValidationError: If the core cannot read a key.
            TypeError: If `fetch` is given for keys held in code.
        """
        instance = cls.__new__(cls)
        instance._setup(config, fetch=fetch)  # noqa: SLF001
        return instance

    def _setup(
        self,
        config: JWTKeysConfig | JWKSConfig,
        *,
        fetch: Fetcher | None,
    ) -> None:
        """Hold the policy, and load the keys when they are held in code."""
        if fetch is not None and not isinstance(config, JWKSConfig):
            msg = (
                "fetch= applies to keys published at an endpoint. These keys"
                " are held in code, so there is nothing to fetch."
            )
            raise TypeError(msg)
        self._config = config
        self._reconfigure_lock = asyncio.Lock()
        compiled = _core()
        self._compiled = compiled
        self._error = compiled.CoreVerificationError
        self._digest = (
            compiled.sha256_digest if config.cache_key == "sha256" else None
        )
        self._cache_size = config.cache_size
        self._cache_ttl = config.cache_ttl
        self._leeway = config.leeway
        self._scope_claims = tuple(config.scope_claims)
        self._fetch: Fetcher = fetch or fetch_with_httpx
        self._document: bytes | None = None
        self._loaded_at: float | None = None
        self._attempted_at: float | None = None
        self._wants_keys = False
        self._inflight: asyncio.Task[bool] | None = None
        self._task: asyncio.Task[None] | None = None
        if isinstance(config, JWKSConfig):
            self._source: JWKSConfig | None = config
            self._keys: _KeySet | None = None
            return
        self._source = None
        self._keys = self._key_set(config)

    def _key_set(self, config: JWTKeysConfig) -> _KeySet:
        """Parse every key in `config` into a key set with an empty cache."""
        try:
            verify = self._compiled.Verifier(
                [
                    (
                        key.kid,
                        key.algorithm,
                        key.key.get_secret_value(),
                        # The core reads a secret and a PEM through the same
                        # door, telling them apart by the algorithm.
                        "jwk" if key.format == "jwk" else "pem",
                    )
                    for key in config.keys
                ],
                audience=config.audience or None,
                issuer=config.issuer or None,
                leeway=config.leeway,
                required=config.enforced_claims(),
                token_type=config.token_type,
            ).verify
        except ValueError as error:
            # The core names the failure without quoting the key, which is
            # what makes this safe to render and to log.
            detail = f"keys: {error}"
            raise SettingsValidationError(detail) from None
        return _KeySet(verify)

    @property
    def ready(self) -> bool:
        """Whether a key set is loaded. Always true for keys held in code."""
        return self._keys is not None

    @property
    def stale(self) -> bool:
        """Whether the next `refresh` would fetch.

        Never true for keys held in code. For keys a provider publishes, true
        before the first load, once `ttl` has passed, and once a token named
        a key the current set does not hold.
        """
        source = self._source
        if source is None:
            return False
        if self._keys is None or self._wants_keys:
            return True
        return (
            self._loaded_at is None
            or monotonic() - self._loaded_at >= source.ttl
        )

    async def refresh(
        self,
        *,
        force: Annotated[
            bool, Doc("Fetch even when the current keys are still fresh.")
        ] = False,
    ) -> bool:
        """Fetch the published key set, and return whether the keys changed.

        Does nothing for keys held in code. For keys a provider publishes, it
        does nothing while they are fresh and never fetches more often than
        `retry_interval`. A failure raises and leaves the loaded keys in
        place.

        One fetch runs at a time. A caller arriving while one runs waits for
        it and gets its answer, so a burst of tokens naming a new key costs
        the provider one request. That makes it safe to await from a request
        that was refused with `unknown-key`, and to verify again when it
        returns `True`:

        ```python
        try:
            claims = verifier.verify(token)
        except TokenRejectedError as error:
            if error.reason is not TokenRejectedReason.UNKNOWN_KEY or not await verifier.refresh():
                raise
            claims = verifier.verify(token)
        ```

        Raises:
            SigningKeysUnavailableError: If the document cannot be fetched,
                or holds no usable key.
        """
        source = self._source
        if source is None:
            return False
        inflight = self._inflight
        if inflight is not None:
            return await asyncio.shield(inflight)
        if not force and not self.stale:
            return False
        now = monotonic()
        if (
            not force
            and self._attempted_at is not None
            and now - self._attempted_at < source.retry_interval
        ):
            return False
        self._attempted_at = now
        task = asyncio.create_task(self._load(source))
        self._inflight = task
        task.add_done_callback(self._settled)
        # Shielded, so a request that gives up waiting does not cancel the
        # fetch every other caller is waiting on.
        return await asyncio.shield(task)

    def _settled(self, task: asyncio.Task[bool]) -> None:
        """Let the next refresh fetch again, once this one has finished.

        The outcome is read here, so a fetch that failed after every caller
        stopped waiting is not reported as an exception nobody retrieved.
        """
        self._inflight = None
        if not task.cancelled():
            task.exception()

    async def _load(self, source: JWKSConfig) -> bool:
        """Fetch the document, and swap the keys in when it changed."""
        try:
            document = await self._fetch(
                source.url, timeout=source.timeout, max_bytes=source.max_bytes
            )
        except (SigningKeysUnavailableError, DependencyNotFoundError):
            # A missing dependency is a broken install, not an outage, so it
            # stops the app at startup instead of being retried forever.
            raise
        except Exception as error:
            # A fetcher of the caller's own can fail in its own way. Left
            # unwrapped, that error would stop the app at startup and end the
            # background refresh for good, which only this one is handled for.
            msg = f"jwks endpoint could not be fetched: {type(error).__name__}"
            raise SigningKeysUnavailableError(msg) from error
        if document == self._document:
            # Same bytes, so the keys already loaded are the current ones.
            self._loaded_at = monotonic()
            self._wants_keys = False
            return False

        try:
            keys = self._key_set(_document_config(source, document))
        except SettingsValidationError as error:
            # A document can parse and still hold a key the core refuses.
            # `refresh` promises one error, so it raises that one.
            msg = f"jwks document holds no usable key: {error}"
            raise SigningKeysUnavailableError(msg) from None

        # One assignment, so a thread reading it gets the old keys with their
        # cache or the new keys with an empty one, and never a mix.
        self._keys = keys
        self._document = document
        # Marked current only now. Doing it when the fetch returned would
        # call a document that failed to build a successful refresh, so a
        # provider serving a broken key set would stop the next attempt for a
        # whole `ttl` and clear the rotation signal that asked for it.
        self._loaded_at = monotonic()
        self._wants_keys = False
        return True

    async def __aenter__(self) -> Self:
        """Load the published keys, then keep them fresh until exit.

        Keys held in code need nothing. A provider that cannot be reached
        does not stop the app: the verifier opens without keys, every
        verification raises `SigningKeysUnavailableError`, and the background
        refresh keeps trying every `retry_interval`.
        """
        source = self._source
        if source is None or self._task is not None:
            return self
        try:
            await self.refresh()
        except SigningKeysUnavailableError as error:
            logger.warning(
                "signing keys could not be loaded, retrying every %ss: %s",
                source.retry_interval,
                error,
            )
        self._task = asyncio.create_task(self._keep_fresh(source))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Stop refreshing, and abandon a fetch still running."""
        for task in (self._task, self._inflight):
            if task is not None:
                task.cancel()
                # Whatever an abandoned fetch failed with, closing still
                # succeeds: nothing is waiting on that result any more.
                with suppress(asyncio.CancelledError, Exception):
                    await task
        self._task = None
        self._inflight = None

    async def _keep_fresh(self, source: JWKSConfig) -> None:
        """Refresh every `retry_interval` until cancelled.

        `refresh` fetches only when the keys are stale, so a pass while they
        are fresh costs nothing, and a key the provider rotated in reaches
        the verifier within one interval of a token naming it. The interval
        is read again on every pass, so a reload paces the next one.
        """
        while True:
            source = self._source or source
            await asyncio.sleep(source.retry_interval)
            try:
                await self.refresh()
            except SigningKeysUnavailableError as error:
                logger.warning(
                    "signing keys could not be refreshed, keeping the loaded"
                    " keys: %s",
                    error,
                )
            except Exception:
                # Logged, never raised: an error nobody foresaw must not end
                # the refresh for good and leave the keys to go stale.
                logger.exception(
                    "signing keys could not be refreshed, keeping the loaded"
                    " keys"
                )

    async def _apply_reconfigure(
        self, new_config: JWTKeysConfig | JWKSConfig
    ) -> None:
        """Take a new cache size and refresh pacing.

        Nothing else changes while the service runs. A new audience, issuer
        or key is refused, so the config a verifier reports is always the
        one it enforces.

        Raises:
            ValueError: If `new_config` changes a setting that only applies
                at startup.
        """
        current = self._config
        changed = sorted(
            name
            for name in type(current).model_fields
            if name in self._IMMUTABLE_RECONFIGURE_FIELDS
            and getattr(new_config, name) != getattr(current, name)
        )
        if changed:
            msg = f"only applies at startup: {', '.join(changed)}"
            raise ValueError(msg)
        self._cache_size = new_config.cache_size
        if isinstance(new_config, JWKSConfig):
            self._source = new_config

    def verify(
        self,
        token: Annotated[str, Doc("The encoded JWT, with no scheme prefix.")],
    ) -> JWTClaims:
        """Return the claims of `token`, or raise `TokenRejectedError`.

        A token verified earlier in this process is answered from the cache
        until `cache_ttl` or its own `exp` passes, whichever comes first, so a
        cache hit is never staler than a full verification.

        Raises:
            TokenRejectedError: If the token does not verify.
            SigningKeysUnavailableError: If no key set has loaded yet.
        """
        keys = self._keys
        if keys is None:
            raise SigningKeysUnavailableError(_NOT_LOADED)
        key = self._key(token)
        cache = keys.cache
        cached = cache.get(key)
        if cached is not None:
            if cached[0] > time():
                return cached[1]
            cache.pop(key, None)
        try:
            raw = keys.verify(token)
        except self._error as error:
            reason = error.args[0]
            if reason == TokenRejectedReason.UNKNOWN_KEY:
                # The provider has probably rotated. Mark the set stale so the
                # next refresh fetches, rather than fetching here, which would
                # put the provider on the request path.
                self._wants_keys = True
            raise TokenRejectedError(reason) from None
        claims = _claims_of(raw, self._scope_claims)
        self._store(keys, key, claims)
        return claims

    def _key(self, token: str) -> str | bytes:
        """Return the cache key for `token`."""
        digest = self._digest
        return token if digest is None else digest(token)

    def verify_header(
        self,
        header: Annotated[
            str | None, Doc("The `Authorization` header value, or `None`.")
        ],
    ) -> JWTClaims:
        """Return the claims of the bearer token in `header`.

        The scheme is matched without regard to case, as RFC 7235 asks.

        Raises:
            TokenRejectedError: With `scheme` when `header` carries no bearer
                token, or with the reason the token itself failed.
        """
        if not header or header[:_BEARER_LENGTH].lower() != _BEARER_LOWER:
            raise TokenRejectedError(TokenRejectedReason.SCHEME)
        return self.verify(header[_BEARER_LENGTH:])

    def _store(
        self, keys: _KeySet, key: str | bytes, claims: JWTClaims
    ) -> None:
        """Cache `claims` until its deadline, making room when the cache is full.

        A token with no `exp` is never cached: nothing would bound how long
        the entry stays valid.

        Deadlines are written in insertion order, so the oldest key in the
        queue is the one most likely past its deadline. Making room takes
        from that end. The work lands here, never on a hit.
        """
        if not self._cache_size or claims.expires_at is None:
            return
        now = time()
        deadline = min(claims.expires_at + self._leeway, now + self._cache_ttl)
        if deadline <= now:
            return
        cache = keys.cache
        order = keys.order
        size = self._cache_size
        while order:
            try:
                oldest = order.popleft()
            except IndexError:
                break
            entry = cache.get(oldest)
            if entry is not None and entry[0] > now and len(cache) < size:
                # Still live, and there is room. Put it back and stop.
                order.appendleft(oldest)
                break
            cache.pop(oldest, None)
        cache[key] = (deadline, claims)
        order.append(key)


def _document_config(source: JWKSConfig, document: bytes) -> JWTKeysConfig:
    """Turn a fetched JWKS document into keys, refusing what it should."""
    try:
        parsed = json.loads(document)
    except ValueError:
        msg = "jwks document is not valid JSON"
        raise SigningKeysUnavailableError(msg) from None
    if not isinstance(parsed, dict):
        shape = "jwks document is not a JSON object"
        raise SigningKeysUnavailableError(shape)
    keys = parsed.get("keys")
    if not isinstance(keys, list) or not keys:
        msg = "jwks document carries no keys"
        raise SigningKeysUnavailableError(msg)
    if len(keys) > source.max_keys:
        msg = f"jwks document carries more than {source.max_keys} keys"
        raise SigningKeysUnavailableError(msg)
    policy = source.model_dump(exclude=set(_PUBLISHING_FIELDS))
    try:
        return JWTKeysConfig.from_jwks(
            parsed, algorithm=source.algorithm, **policy
        )
    except (SettingsValidationError, ValueError) as error:
        msg = f"jwks document holds no usable key: {error}"
        raise SigningKeysUnavailableError(msg) from None


def unverified_header(
    token: Annotated[str, Doc("The encoded JWT.")],
) -> dict[str, Any]:
    """Return the `alg` and `kid` of `token` without checking its signature.

    Nothing it returns is trustworthy. It routes a token to the right
    verifier, it never decides whether a token is valid, and it reads no key.

    Raises:
        TokenRejectedError: If `token` is not a well-formed JWS.
    """
    compiled = _core()
    try:
        header: dict[str, Any] = compiled.unverified_header(token)
    except compiled.CoreVerificationError as error:
        raise TokenRejectedError(error.args[0]) from None
    return header


def _core() -> Any:  # noqa: ANN401
    """Return the compiled verification core, or say how to install it."""
    try:
        # Imported here so the module loads without the core installed, and
        # the failure names the extra instead of breaking the import of
        # anything that reads `grelmicro.security`.
        import grelmicro_core  # noqa: PLC0415
    except ImportError:
        raise DependencyNotFoundError(module="grelmicro-core") from None
    return grelmicro_core
