"""JWT verification against keys published at a JWKS endpoint.

An OIDC provider serves its signing keys over HTTP and rotates them. Fetching
them is network I/O, and verifying a token is not, so the two are kept apart:
`refresh` is a coroutine you schedule, and `verify` stays the same synchronous
call it is with a static key.

Nothing fetches on the request path. A token naming a key the verifier does
not hold marks the key set stale and is refused, and the next refresh picks
the new keys up. Fetching inline would put network latency on every request
that presented an unknown `kid`, and would let anyone spray invented `kid`
values to make the service hammer its own identity provider.

Read more in the [JWT](../security/jwt.md) docs.
"""

from __future__ import annotations

import json
from importlib import import_module
from time import monotonic
from typing import TYPE_CHECKING, Annotated, Any, Final, Protocol

from pydantic import field_validator
from typing_extensions import Doc

from grelmicro.errors import (
    DependencyNotFoundError,
    GrelmicroError,
    OutOfContextError,
    SettingsValidationError,
)
from grelmicro.security.bans import ClientBannedError, ClientBans
from grelmicro.security.jwt import (
    JWTConfig,
    JWTPolicy,
    JWTVerifier,
    TokenRejectedError,
    _responsible,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from grelmicro.security.jwt import JWTClaims

__all__ = [
    "JWKSConfig",
    "JWKSFetcher",
    "JWKSUnavailableError",
    "JWKSVerifier",
]

_ONE_MIB: Final = 1_048_576

_NOT_LOADED: Final = (
    "No key set has been loaded. Await refresh() before verifying."
)


class JWKSUnavailableError(GrelmicroError, RuntimeError):
    """The key set could not be fetched or could not be read.

    Raised by `refresh`, never by `verify`. A refresh that fails leaves the
    keys already loaded in place, so a provider that goes down does not take
    authentication down with it until those keys expire on its side.
    """


class JWKSFetcher(Protocol):
    """Fetches a JWKS document.

    Supply your own to reuse a client that already carries your proxy
    settings, certificate authority, or mutual TLS identity, and to keep the
    request inside whatever tracing and retry policy that client has.
    """

    async def __call__(
        self,
        url: str,
        *,
        timeout: float,  # noqa: ASYNC109
        max_bytes: int,
    ) -> bytes:
        """Return the document body, refusing anything over `max_bytes`."""
        ...  # pragma: no cover


class JWKSConfig(JWTPolicy):
    """Where the keys come from, and the claim policy they enforce.

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
        str | None,
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


async def fetch_with_httpx(
    url: Annotated[str, Doc("The JWKS endpoint.")],
    *,
    # The client applies this per connect, read and write phase, which one
    # cancel scope around the call cannot express, so it stays an argument.
    timeout: Annotated[float, Doc("Seconds to wait for the endpoint.")],  # noqa: ASYNC109
    max_bytes: Annotated[int, Doc("Largest body accepted.")],
) -> bytes:
    """Fetch a JWKS document with `httpx`, the default fetcher.

    Either `httpx` or `httpx2` will do, whichever the application already
    has, because the ecosystem is split across the two lines.

    The body is read in chunks and abandoned the moment it passes
    `max_bytes`, rather than trusting the length the server declares.
    Redirects are not followed: a key set that answers from somewhere else is
    a key set from somewhere else.
    """
    httpx = _httpx()

    async with (
        httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client,
        client.stream("GET", url) as response,
    ):
        if response.status_code != httpx.codes.OK:
            answered = f"jwks endpoint answered {response.status_code}"
            raise JWKSUnavailableError(answered)
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > max_bytes:
                msg = f"jwks document is larger than {max_bytes} bytes"
                raise JWKSUnavailableError(msg)
        return bytes(body)


def _httpx() -> Any:  # noqa: ANN401
    """Return whichever httpx the application has installed.

    Both lines are accepted because the ecosystem is split across them.
    FastAPI's `standard` extra pulls `httpx<1.0`, and Starlette's `full`
    extra pulls both, so either one can be the client an application already
    has. This fetcher touches only what the two have in common.

    `httpx` is tried first because it is what FastAPI installs today. Once
    FastAPI moves to `httpx2` alone, the other name can go and this becomes
    a plain import again.
    """
    for name in ("httpx", "httpx2"):
        try:
            return import_module(name)
        except ImportError:
            continue
    raise DependencyNotFoundError(module="httpx")


class JWKSVerifier:
    """Verifies tokens against keys fetched from a JWKS endpoint.

    `refresh` is a coroutine, `verify` is not. Call `refresh` once before
    serving and then on a schedule, so no request ever waits on the provider.

    Example:
        ```python
        verifier = JWKSVerifier(
            JWKSConfig(
                url="https://auth.example.com/.well-known/jwks.json",
                audience=["my-api"],
                issuer=["https://auth.example.com/"],
            )
        )
        await verifier.refresh()

        claims = verifier.verify(token)
        ```
    """

    def __init__(
        self,
        config: Annotated[JWKSConfig, Doc("Endpoint and claim policy.")],
        *,
        fetch: Annotated[
            JWKSFetcher | None,
            Doc("Fetcher to use. Defaults to one built on `httpx`."),
        ] = None,
        bans: Annotated[
            ClientBans | None,
            Doc(
                "Opt in to refusing callers that keep presenting tokens that"
                " do not verify. Pass `client=` to every call once set."
            ),
        ] = None,
    ) -> None:
        """Initialize the verifier. Keys are loaded by the first `refresh`."""
        self._config = config
        self._bans = bans
        self._fetch: JWKSFetcher = fetch or fetch_with_httpx
        self._verifier: JWTVerifier | None = None
        self._document: bytes | None = None
        self._loaded_at: float | None = None
        self._attempted_at: float | None = None
        self._wants_keys = False

    @property
    def ready(self) -> bool:
        """Whether a key set has been loaded."""
        return self._verifier is not None

    @property
    def stale(self) -> bool:
        """Whether the next `refresh` would fetch.

        True before the first load, once `ttl` has passed, and once a token
        named a key the current set does not hold.
        """
        if self._verifier is None or self._wants_keys:
            return True
        return (
            self._loaded_at is None
            or monotonic() - self._loaded_at >= self._config.ttl
        )

    async def refresh(
        self,
        *,
        force: Annotated[
            bool, Doc("Fetch even when the current keys are still fresh.")
        ] = False,
    ) -> bool:
        """Fetch the key set, and return whether the keys changed.

        Does nothing when the current keys are fresh, and never fetches more
        often than `retry_interval`. A failure raises and leaves the loaded
        keys in place.
        """
        if not force and not self.stale:
            return False
        now = monotonic()
        if (
            not force
            and self._attempted_at is not None
            and now - self._attempted_at < self._config.retry_interval
        ):
            return False
        self._attempted_at = now

        document = await self._fetch(
            self._config.url,
            timeout=self._config.timeout,
            max_bytes=self._config.max_bytes,
        )
        self._loaded_at = monotonic()
        self._wants_keys = False
        if document == self._document:
            return False

        verifier = JWTVerifier(self._build(document))
        # One assignment, so a thread reading it gets the old keys or the new
        # ones and never a half-built verifier.
        self._verifier = verifier
        self._document = document
        return True

    def verify(
        self,
        token: Annotated[str, Doc("The encoded JWT, with no scheme prefix.")],
        *,
        client: Annotated[
            str | None, Doc("The address to hold responsible, when banning.")
        ] = None,
    ) -> JWTClaims:
        """Return the claims of `token`, or raise `TokenRejectedError`."""
        return self._guarded(self._loaded().verify, token, client)

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
        return self._guarded(self._loaded().verify_header, header, client)

    def _guarded(
        self,
        verify: Callable[[Any], JWTClaims],
        value: Any,  # noqa: ANN401
        client: str | None,
    ) -> JWTClaims:
        """Apply the ban table around a verification, when one is configured."""
        bans = self._bans
        if bans is None:
            return self._checked(verify, value)
        responsible = _responsible(client)
        if bans.banned(responsible):
            raise ClientBannedError
        try:
            return self._checked(verify, value)
        except TokenRejectedError as error:
            bans.record(responsible, error.reason)
            raise

    def unverified_header(
        self,
        token: Annotated[str, Doc("The encoded JWT.")],
    ) -> dict[str, Any]:
        """Return the `alg` and `kid` of `token` without checking its signature.

        Nothing it returns is trustworthy. It routes a token to the right key
        set, it never decides whether a token is valid.
        """
        return self._loaded().unverified_header(token)

    def _loaded(self) -> JWTVerifier:
        """Return the current verifier, or say that none has been fetched."""
        verifier = self._verifier
        if verifier is None:
            raise OutOfContextError(_NOT_LOADED)
        return verifier

    def _checked(
        self,
        verify: Callable[[Any], JWTClaims],
        value: Any,  # noqa: ANN401
    ) -> JWTClaims:
        """Verify, and read a rejection for a sign the provider rotated."""
        try:
            return verify(value)
        except TokenRejectedError as error:
            if error.reason == "unknown-key":
                # The provider has probably rotated. Mark the set stale so the
                # next scheduled refresh fetches, rather than fetching here,
                # which would put the provider on the request path.
                self._wants_keys = True
            raise

    def _build(self, document: bytes) -> JWTConfig:
        """Turn a fetched document into a config, refusing what it should."""
        try:
            parsed = json.loads(document)
        except ValueError:
            msg = "jwks document is not valid JSON"
            raise JWKSUnavailableError(msg) from None
        if not isinstance(parsed, dict):
            shape = "jwks document is not a JSON object"
            raise JWKSUnavailableError(shape)
        keys = parsed.get("keys")
        if not isinstance(keys, list) or not keys:
            msg = "jwks document carries no keys"
            raise JWKSUnavailableError(msg)
        if len(keys) > self._config.max_keys:
            msg = (
                f"jwks document carries more than {self._config.max_keys} keys"
            )
            raise JWKSUnavailableError(msg)

        policy = self._config.model_dump(
            exclude={
                "url",
                "ttl",
                "retry_interval",
                "timeout",
                "max_bytes",
                "max_keys",
                "algorithm",
            }
        )
        try:
            return JWTConfig.from_jwks(
                parsed, algorithm=self._config.algorithm, **policy
            )
        except (SettingsValidationError, ValueError) as error:
            msg = f"jwks document holds no usable key: {error}"
            raise JWKSUnavailableError(msg) from None
