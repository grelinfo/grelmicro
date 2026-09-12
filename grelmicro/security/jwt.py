"""JWT verification for inbound requests.

grelmicro checks the token a caller presents, it never issues one.
`JWTVerifier` resolves its keys and claim policy once, then answers every
request from a compiled core: key selection, signature check, `exp`, `nbf`,
`aud` and `iss` checks and claim decoding happen in one call.

Read more in the [JWT](../security/jwt.md) docs.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from time import time
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Any, Final, Protocol

from pydantic import BaseModel, Field, field_validator
from typing_extensions import Doc

from grelmicro.errors import (
    DependencyNotFoundError,
    GrelmicroError,
    SettingsValidationError,
)
from grelmicro.security.bans import (
    ClientBannedError,
    ClientBans,
    _responsible_client,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "ALGORITHMS",
    "JWTClaims",
    "JWTConfig",
    "JWTKey",
    "JWTPolicy",
    "JWTVerifier",
    "TokenRejectedError",
    "TokenVerifier",
]

_JWK_ALGORITHM: Final[Mapping[tuple[str, str], str]] = {
    ("RSA", ""): "RS256",
    ("EC", "P-256"): "ES256",
    ("EC", "P-384"): "ES384",
    ("OKP", "Ed25519"): "EdDSA",
    ("oct", ""): "HS256",
}
"""Algorithm assumed for a JWK that names none, by key type and curve.

Entra ID publishes signing keys without an `alg`. The verifier pins one
algorithm per key either way, so a token asking for a different one is
refused whether the algorithm was published or inferred.
"""

ALGORITHMS: Final = frozenset(
    {
        "EdDSA",
        "ES256",
        "ES384",
        "HS256",
        "HS384",
        "HS512",
        "PS256",
        "PS384",
        "PS512",
        "RS256",
        "RS384",
        "RS512",
    }
)
"""Signature algorithms a verifier accepts. `none` is not one of them."""

BEARER_PREFIX: Final = "Bearer "
"""Scheme an `Authorization` header must carry, per RFC 6750."""

_REASONS: Final[Mapping[str, str]] = {
    "algorithm": "The token uses an algorithm this verifier does not accept.",
    "audience": "The token was issued for another audience.",
    "expired": "The token has expired.",
    "invalid": "The token is not valid.",
    "malformed": "The token is not a well-formed JWS.",
    "missing-claim": "The token is missing a required claim.",
    "not-yet-valid": "The token is not valid yet.",
    "scheme": "The Authorization header does not carry a bearer token.",
    "signature": "The signature does not match the key.",
    "subject": "The token names another subject.",
    "issuer": "The token was issued by another issuer.",
    "unknown-key": "No configured key matches the token.",
}


class TokenRejectedError(GrelmicroError, ValueError):
    """A token failed verification.

    `reason` is a stable tag such as `expired` or `signature`, so a caller
    branches on it rather than on message text. Neither the tag nor the
    message quotes the token: it is a live credential and the message reaches
    logs and error responses.
    """

    def __init__(
        self,
        reason: Annotated[str, Doc("Stable tag naming what failed.")],
    ) -> None:
        """Initialize the error."""
        self.reason = reason
        super().__init__(_REASONS.get(reason, _REASONS["invalid"]))


class JWTKey(BaseModel):
    """One verification key and the algorithm it verifies."""

    algorithm: Annotated[
        str,
        Doc("Signature algorithm this key verifies, such as `RS256`."),
    ]
    key: Annotated[
        bytes,
        Doc("PEM public key, or the shared secret for the `HS*` family."),
    ]
    kid: Annotated[
        str | None,
        Doc("Key id this key answers to. `None` serves tokens with no `kid`."),
    ] = None
    format: Annotated[
        str,
        Doc("`pem` for PEM or an `HS*` secret, `jwk` for a JSON Web Key."),
    ] = "pem"

    @classmethod
    def from_jwk(
        cls,
        jwk: Annotated[Mapping[str, Any], Doc("One key from a JWKS document.")],
        *,
        algorithm: Annotated[
            str | None,
            Doc("Algorithm to pin when the JWK names none."),
        ] = None,
    ) -> JWTKey:
        """Build a key from one JWK, as published at a JWKS endpoint.

        The `kid` and `alg` are read from the JWK. A provider that publishes
        no `alg`, as Entra ID does, needs one here or gets the algorithm its
        key type implies.
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
            algorithm=str(named),
            key=json.dumps(dict(jwk)).encode(),
            kid=jwk.get("kid"),
            format="jwk",
        )

    @field_validator("format")
    @classmethod
    def _check_format(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse a key format the core cannot read."""
        if value not in {"jwk", "pem"}:
            msg = "format must be 'pem' or 'jwk'"
            raise ValueError(msg)
        return value

    @field_validator("algorithm")
    @classmethod
    def _check_algorithm(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse an algorithm the core cannot verify."""
        if value not in ALGORITHMS:
            msg = "algorithm is not one of the accepted signature algorithms"
            raise ValueError(msg)
        return value


class JWTPolicy(BaseModel):
    """What a verifier checks, and what it remembers.

    Everything here is independent of where the keys came from, so a verifier
    built from a PEM and one built from a JWKS endpoint enforce it the same
    way.
    """

    audience: Annotated[
        list[str],
        Doc(
            "Accepted `aud` values. Leaving this empty says the service"
            " identifies with no audience, and RFC 7519 then requires"
            " refusing any token that carries an `aud` claim, so set it"
            " whenever your tokens have one. A token that carries no `aud`"
            " passes this check: add `aud` to `required` to insist on one."
        ),
    ] = Field(default_factory=list)
    issuer: Annotated[
        list[str],
        Doc("Accepted `iss` values. Empty leaves the issuer unchecked."),
    ] = Field(default_factory=list)
    leeway: Annotated[
        int,
        Doc("Seconds of clock skew allowed on `exp` and `nbf`."),
    ] = 0
    required: Annotated[
        list[str],
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
            "What the cache holds as its key. `token` keeps the encoded"
            " token, which is the fastest and what an in-process cache"
            " normally does. `sha256` keeps a SHA-256 digest instead, so a"
            " live bearer token is not held in memory for the lifetime of"
            " the entry. The digest is computed in the core, so it costs"
            " around 80 ns on a hit, under 1% of a verification. `token`"
            " keeps the encoded token as the key, which is what an"
            " in-process cache normally does and is the faster of the two."
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
        to the tokens most worth checking. Leave `audience` empty for a
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


class JWTConfig(JWTPolicy):
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
            str | None, Doc("Algorithm to pin for keys that name none.")
        ] = None,
        **policy: Annotated[Any, Doc("Any other `JWTConfig` setting.")],  # noqa: ANN401
    ) -> JWTConfig:
        """Build a config from a JWKS document.

        Keys marked for encryption are skipped: a signature is never verified
        with one. Everything else in `policy` is passed through.

        Example:
            ```python
            jwks = httpx.get(f"{issuer}/.well-known/jwks.json").json()
            config = JWTConfig.from_jwks(
                jwks, audience=["my-api"], issuer=[issuer]
            )
            ```
        """
        keys = []
        for jwk in jwks.get("keys", []):
            if not _usable_for_signatures(jwk):
                continue
            try:
                keys.append(JWTKey.from_jwk(jwk, algorithm=algorithm))
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


@dataclass(frozen=True, slots=True)
class JWTClaims:
    """The claims of a verified token.

    `raw` holds every claim as it arrived. The named fields are the registered
    claims lifted out of it, so the common path needs no dict lookups.
    """

    raw: Annotated[
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

    @property
    def scopes(self) -> frozenset[str]:
        """The `scope` claim, split on whitespace."""
        scope = self.raw.get("scope")
        return (
            frozenset(scope.split()) if isinstance(scope, str) else frozenset()
        )


def _usable_for_signatures(
    jwk: Annotated[Mapping[str, Any], Doc("One key from a JWKS document.")],
) -> bool:
    """Whether a JWK is published for verifying signatures.

    A key says so with `use`, with `key_ops`, or by saying nothing at all,
    which RFC 7517 leaves open and every provider uses for a signing key.
    A key that names another purpose is not one a signature is checked with.
    """
    use = jwk.get("use")
    if use is not None:
        return bool(use == "sig")
    operations = jwk.get("key_ops")
    if operations is None:
        return True
    return "verify" in operations


def _claims_of(raw: dict[str, Any]) -> JWTClaims:
    """Wrap a verified claim set, lifting the registered claims out of it."""
    get = raw.get
    return JWTClaims(
        raw=MappingProxyType(raw),
        subject=get("sub"),
        issuer=get("iss"),
        audience=get("aud"),
        expires_at=get("exp"),
        issued_at=get("iat"),
        token_id=get("jti"),
    )


class TokenVerifier(Protocol):
    """What every verifier in grelmicro answers.

    `JWTVerifier` and `JWKSVerifier` both satisfy it, so a dependency can be
    written against this and take either. Where the keys came from is the
    verifier's business, not the endpoint's.
    """

    def verify(
        self,
        token: Annotated[str, Doc("The encoded JWT, with no scheme prefix.")],
        *,
        client: Annotated[
            str | None, Doc("The address to hold responsible, when banning.")
        ] = None,
    ) -> JWTClaims:
        """Return the claims of `token`, or raise `TokenRejectedError`."""
        ...  # pragma: no cover

    def verify_header(
        self,
        header: Annotated[
            str | None, Doc("The `Authorization` header value, or `None`.")
        ],
        *,
        client: Annotated[
            str | None, Doc("The address to hold responsible, when banning.")
        ] = None,
    ) -> JWTClaims:
        """Return the claims of the bearer token in `header`."""
        ...  # pragma: no cover

    def unverified_header(
        self,
        token: Annotated[str, Doc("The encoded JWT.")],
    ) -> dict[str, Any]:
        """Return the `alg` and `kid` of `token` without checking it."""
        ...  # pragma: no cover


class JWTVerifier:
    """Verifies inbound JWTs against a fixed key set and claim policy.

    Keys are parsed once at construction. Verification runs in the compiled
    core with the GIL released, so a thread pool verifies in parallel.

    Example:
        ```python
        verifier = JWTVerifier(
            JWTConfig(
                keys=[JWTKey(algorithm="RS256", key=public_pem)],
                audience=["grelmicro-api"],
                issuer=["https://auth.example.com/"],
            )
        )
        claims = verifier.verify(token)
        ```
    """

    def __init__(
        self,
        config: Annotated[JWTConfig, Doc("Keys and claim policy to enforce.")],
        *,
        bans: Annotated[
            ClientBans | None,
            Doc(
                "Opt in to refusing callers that keep presenting tokens that"
                " do not verify. Pass `client=` to every call once set, so"
                " the protection cannot be half wired."
            ),
        ] = None,
    ) -> None:
        """Initialize the verifier, parsing every key."""
        self._bans = bans
        core = _core()
        self._error = core.CoreVerificationError
        self._unverified_header = core.unverified_header
        try:
            self._verify = core.Verifier(
                [
                    (key.kid, key.algorithm, key.key, key.format)
                    for key in config.keys
                ],
                audience=config.audience or None,
                issuer=config.issuer or None,
                leeway=config.leeway,
                required=config.enforced_claims(),
            ).verify
        except ValueError as error:
            # The core names the failure without quoting the key, which is
            # what makes this safe to render and to log.
            detail = f"keys: {error}"
            raise SettingsValidationError(detail) from None
        self._cache: dict[str | bytes, tuple[float, JWTClaims]] = {}
        # Eviction order is kept beside the cache rather than inside it. A
        # `dict` cannot be iterated while another thread writes to it, and a
        # verifier is shared across a thread pool, so nothing here may walk
        # the cache. Every operation below is one the interpreter applies
        # whole: `get`, `pop` with a default, a single assignment, and the
        # deque's own append and popleft.
        self._order: deque[str | bytes] = deque()
        self._digest = (
            core.sha256_digest if config.cache_key == "sha256" else None
        )
        self._cache_size = config.cache_size
        self._cache_ttl = config.cache_ttl
        self._leeway = config.leeway

    def verify(
        self,
        token: Annotated[str, Doc("The encoded JWT, with no scheme prefix.")],
        *,
        client: Annotated[
            str | None,
            Doc(
                "The address to hold responsible. Required when `bans` is"
                " set, and it must be one the caller cannot choose: pass"
                " what `resolve_client_address` returned."
            ),
        ] = None,
    ) -> JWTClaims:
        """Return the claims of `token`, or raise `TokenRejectedError`.

        A token verified earlier in this process is answered from the cache
        until `cache_ttl` or its own `exp` passes, whichever comes first, so a
        cache hit is never staler than a full verification.

        With `bans` set, a caller already banned raises `ClientBannedError`
        before the token is looked at, and a rejection is counted against it.
        """
        bans = self._bans
        if bans is not None:
            client = _responsible_client(client)
            if bans.banned(client):
                raise ClientBannedError
            try:
                return self._verified(token)
            except TokenRejectedError as error:
                bans.record(client, error.reason)
                raise
        return self._verified(token)

    def _verified(self, token: str) -> JWTClaims:
        """Return the claims of `token`, with no ban bookkeeping."""
        key = self._key(token)
        cached = self._cache.get(key)
        if cached is not None:
            if cached[0] > time():
                return cached[1]
            self._cache.pop(key, None)
        try:
            raw = self._verify(token)
        except self._error as error:
            raise TokenRejectedError(error.args[0]) from None
        claims = _claims_of(raw)
        self._store(key, claims)
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
        *,
        client: Annotated[
            str | None, Doc("The address to hold responsible, when banning.")
        ] = None,
    ) -> JWTClaims:
        """Return the claims of the bearer token in `header`."""
        if not header or not header.startswith(BEARER_PREFIX):
            reason = "scheme"
            raise TokenRejectedError(reason)
        return self.verify(header[len(BEARER_PREFIX) :], client=client)

    def unverified_header(
        self,
        token: Annotated[str, Doc("The encoded JWT.")],
    ) -> dict[str, Any]:
        """Return the `alg` and `kid` of `token` without checking its signature.

        Nothing it returns is trustworthy. It routes a token to the right key
        set, it never decides whether a token is valid.
        """
        try:
            header: dict[str, Any] = self._unverified_header(token)
        except self._error as error:
            raise TokenRejectedError(error.args[0]) from None
        return header

    def _store(self, key: str | bytes, claims: JWTClaims) -> None:
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
        cache = self._cache
        order = self._order
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
