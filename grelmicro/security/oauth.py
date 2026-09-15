"""Outbound tokens.

A service that calls another API authenticates the call. `OAuthClient`
registers the service with its authorization server once. `ClientCredentials`
gets a token for the service itself, and `TokenExchange` trades the token a
request presented for one issued to another API. Each caches its token,
refreshes it before it expires, and hands it to `httpx` through `auth()`.

grelmicro still issues no token. The authorization server does.

Read more in the [Outbound Tokens](../security/tokens.md) docs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import math
import random
import re
import secrets
import ssl
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from email.utils import parsedate_to_datetime
from functools import cache
from http import HTTPStatus
from importlib import import_module
from pathlib import Path
from time import monotonic, time
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    Final,
    Literal,
    NamedTuple,
    Self,
    cast,
)
from urllib.parse import quote_plus

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    SecretStr,
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
    OutOfContextError,
    SettingsValidationError,
)
from grelmicro.metrics import _emit
from grelmicro.security._events import encoded
from grelmicro.security._events import logger as security_events
from grelmicro.security.jwks import _httpx
from grelmicro.security.jwt import _core, _metadata_urls, _UnusableMetadataError
from grelmicro.security.principal import VerifiedToken

if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
        Hashable,
        Iterable,
        Iterator,
        Mapping,
        Sequence,
    )
    from os import PathLike
    from types import TracebackType

__all__ = [
    "AccessToken",
    "ClientAuth",
    "ClientCredentials",
    "ClientCredentialsConfig",
    "ClientRejectedError",
    "OAuthClient",
    "OAuthClientConfig",
    "TokenExchange",
    "TokenExchangeConfig",
    "TokenUnavailableError",
]

logger = logging.getLogger(__name__)

type SigningAlgorithm = Literal[
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "EdDSA",
]
"""The algorithms a private key signs a client assertion under."""

FETCHES: Final = "grelmicro.oauth_client.fetches"
"""Counter of token requests sent to the authorization server."""

FETCH_DURATION: Final = "grelmicro.oauth_client.fetch.duration"
"""Histogram of how long a token request took, in seconds."""

CLIENT_REJECTED: Final = "grelmicro.oauth_client.rejected"
"""The event name of a client the authorization server refused."""

_JWT_BEARER_ASSERTION: Final = (
    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
)
"""The client assertion type of a signed JWT, RFC 7523."""

_TOKEN_EXCHANGE_GRANT: Final = "urn:ietf:params:oauth:grant-type:token-exchange"  # noqa: S105
"""The grant type of RFC 8693 token exchange."""

_JWT_BEARER_GRANT: Final = "urn:ietf:params:oauth:grant-type:jwt-bearer"
"""The grant type the on-behalf-of grant sends the caller's token under."""

_ACCESS_TOKEN_TYPE: Final = "urn:ietf:params:oauth:token-type:access_token"  # noqa: S105
"""The RFC 8693 type of an access token, sent and requested."""

_ASSERTION_TYPE: Final = "client-authentication+jwt"
"""The `typ` of an assertion that names the issuer, RFC 7523bis."""

_ASSERTION_LIFETIME: Final = 60
"""Seconds a signed client assertion is valid for."""

_RETRY_AFTER_CEILING: Final = 3600.0
"""The longest `Retry-After` honoured, in seconds."""

_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
"""Methods a request refused with `invalid_token` is sent again for."""


_CLIENT_CREDENTIALS_CACHE: Final = 16
"""Clients one `ClientCredentials` holds a token for, one per client it used."""

_REMEMBERED_REFUSALS: Final = 4096
"""Refused tokens remembered at once, the oldest dropped first."""

_ERROR_CODE: Final = re.compile(r"[\x20\x21\x23-\x5b\x5d-\x7e]+")
"""The characters RFC 6749 allows in an `error` code."""

_DESCRIPTION_LIMIT: Final = 256
"""Characters kept of an `error_description`."""

_INVALID_TOKEN: Final = re.compile(
    r"(?i)(?:^|,)\s*bearer\s+"
    r'(?:[\w.~+/-]+\s*=\s*(?:"(?:[^"\\]|\\.)*"|[^,\s"]*)\s*,\s*)*'
    r'error\s*=\s*"?invalid_token"?\s*(?:,|$)'
)
"""A `WWW-Authenticate` value holding a Bearer challenge that refuses the token.

The challenge is matched where one starts, at the start of the value or after
a comma, and only its own parameters are read up to `error`. A challenge of
another scheme after it, such as `DPoP error="invalid_token"`, never counts.
"""

_SECRET: Final = "secret"  # noqa: S105
_PRIVATE_KEY: Final = "private_key"
_ASSERTION_FILE: Final = "assertion_file"


class TokenUnavailableError(GrelmicroError, RuntimeError):
    """No valid token could be had for an outbound call.

    Raised by `token()`, and by a request sent through `auth()`, once no
    cached token is left and fetching one failed or was refused. A failure is
    remembered for a while, so requests arriving in that time raise at once.

    `error` holds the code the authorization server sent, such as
    `invalid_grant`, or `None` when it sent none. `description` holds its
    description. It is kept here and never written to a log or to the
    message, because a server can echo input into it.
    """

    def __init__(
        self,
        message: str,
        *,
        error: str | None = None,
        description: str | None = None,
    ) -> None:
        """Hold the message, and what the authorization server sent."""
        super().__init__(message)
        self.error = error
        """The error code the authorization server sent, or `None`."""
        self.description = description
        """The server's description of the error, or `None`."""


class ClientRejectedError(TokenUnavailableError):
    """The authorization server refused the service itself.

    A wrong secret, a key it does not know, or a client not allowed this
    grant. Waiting does not fix it. It is also written on the
    `grelmicro.security.events` logger, because a client the server stops
    accepting is often a rotated or leaked credential.
    """


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A token issued for an outbound call.

    `auth()` is how it should reach a request. Read one with `token()` to
    send it through another client. Its `repr` never shows the token.
    """

    value: str = field(repr=False)
    """The token."""

    token_type: str
    """The scheme the token is sent under, such as `Bearer`."""

    expires_at: int
    """When the token expires, in seconds since the epoch."""

    scopes: frozenset[str] = frozenset()
    """What the server granted. Empty when the response names no scope."""


class ClientAuth:
    """How a service proves who it is to its authorization server.

    Build one with a factory: `ClientAuth.secret`, `ClientAuth.private_key`
    or `ClientAuth.assertion_file`. There is no bare constructor, because no
    method is a safe default. Code chooses the method, and the environment
    only ever supplies a secret or a path.

    Example:
        ```python
        OAuthClient.discover(
            "https://auth.example.com/",
            client_id="orders-api",
            client_auth=ClientAuth.secret(),
        )
        ```
    """

    __slots__ = (
        "_algorithm",
        "_audience",
        "_kid",
        "_kind",
        "_method",
        "_path",
        "_secret",
        "_signer",
        "_thumbprint",
    )

    _kind: str
    _secret: SecretStr | None
    _method: Literal["basic", "post"] | None
    _path: str | None
    _signer: Any
    _algorithm: str | None
    _kid: str | None
    _thumbprint: str | None
    _audience: Literal["issuer", "token_endpoint"]

    def __init__(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        """Refuse construction: a client has no default way to authenticate."""
        msg = (
            "ClientAuth has no default method, so it cannot be built from a"
            " bare constructor. Use ClientAuth.secret(),"
            " ClientAuth.private_key(key, algorithm=...) or"
            " ClientAuth.assertion_file()."
        )
        raise TypeError(msg)

    def _setup(
        self,
        kind: str,
        *,
        secret: SecretStr | None = None,
        method: Literal["basic", "post"] | None = None,
        path: str | None = None,
        signer: Any = None,  # noqa: ANN401
        algorithm: str | None = None,
        kid: str | None = None,
        thumbprint: str | None = None,
        audience: Literal["issuer", "token_endpoint"] = "issuer",
    ) -> None:
        """Hold the method and the settings it reads."""
        self._kind = kind
        self._secret = secret
        self._method = method
        self._path = path
        self._signer = signer
        self._algorithm = algorithm
        self._kid = kid
        self._thumbprint = thumbprint
        self._audience = audience

    @classmethod
    def secret(
        cls,
        secret: Annotated[
            str | SecretStr | None,
            Doc(
                "The client secret. Left out, it is read from"
                " `GREL_OAUTHCLIENT_CLIENT_SECRET`, or"
                " `GREL_OAUTHCLIENT_{NAME}_CLIENT_SECRET` for a named client."
            ),
        ] = None,
        *,
        method: Annotated[
            Literal["basic", "post"] | None,
            Doc(
                "Pins how the secret is sent: `basic` in"
                " `Authorization: Basic`, `post` in the request body. Left"
                " out, the server's metadata decides, and Basic is used when"
                " it lists both or says nothing."
            ),
        ] = None,
    ) -> Self:
        """Authenticate with a client secret.

        Raises:
            SettingsValidationError: If `method` is neither `basic` nor `post`.
        """
        if method not in (None, "basic", "post"):
            msg = "method must be 'basic' or 'post'"
            raise SettingsValidationError(msg)
        held = (
            secret
            if secret is None or isinstance(secret, SecretStr)
            else SecretStr(secret)
        )
        auth = object.__new__(cls)
        auth._setup(_SECRET, secret=held, method=method)  # noqa: SLF001
        return auth

    @classmethod
    def private_key(
        cls,
        key: Annotated[
            bytes | str,
            Doc(
                "The private key, as PEM. RSA keys are read from PKCS #8 or"
                " PKCS #1, EC keys from PKCS #8 or SEC 1, Ed25519 keys from"
                " PKCS #8. Only code passes it."
            ),
        ],
        *,
        algorithm: Annotated[
            SigningAlgorithm,
            Doc("The algorithm the assertion is signed under."),
        ],
        kid: Annotated[
            str | None,
            Doc("The key ID the server knows the key by, sent as `kid`."),
        ] = None,
        certificate: Annotated[
            bytes | str | None,
            Doc(
                "The certificate of the key, as PEM. Its SHA-256 thumbprint"
                " is sent as `x5t#S256`, which is how Microsoft Entra ID"
                " finds the key."
            ),
        ] = None,
        audience: Annotated[
            Literal["issuer", "token_endpoint"],
            Doc(
                "What the assertion names as its audience. `issuer`, the"
                " default, is what RFC 7523bis requires. `token_endpoint` is"
                " what Okta and Microsoft Entra ID still require."
            ),
        ] = "issuer",
    ) -> Self:
        """Authenticate with an assertion signed by a private key, RFC 7523.

        The key is read once, here, so a key that cannot sign under
        `algorithm` is refused at startup rather than on the first fetch. An
        encrypted key is refused. Every fetch signs a fresh assertion, valid
        for one minute, with a new `jti`.

        Raises:
            SettingsValidationError: If the key or the certificate cannot be
                read, or the key cannot sign under `algorithm`.
            DependencyNotFoundError: If `grelmicro[jwt]` is not installed.
        """
        if audience not in ("issuer", "token_endpoint"):
            msg = "audience must be 'issuer' or 'token_endpoint'"
            raise SettingsValidationError(msg)
        material = key.encode() if isinstance(key, str) else bytes(key)
        try:
            signer = _core().Signer(algorithm, material)
        except ValueError as error:
            raise SettingsValidationError(str(error)) from None
        thumbprint = None if certificate is None else _thumbprint(certificate)
        auth = object.__new__(cls)
        auth._setup(  # noqa: SLF001
            _PRIVATE_KEY,
            signer=signer,
            algorithm=algorithm,
            kid=kid,
            thumbprint=thumbprint,
            audience=audience,
        )
        return auth

    @classmethod
    def assertion_file(
        cls,
        path: Annotated[
            str | PathLike[str] | None,
            Doc(
                "The file holding the signed assertion, such as a projected"
                " service account token. Left out, it is read from"
                " `GREL_OAUTHCLIENT_ASSERTION_FILE`."
            ),
        ] = None,
    ) -> Self:
        """Authenticate with a signed assertion read from a file on every fetch.

        The file is read again for every token request, so an assertion the
        platform rotates in place is always current.
        """
        auth = object.__new__(cls)
        auth._setup(  # noqa: SLF001
            _ASSERTION_FILE, path=None if path is None else str(path)
        )
        return auth

    def __repr__(self) -> str:
        """Return the method, without a secret, a key or a path."""
        if self._kind == _SECRET:
            return f"ClientAuth.secret(method={self._method!r})"
        if self._kind == _PRIVATE_KEY:
            return (
                f"ClientAuth.private_key(algorithm={self._algorithm!r},"
                f" kid={self._kid!r}, audience={self._audience!r})"
            )
        return "ClientAuth.assertion_file()"


def _thumbprint(certificate: bytes | str) -> str:
    """Return the `x5t#S256` thumbprint of a PEM certificate.

    Raises:
        SettingsValidationError: If `certificate` is not a PEM certificate.
    """
    text = (
        certificate.decode("ascii", "replace")
        if isinstance(certificate, bytes)
        else certificate
    )
    try:
        der = ssl.PEM_cert_to_DER_cert(text.strip())
    except ValueError:
        msg = "certificate is not a PEM certificate"
        raise SettingsValidationError(msg) from None
    return _b64u(hashlib.sha256(der).digest())


class OAuthClientConfig(
    BaseModel, frozen=True, extra="forbid", hide_input_in_errors=True
):
    """Where the authorization server is, who the service is, and how tokens are kept."""

    client_id: Annotated[
        str, Doc("The client ID the authorization server knows the service by.")
    ]
    issuer: Annotated[
        str | None,
        Doc(
            "The issuer, exactly as its metadata names it. Must be `https`."
            " Discovery reads the token endpoint from its metadata, and a"
            " signed assertion names it as its audience."
        ),
    ] = None
    token_endpoint: Annotated[
        str | None,
        Doc(
            "The token endpoint, for a server that publishes no metadata. Must"
            " be `https`."
        ),
    ] = None
    client_secret: Annotated[
        SecretStr | None, Doc("The client secret, for `ClientAuth.secret`.")
    ] = None
    assertion_file: Annotated[
        str | None,
        Doc("The assertion file, for `ClientAuth.assertion_file`."),
    ] = None
    refresh_before: Annotated[
        float,
        Doc(
            "Seconds before expiry a token is refreshed, capped at half its"
            " lifetime."
        ),
    ] = 60.0
    default_lifetime: Annotated[
        float,
        Doc("Seconds a token lives when its response omits `expires_in`."),
    ] = 300.0
    timeout: Annotated[
        float, Doc("Seconds to wait for the authorization server.")
    ] = 5.0
    retry_interval: Annotated[
        float, Doc("Seconds a failed token request is remembered for.")
    ] = 5.0
    max_bytes: Annotated[
        int, Doc("Largest response accepted from the authorization server.")
    ] = 1 << 20

    @field_validator("issuer", "token_endpoint")
    @classmethod
    def _check_https(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse a URL that is not `https`."""
        if value is not None and not value.startswith("https://"):
            msg = "must be an https URL"
            raise ValueError(msg)
        return value

    @field_validator("client_id")
    @classmethod
    def _check_client_id(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse an empty client ID."""
        if not value:
            msg = "client_id must not be empty"
            raise ValueError(msg)
        return value

    @field_validator(
        "refresh_before", "default_lifetime", "timeout", "max_bytes"
    )
    @classmethod
    def _check_positive(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse a value that is not above zero."""
        if value <= 0:
            msg = "value must be above zero"
            raise ValueError(msg)
        return value

    @field_validator("retry_interval")
    @classmethod
    def _check_not_negative(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse a negative interval."""
        if value < 0:
            msg = "value must not be negative"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _check_server(self) -> Self:
        """Refuse a client that names neither an issuer nor a token endpoint."""
        if self.issuer is None and self.token_endpoint is None:
            msg = "name an issuer or a token endpoint"
            raise ValueError(msg)
        return self


class _TargetConfig(BaseModel, frozen=True, extra="forbid"):
    """The API a token is requested for, named the way the provider expects."""

    audience: Annotated[
        str | None,
        Doc("Sent as `audience`, as Auth0, Keycloak and Okta expect."),
    ] = None
    resource: Annotated[
        str | None,
        Doc("Sent as `resource`, the RFC 8707 resource indicator."),
    ] = None
    scopes: Annotated[
        list[str],
        NoDecode,
        BeforeValidator(parse_csv_or_json),
        Doc("Sent as `scope`, as Microsoft Entra ID and AWS Cognito expect."),
    ] = Field(default_factory=list)


class ClientCredentialsConfig(_TargetConfig):
    """The API a `ClientCredentials` requests its token for."""


class TokenExchangeConfig(_TargetConfig):
    """The API a `TokenExchange` exchanges for, and how many tokens it keeps."""

    cache_size: Annotated[
        int,
        Doc("Exchanged tokens held in memory. Zero turns the cache off."),
    ] = 1024

    @field_validator("cache_size")
    @classmethod
    def _check_cache_size(cls, value: Any) -> Any:  # noqa: ANN401
        """Refuse a negative size."""
        if value < 0:
            msg = "cache_size must not be negative"
            raise ValueError(msg)
        return value


_LIVE_FIELDS: Final = frozenset(
    {
        "client_secret",
        "assertion_file",
        "refresh_before",
        "default_lifetime",
        "timeout",
        "retry_interval",
    }
)
"""Fields a mounted file may change while the service runs."""


class _Request(NamedTuple):
    """A token request, before the client authenticates it."""

    grant: str
    """The grant, as metrics name it."""
    form: dict[str, str]
    """The form fields the grant sends."""
    cacheable: bool
    """Whether the token it returns may be cached."""


class _Entry(NamedTuple):
    """A cached token and the monotonic times that bound it."""

    token: AccessToken
    refresh_at: float
    expires: float


class _Failure(Exception):  # noqa: N818
    """A failed token request, and how long and for whom it is remembered.

    Raised and caught inside the client. Callers see `error`.
    """

    def __init__(
        self,
        error: TokenUnavailableError,
        *,
        scope: Literal["client", "grant", "token"],
        seconds: float,
        outcome: str,
    ) -> None:
        """Hold the error, and whether the client, a grant or a token is refused."""
        super().__init__(str(error))
        self.error = error
        self.scope = scope
        self.seconds = seconds
        self.outcome = outcome


class _Oversized(Exception):  # noqa: N818
    """A response larger than the client accepts."""


class _TokenCache:
    """Tokens and refusals, one entry per key, bounded in size.

    Entries are evicted oldest first, after draining the ones already
    expired, so a cache of rotating tokens keeps the ones still in use.
    """

    __slots__ = ("_entries", "_refused", "_size")

    def __init__(self, size: int) -> None:
        """Start empty, holding at most `size` tokens."""
        self._size = size
        self._entries: dict[Hashable, _Entry] = {}
        self._refused: dict[Hashable, tuple[float, TokenUnavailableError]] = {}

    def get(self, key: Hashable, now: float) -> _Entry | None:
        """Return the token for `key` while it has not expired."""
        entry = self._entries.get(key)
        if entry is None or now >= entry.expires:
            return None
        return entry

    def put(self, key: Hashable, entry: _Entry, now: float) -> None:
        """Hold `entry` for `key`, evicting what the size requires."""
        if self._size <= 0:
            return
        entries = self._entries
        entries.pop(key, None)
        # A dict keeps insertion order, so the first key is the oldest entry.
        while entries:
            oldest = next(iter(entries))
            if len(entries) < self._size and now < entries[oldest].expires:
                break
            del entries[oldest]
        entries[key] = entry
        self._refused.pop(key, None)

    def drop(self, key: Hashable, value: str) -> None:
        """Drop the token for `key`, but only while it is still `value`."""
        entry = self._entries.get(key)
        if entry is not None and entry.token.value == value:
            self._entries.pop(key, None)

    def refused(
        self, key: Hashable, now: float
    ) -> TokenUnavailableError | None:
        """Return the refusal remembered for `key`, while it lasts."""
        remembered = self._refused.get(key)
        if remembered is None:
            return None
        if now >= remembered[0]:
            del self._refused[key]
            return None
        return remembered[1]

    def refuse(
        self, key: Hashable, until: float, error: TokenUnavailableError
    ) -> None:
        """Remember that `key` was refused, until `until`."""
        refused = self._refused
        refused.pop(key, None)
        while len(refused) >= _REMEMBERED_REFUSALS:
            refused.pop(next(iter(refused)))
        refused[key] = (until, error)


class OAuthClient(Reconfigurable[OAuthClientConfig]):
    """A service registered with its authorization server, for outbound tokens.

    Build one with a factory: `OAuthClient.discover` for an issuer that
    publishes its metadata, `OAuthClient.endpoint` for a token endpoint, or
    `OAuthClient.from_config`. Register it in `Grelmicro(uses=[...])`, and
    `ClientCredentials` and `TokenExchange` find it through the app, the way
    `Lock` finds its backend.

    `discover` and `endpoint` also read the environment once `GREL_ENV_LOAD`
    is set, under `GREL_OAUTHCLIENT_`, or `GREL_OAUTHCLIENT_{NAME}_` for a
    named client, and a keyword always wins. The environment supplies the
    issuer, the client ID, a secret or an assertion file path. It never
    chooses how the service authenticates.

    Example:
        ```python
        micro = Grelmicro(
            uses=[
                OAuthClient.discover(
                    "https://auth.example.com/",
                    client_id="orders-api",
                    client_auth=ClientAuth.secret(),
                ),
            ]
        )
        ```
    """

    kind: ClassVar[str] = "oauthclient"
    """The component kind patterns resolve the client under."""

    _IMMUTABLE_RECONFIGURE_FIELDS: ClassVar[frozenset[str]] = (
        frozenset(OAuthClientConfig.model_fields) - _LIVE_FIELDS
    )
    """Every field but a secret, a path and the refresh and fetch pacing."""

    def __init__(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        """Refuse construction: a client has no default authorization server."""
        msg = (
            "OAuthClient has no default authorization server, so it cannot be"
            " built from a bare constructor. Use OAuthClient.discover(issuer,"
            " client_id=..., client_auth=...), OAuthClient.endpoint(url, ...)"
            " or OAuthClient.from_config(config, client_auth=...)."
        )
        raise TypeError(msg)

    @classmethod
    def discover(
        cls,
        issuer: Annotated[
            str | None,
            Doc(
                "The issuer, exactly as its metadata names it. Must be"
                " `https`. Left out, it is read from the environment."
            ),
        ] = None,
        *,
        client_auth: Annotated[
            ClientAuth, Doc("How the service proves who it is.")
        ],
        client_id: Annotated[
            str | None,
            Doc(
                "The client ID. Left out, it is read from the environment,"
                " where it is required."
            ),
        ] = None,
        refresh_before: Annotated[
            float | None,
            Doc("Seconds before expiry a token is refreshed."),
        ] = None,
        default_lifetime: Annotated[
            float | None,
            Doc("Seconds a token lives when its response omits `expires_in`."),
        ] = None,
        timeout: Annotated[
            float | None, Doc("Seconds to wait for the authorization server.")
        ] = None,
        retry_interval: Annotated[
            float | None,
            Doc("Seconds a failed token request is remembered for."),
        ] = None,
        name: Annotated[
            str,
            Doc(
                "Registration name, and the environment namespace:"
                " `GREL_OAUTHCLIENT_{NAME}_`. The default client reads"
                " `GREL_OAUTHCLIENT_`."
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
        """Build a client from an issuer, reading its token endpoint from its metadata.

        Nothing is fetched here. The metadata is read when the client opens.
        The issuer's RFC 8414 metadata is read first, then its OpenID Connect
        discovery document, and the one that answers must name the issuer
        exactly and an `https` token endpoint.

        Raises:
            SettingsValidationError: If a setting is refused, no issuer is
                named, or the environment names what `client_auth` does not
                use.
        """
        config, env_prefix = cls._resolve(
            name,
            client_auth,
            env_load=env_load,
            issuer=issuer,
            client_id=client_id,
            refresh_before=refresh_before,
            default_lifetime=default_lifetime,
            timeout=timeout,
            retry_interval=retry_interval,
        )
        if config.issuer is None:
            msg = (
                f"discover needs an issuer. Pass it, or set {env_prefix}ISSUER."
            )
            raise SettingsValidationError(msg)
        if config.token_endpoint is not None:
            msg = (
                f"{env_prefix}TOKEN_ENDPOINT names a token endpoint, which"
                " discover reads from the issuer's metadata. Use"
                " OAuthClient.endpoint for a fixed one."
            )
            raise SettingsValidationError(msg)
        instance = cls._build(config, client_auth, name)
        instance._track_reconfigure(env_prefix)  # noqa: SLF001
        return instance

    @classmethod
    def endpoint(
        cls,
        token_endpoint: Annotated[
            str | None,
            Doc(
                "The token endpoint. Must be `https`. Left out, it is read"
                " from the environment."
            ),
        ] = None,
        *,
        client_auth: Annotated[
            ClientAuth, Doc("How the service proves who it is.")
        ],
        client_id: Annotated[
            str | None,
            Doc(
                "The client ID. Left out, it is read from the environment,"
                " where it is required."
            ),
        ] = None,
        issuer: Annotated[
            str | None,
            Doc(
                "The issuer, which a signed assertion names as its audience."
                " Only code passes it here."
            ),
        ] = None,
        refresh_before: Annotated[
            float | None,
            Doc("Seconds before expiry a token is refreshed."),
        ] = None,
        default_lifetime: Annotated[
            float | None,
            Doc("Seconds a token lives when its response omits `expires_in`."),
        ] = None,
        timeout: Annotated[
            float | None, Doc("Seconds to wait for the authorization server.")
        ] = None,
        retry_interval: Annotated[
            float | None,
            Doc("Seconds a failed token request is remembered for."),
        ] = None,
        name: Annotated[
            str,
            Doc(
                "Registration name, and the environment namespace:"
                " `GREL_OAUTHCLIENT_{NAME}_`."
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
        """Build a client from a token endpoint, for a server that publishes no metadata.

        With no metadata, `ClientAuth.secret()` sends the secret in
        `Authorization: Basic` unless `method=` says otherwise.

        Raises:
            SettingsValidationError: If a setting is refused, no endpoint is
                named, a variable names the issuer, or the environment names
                what `client_auth` does not use.
        """
        config, env_prefix = cls._resolve(
            name,
            client_auth,
            env_load=env_load,
            token_endpoint=token_endpoint,
            issuer=issuer,
            client_id=client_id,
            refresh_before=refresh_before,
            default_lifetime=default_lifetime,
            timeout=timeout,
            retry_interval=retry_interval,
        )
        if config.token_endpoint is None:
            msg = (
                "endpoint needs a token endpoint. Pass it, or set"
                f" {env_prefix}TOKEN_ENDPOINT."
            )
            raise SettingsValidationError(msg)
        if issuer is None and config.issuer is not None:
            msg = (
                f"{env_prefix}ISSUER is not read for a client built with"
                " endpoint. Pass issuer= in code."
            )
            raise SettingsValidationError(msg)
        instance = cls._build(config, client_auth, name)
        instance._track_reconfigure(env_prefix)  # noqa: SLF001
        return instance

    @classmethod
    def from_config(
        cls,
        config: Annotated[
            OAuthClientConfig,
            Doc(
                "The client, taken as it is. It discovers when it names an"
                " issuer and no token endpoint."
            ),
        ],
        *,
        client_auth: Annotated[
            ClientAuth, Doc("How the service proves who it is.")
        ],
        name: Annotated[str, Doc("Registration name.")] = "default",
    ) -> Self:
        """Build a client from a configuration that is already whole.

        What you pass is what runs: no environment variable is read, and the
        client is not registered for live reload.

        A secret or a path `client_auth` carries is used, and one set both
        there and in `config` is refused rather than one silently winning.

        Raises:
            SettingsValidationError: If `config` holds what `client_auth`
                does not use, lacks what it needs, or holds a secret or path
                `client_auth` carries too.
            TypeError: If `client_auth` is not a `ClientAuth`.
        """
        if not isinstance(client_auth, ClientAuth):
            msg = "client_auth= takes a ClientAuth, such as ClientAuth.secret()"
            raise TypeError(msg)
        config = _with_client_auth(config, client_auth)
        _check_auth(config, client_auth, prefix="")
        instance = cls.__new__(cls)
        instance._setup(config, client_auth, name)  # noqa: SLF001
        return instance

    @staticmethod
    def _resolve(
        name: str,
        client_auth: ClientAuth,
        *,
        env_load: bool | None,
        **settings: object,
    ) -> tuple[OAuthClientConfig, str]:
        """Resolve a config from keywords, `client_auth` and the environment.

        Raises:
            TypeError: If `client_auth` is not a `ClientAuth`.
            SettingsValidationError: If a value is refused, or the config
                does not suit `client_auth`.
        """
        if not isinstance(client_auth, ClientAuth):
            msg = "client_auth= takes a ClientAuth, such as ClientAuth.secret()"
            raise TypeError(msg)
        env_prefix, _ = env_prefixes("OAUTHCLIENT", name)
        kwargs: dict[str, object] = dict(settings)
        kwargs["client_secret"] = client_auth._secret  # noqa: SLF001
        kwargs["assertion_file"] = client_auth._path  # noqa: SLF001
        config = resolve_config(
            OAuthClientConfig,
            explicit=None,
            kwargs=kwargs,
            env_prefix=env_prefix,
            env_load=env_load,
        )
        _check_auth(config, client_auth, prefix=env_prefix)
        return config, env_prefix

    @classmethod
    def _build(
        cls, config: OAuthClientConfig, client_auth: ClientAuth, name: str
    ) -> Self:
        """Return a client holding `config`, not yet open."""
        instance = cls.__new__(cls)
        instance._setup(config, client_auth, name)  # noqa: SLF001
        return instance

    def _setup(
        self, config: OAuthClientConfig, client_auth: ClientAuth, name: str
    ) -> None:
        """Hold `config` and `client_auth`, with nothing fetched or open."""
        self._config = config
        self._auth = client_auth
        self._name = name
        self._identity = (
            config.issuer or "",
            config.token_endpoint or "",
            config.client_id,
        )
        self._reconfigure_lock = asyncio.Lock()
        self._httpx = None
        self._client = None
        self._loop = None
        self._token_endpoint = config.token_endpoint
        self._auth_methods = None
        self._discovery = None
        self._inflight = {}
        self._down_until = 0.0
        self._down = None
        self._refused_grants: dict[
            str, tuple[float, TokenUnavailableError]
        ] = {}

    _auth: ClientAuth
    _name: str
    _identity: tuple[str, str, str]
    _httpx: Any
    _client: Any
    _loop: asyncio.AbstractEventLoop | None
    _token_endpoint: str | None
    _auth_methods: frozenset[str] | None
    _discovery: asyncio.Task[str] | None
    _inflight: dict[Hashable, asyncio.Task[AccessToken]]
    _down_until: float
    _down: TokenUnavailableError | None

    @property
    def name(self) -> str:
        """The name the client is registered under."""
        return self._name

    @property
    def config(self) -> OAuthClientConfig:
        """The configuration in effect."""
        return self._config

    @property
    def token_endpoint(self) -> str | None:
        """The token endpoint, once known. `None` until discovery succeeds."""
        return self._token_endpoint

    def __repr__(self) -> str:
        """Return the name and client ID, without any credential."""
        return (
            f"OAuthClient(name={self._name!r},"
            f" client_id={self._config.client_id!r})"
        )

    async def __aenter__(self) -> Self:
        """Open the HTTP client, and read the issuer's metadata.

        A server that cannot be reached does not stop the app. The failure
        is remembered for `retry_interval`, and the first token request after
        that reads the metadata again.
        """
        if self._client is not None:
            return self
        module = _httpx()
        self._httpx = module
        self._client = module.AsyncClient(
            timeout=self._config.timeout, follow_redirects=False
        )
        self._loop = asyncio.get_running_loop()
        if self._token_endpoint is None:
            try:
                await self._endpoint()
            except _Failure as failure:
                self._remember_down(failure)
                logger.warning(
                    "issuer metadata could not be read, trying again on the"
                    " first token request: %s",
                    failure.error,
                )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Abandon every fetch still running, and close the HTTP client."""
        tasks: list[asyncio.Task[Any]] = list(self._inflight.values())
        if self._discovery is not None:
            tasks.append(self._discovery)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)
        self._inflight.clear()
        self._discovery = None
        client = self._client
        self._client = None
        self._loop = None
        if client is not None:
            await client.aclose()

    async def _apply_reconfigure(self, new_config: OAuthClientConfig) -> None:
        """Take a new secret, assertion path, and refresh and fetch pacing.

        Raises:
            ValueError: If `new_config` changes a setting that only applies
                at startup, or does not suit how the client authenticates.
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
        try:
            _check_auth(new_config, self._auth, prefix="")
        except SettingsValidationError as error:
            raise ValueError(str(error)) from None
        self._config = new_config

    async def _token(
        self,
        cache: _TokenCache,
        key: Hashable,
        request: _Request,
        *,
        not_after: int | None,
    ) -> AccessToken:
        """Return a valid token for `key`, from the cache or a shared fetch.

        Raises:
            OutOfContextError: If the client is not open.
            TokenUnavailableError: If no valid token is cached and the fetch
                fails, or a failure is still remembered.
        """
        loop = self._loop
        if self._client is None or loop is None:
            msg = (
                f"OAuthClient({self._name!r}) is not open. Register it in"
                " Grelmicro(uses=[...]), or open it with `async with`."
            )
            raise OutOfContextError(msg)
        if asyncio.get_running_loop() is not loop:
            future = asyncio.run_coroutine_threadsafe(
                self._token(cache, key, request, not_after=not_after), loop
            )
            return await asyncio.wrap_future(future)
        now = monotonic()
        flight = (id(cache), key)
        entry = cache.get(key, now)
        if entry is not None:
            if (
                now >= entry.refresh_at
                and flight not in self._inflight
                and self._failure(cache, key, request.grant, now) is None
            ):
                self._start(cache, key, request, not_after, background=True)
            return entry.token
        failure = self._failure(cache, key, request.grant, now)
        if failure is not None:
            raise _again(failure)
        task = self._inflight.get(flight)
        if task is None:
            task = self._start(cache, key, request, not_after, background=False)
        return await asyncio.shield(task)

    def _failure(
        self, cache: _TokenCache, key: Hashable, grant: str, now: float
    ) -> TokenUnavailableError | None:
        """Return the failure remembered for the client, the grant or `key`."""
        if self._down is not None and now < self._down_until:
            return self._down
        refused = self._refused_grants.get(grant)
        if refused is not None:
            if now < refused[0]:
                return refused[1]
            del self._refused_grants[grant]
        return cache.refused(key, now)

    async def _drop(
        self, cache: _TokenCache, key: Hashable, value: str
    ) -> None:
        """Drop the token cached for `key`, on the loop the client was opened on.

        Every change to a cache happens on that loop, so a refusal reported
        from another loop never races a fetch storing a new token.
        """
        loop = self._loop
        if loop is not None and asyncio.get_running_loop() is not loop:
            future = asyncio.run_coroutine_threadsafe(
                self._drop(cache, key, value), loop
            )
            await asyncio.wrap_future(future)
            return
        cache.drop(key, value)

    def _start(
        self,
        cache: _TokenCache,
        key: Hashable,
        request: _Request,
        not_after: int | None,
        *,
        background: bool,
    ) -> asyncio.Task[AccessToken]:
        """Start the one fetch for `key`, owned by the client."""
        flight = (id(cache), key)
        task = asyncio.get_running_loop().create_task(
            self._fetch(cache, key, request, not_after, background=background)
        )
        self._inflight[flight] = task
        task.add_done_callback(lambda done: self._settled(flight, done))
        return task

    def _settled(
        self, flight: Hashable, task: asyncio.Task[AccessToken]
    ) -> None:
        """Let the next request fetch again, once this fetch has finished.

        The outcome is read here, so a fetch that failed after every caller
        stopped waiting is not reported as an exception nobody retrieved.
        """
        self._inflight.pop(flight, None)
        if not task.cancelled():
            task.exception()

    async def _fetch(
        self,
        cache: _TokenCache,
        key: Hashable,
        request: _Request,
        not_after: int | None,
        *,
        background: bool,
    ) -> AccessToken:
        """Request a token, cache it, and remember a failure.

        Raises:
            TokenUnavailableError: If the request fails or is refused.
        """
        started = monotonic()
        attributes = {
            "grelmicro.oauth_client.name": self._name,
            "grelmicro.oauth_client.grant": request.grant,
        }
        try:
            with _span(attributes):
                token, lifetime, refresh_in = await self._issue(request)
        except _Failure as failure:
            error = failure.error
            until = monotonic() + failure.seconds
            if failure.scope == "client":
                self._remember_down(failure)
            elif failure.scope == "grant":
                self._refused_grants[request.grant] = (until, error)
            else:
                cache.refuse(key, until, error)
            _record(attributes, failure.outcome, error.error, started)
            if isinstance(error, ClientRejectedError):
                _client_rejected(self._name, error)
            if background:
                logger.warning(
                    "token refresh failed, serving the cached token until it"
                    " expires: %s",
                    error,
                )
            raise _again(error) from None
        now = monotonic()
        wall = time()
        expires = now + lifetime
        expires_at = wall + lifetime
        capped = False
        if not_after is not None:
            until_caller = not_after - wall
            capped = until_caller < lifetime
            expires = min(expires, now + until_caller)
            expires_at = min(expires_at, float(not_after))
        token = replace(token, expires_at=math.floor(expires_at))
        remaining = expires - now
        if request.cacheable and remaining > 0:
            # A token cut short by the caller's own expiry cannot be renewed
            # for longer, so it is kept to its end, not refreshed ahead.
            refresh_at = (
                expires
                if capped
                else self._refresh_at(now, remaining, refresh_in)
            )
            cache.put(key, _Entry(token, refresh_at, expires), now)
        _record(attributes, "success", None, started)
        return token

    def _refresh_at(
        self, now: float, lifetime: float, refresh_in: float | None
    ) -> float:
        """Return when a token living `lifetime` seconds from `now` refreshes.

        A server's `refresh_in` decides. Otherwise the token refreshes
        `refresh_before` ahead, at most half its lifetime, at a random point
        in the first half of that window.
        """
        if refresh_in is not None and refresh_in < lifetime:
            return now + refresh_in
        before = min(self._config.refresh_before, lifetime / 2)
        return now + lifetime - before + random.uniform(0, before / 2)  # noqa: S311

    def _remember_down(self, failure: _Failure) -> None:
        """Remember a failure that applies to every token of the client."""
        self._down_until = monotonic() + failure.seconds
        self._down = failure.error

    async def _issue(
        self, request: _Request
    ) -> tuple[AccessToken, float, float | None]:
        """Send `request` to the token endpoint and read the token it returns.

        A connection lost before any answer is sent again once, with its
        client authentication built again, so a signed assertion carries a
        new `jti`.

        Raises:
            _Failure: If the request fails or is refused.
        """
        endpoint = await self._endpoint()
        module = self._httpx
        lost = (module.ConnectError, module.RemoteProtocolError)
        attempts = 2
        for attempt in range(attempts):
            form = dict(request.form)
            headers = {"Accept": "application/json"}
            await self._authenticate(form, headers, endpoint)
            try:
                status, body, retry_after = await self._post(
                    endpoint, form, headers
                )
            except lost as error:
                if attempt + 1 < attempts:
                    continue
                msg = (
                    "the token endpoint could not be reached:"
                    f" {type(error).__name__}"
                )
                raise self._down_failure(msg) from None
            except module.HTTPError as error:
                msg = (
                    "the token endpoint could not be reached:"
                    f" {type(error).__name__}"
                )
                raise self._down_failure(msg) from None
            except _Oversized:
                msg = (
                    "the token endpoint answered more than"
                    f" {self._config.max_bytes} bytes"
                )
                raise self._down_failure(msg) from None
            return self._read(status, body, retry_after)
        msg = "unreachable"  # pragma: no cover
        raise AssertionError(msg)  # pragma: no cover

    async def _post(
        self, url: str, form: dict[str, str], headers: dict[str, str]
    ) -> tuple[int, bytes, str | None]:
        """Post `form` and return the status, the body and `Retry-After`.

        Raises:
            _Oversized: If the body passes `max_bytes`.
        """
        config = self._config
        async with self._client.stream(
            "POST", url, data=form, headers=headers, timeout=config.timeout
        ) as response:
            body = await _read_capped(response, config.max_bytes)
            return (
                response.status_code,
                body,
                response.headers.get("retry-after"),
            )

    async def _get(self, url: str) -> tuple[int, bytes]:
        """Get `url` and return the status and the body.

        Raises:
            _Oversized: If the body passes `max_bytes`.
        """
        config = self._config
        async with self._client.stream(
            "GET",
            url,
            headers={"Accept": "application/json"},
            timeout=config.timeout,
        ) as response:
            body = await _read_capped(response, config.max_bytes)
            return response.status_code, body

    def _read(
        self, status: int, body: bytes, retry_after: str | None
    ) -> tuple[AccessToken, float, float | None]:
        """Read a token response, or raise how it failed.

        Raises:
            _Failure: If the response is not a usable token.
        """
        ok = 200
        if status == ok:
            return self._issued(body)
        interval = self._config.retry_interval
        if status in (429, 503):
            asked = _retry_after(retry_after)
            seconds = (
                interval
                if asked is None
                else min(max(asked, interval), _RETRY_AFTER_CEILING)
            )
            raise _Failure(
                TokenUnavailableError(
                    f"the authorization server answered {status}"
                ),
                scope="client",
                seconds=seconds,
                outcome="throttled",
            )
        if (
            HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR
            and status != HTTPStatus.REQUEST_TIMEOUT
        ):
            raise self._refusal(status, body)
        msg = f"the authorization server answered {status}"
        raise self._down_failure(msg)

    def _refusal(self, status: int, body: bytes) -> _Failure:
        """Return how a token request the server refused is remembered.

        A refusal answers this request, so it is remembered for the token
        asked for, whatever the status and whether or not it names a code.
        `invalid_client` refuses the service itself and `unauthorized_client`
        a grant it may not use, so those two reach further.
        """
        parsed = _json_object(body)
        found = None if parsed is None else parsed.get("error")
        code = (
            found
            if isinstance(found, str) and _ERROR_CODE.fullmatch(found)
            else None
        )
        description = (
            None
            if code is None or parsed is None
            else _description(parsed.get("error_description"))
        )
        interval = self._config.retry_interval
        if code is not None and code in (
            "invalid_client",
            "unauthorized_client",
        ):
            return _Failure(
                ClientRejectedError(
                    self._rejected_message(code),
                    error=code,
                    description=description,
                ),
                scope="client" if code == "invalid_client" else "grant",
                seconds=interval,
                outcome="rejected",
            )
        return _Failure(
            TokenUnavailableError(
                "the authorization server refused the token request:"
                f" {code or status}",
                error=code,
                description=description,
            ),
            scope="token",
            seconds=interval,
            outcome="refused",
        )

    def _issued(self, body: bytes) -> tuple[AccessToken, float, float | None]:
        """Read the token a successful response carries.

        Raises:
            _Failure: If the response carries no usable bearer token.
        """
        parsed = _json_object(body)
        if parsed is None:
            msg = "the token response is not a JSON object"
            raise self._down_failure(msg)
        value = parsed.get("access_token")
        if not isinstance(value, str) or not value:
            msg = "the token response carries no access_token"
            raise self._down_failure(msg)
        token_type = parsed.get("token_type")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            msg = "the token response carries a token type other than Bearer"
            raise self._down_failure(msg)
        lifetime = _lifetime(parsed.get("expires_in"))
        scope = parsed.get("scope")
        token = AccessToken(
            value=value,
            token_type="Bearer",  # noqa: S106
            expires_at=0,
            scopes=frozenset(scope.split())
            if isinstance(scope, str)
            else frozenset(),
        )
        return (
            token,
            self._config.default_lifetime if lifetime is None else lifetime,
            _seconds(parsed.get("refresh_in")),
        )

    def _down_failure(self, message: str) -> _Failure:
        """Return a failure that applies to every token of the client."""
        return _Failure(
            TokenUnavailableError(message),
            scope="client",
            seconds=self._config.retry_interval,
            outcome="failure",
        )

    def _rejected_message(self, code: str) -> str:
        """Return the message of a refused client, with a hint where one helps."""
        if code == "unauthorized_client":
            return (
                "the authorization server does not allow this client this"
                f" grant: {code}"
            )
        message = f"the authorization server refused this client: {code}"
        auth = self._auth
        if auth._kind == _PRIVATE_KEY and auth._audience == "issuer":  # noqa: SLF001
            message += (
                ". If it wants the token endpoint as the assertion audience,"
                ' pass audience="token_endpoint" to ClientAuth.private_key'
            )
        return message

    async def _endpoint(self) -> str:
        """Return the token endpoint, discovering it once when needed.

        Raises:
            _Failure: If the issuer's metadata cannot be read.
        """
        endpoint = self._token_endpoint
        if endpoint is not None:
            return endpoint
        discovery = self._discovery
        if discovery is None:
            discovery = asyncio.get_running_loop().create_task(self._discover())
            self._discovery = discovery
            discovery.add_done_callback(self._discovered)
        return await asyncio.shield(discovery)

    def _discovered(self, task: asyncio.Task[str]) -> None:
        """Let the next request discover again, once this discovery finished."""
        self._discovery = None
        if not task.cancelled():
            task.exception()

    async def _discover(self) -> str:
        """Read the token endpoint from the issuer's metadata.

        Raises:
            _Failure: If no metadata document names this issuer and an
                `https` token endpoint.
        """
        issuer = self._config.issuer or ""
        module = self._httpx
        failures: list[str] = []
        for url in _metadata_urls(issuer, first=None):
            try:
                status, body = await self._get(url)
            except module.HTTPError as error:
                failures.append(f"{url}: {type(error).__name__}")
                continue
            except _Oversized:
                failures.append(
                    f"{url}: larger than {self._config.max_bytes} bytes"
                )
                continue
            if status != 200:  # noqa: PLR2004
                failures.append(f"{url}: answered {status}")
                continue
            try:
                endpoint, methods = _token_endpoint_of(body, issuer)
            except _UnusableMetadataError as error:
                failures.append(f"{url}: {error}")
                continue
            self._token_endpoint = endpoint
            self._auth_methods = methods
            return endpoint
        msg = f"no issuer metadata could be read: {', '.join(failures)}"
        raise self._down_failure(msg)

    async def _authenticate(
        self, form: dict[str, str], headers: dict[str, str], endpoint: str
    ) -> None:
        """Add the client's authentication to a token request.

        Raises:
            _Failure: If an assertion file cannot be read.
        """
        auth = self._auth
        config = self._config
        client_id = config.client_id
        if auth._kind == _SECRET:  # noqa: SLF001
            secret = config.client_secret
            value = "" if secret is None else secret.get_secret_value()
            if self._secret_method() == "basic":
                pair = f"{quote_plus(client_id)}:{quote_plus(value)}"
                headers["Authorization"] = (
                    f"Basic {base64.b64encode(pair.encode()).decode('ascii')}"
                )
            else:
                form["client_id"] = client_id
                form["client_secret"] = value
            return
        form["client_id"] = client_id
        form["client_assertion_type"] = _JWT_BEARER_ASSERTION
        if auth._kind == _PRIVATE_KEY:  # noqa: SLF001
            form["client_assertion"] = self._signed_assertion(endpoint)
        else:
            form["client_assertion"] = await self._file_assertion()

    def _secret_method(self) -> Literal["basic", "post"]:
        """Return how the secret is sent: pinned, or read from the metadata."""
        pinned = self._auth._method  # noqa: SLF001
        if pinned is not None:
            return pinned
        methods = self._auth_methods
        if methods is None or "client_secret_basic" in methods:
            return "basic"
        if "client_secret_post" in methods:
            return "post"
        return "basic"

    def _signed_assertion(self, endpoint: str) -> str:
        """Return a fresh assertion, signed by the client's private key."""
        auth = self._auth
        config = self._config
        now = math.floor(time())
        naming_issuer = auth._audience == "issuer"  # noqa: SLF001
        header: dict[str, str] = {"alg": auth._algorithm or ""}  # noqa: SLF001
        if naming_issuer:
            header["typ"] = _ASSERTION_TYPE
        if auth._kid is not None:  # noqa: SLF001
            header["kid"] = auth._kid  # noqa: SLF001
        if auth._thumbprint is not None:  # noqa: SLF001
            header["x5t#S256"] = auth._thumbprint  # noqa: SLF001
        claims = {
            "iss": config.client_id,
            "sub": config.client_id,
            "aud": config.issuer if naming_issuer else endpoint,
            "jti": secrets.token_urlsafe(32),
            "iat": now,
            "nbf": now,
            "exp": now + _ASSERTION_LIFETIME,
        }
        signing_input = f"{_b64u_json(header)}.{_b64u_json(claims)}"
        signature = auth._signer.sign(signing_input.encode("ascii"))  # noqa: SLF001
        return f"{signing_input}.{_b64u(signature)}"

    async def _file_assertion(self) -> str:
        """Return the assertion the assertion file holds now.

        Raises:
            _Failure: If the file cannot be read, or is empty.
        """
        path = Path(self._config.assertion_file or "")
        try:
            text = await asyncio.to_thread(path.read_text, "utf-8")
        except (OSError, UnicodeDecodeError) as error:
            msg = (
                f"the assertion file could not be read: {type(error).__name__}"
            )
            raise self._down_failure(msg) from None
        assertion = text.strip()
        if not assertion:
            msg = "the assertion file is empty"
            raise self._down_failure(msg)
        return assertion


def _with_client_auth(
    config: OAuthClientConfig, auth: ClientAuth
) -> OAuthClientConfig:
    """Return `config` holding the secret or the path `auth` carries.

    Raises:
        SettingsValidationError: If `config` already holds the one `auth`
            carries.
    """
    update: dict[str, object] = {}
    secret = auth._secret  # noqa: SLF001
    if secret is not None:
        if config.client_secret is not None:
            msg = (
                "the client secret is set on both ClientAuth.secret() and the"
                " config. Set it in one place."
            )
            raise SettingsValidationError(msg)
        update["client_secret"] = secret
    path = auth._path  # noqa: SLF001
    if path is not None:
        if config.assertion_file is not None:
            msg = (
                "the assertion file is set on both"
                " ClientAuth.assertion_file() and the config. Set it in one"
                " place."
            )
            raise SettingsValidationError(msg)
        update["assertion_file"] = path
    return config.model_copy(update=update) if update else config


def _check_auth(
    config: OAuthClientConfig, auth: ClientAuth, *, prefix: str
) -> None:
    """Refuse a config that lacks what `auth` needs, or holds what it does not use.

    Raises:
        SettingsValidationError: If it does.
    """

    def named(field_name: str) -> str:
        return f"{prefix}{field_name.upper()}" if prefix else field_name

    kind = auth._kind  # noqa: SLF001
    if kind != _SECRET and config.client_secret is not None:
        msg = (
            f"{named('client_secret')} is set for a client that does not"
            " authenticate with a secret."
        )
        raise SettingsValidationError(msg)
    if kind != _ASSERTION_FILE and config.assertion_file is not None:
        msg = (
            f"{named('assertion_file')} is set for a client that does not"
            " authenticate with an assertion file."
        )
        raise SettingsValidationError(msg)
    if kind == _SECRET and config.client_secret is None:
        msg = (
            "ClientAuth.secret() needs a client secret. Pass it, or set"
            f" {named('client_secret')}."
        )
        raise SettingsValidationError(msg)
    if kind == _ASSERTION_FILE and config.assertion_file is None:
        msg = (
            "ClientAuth.assertion_file() needs a path. Pass it, or set"
            f" {named('assertion_file')}."
        )
        raise SettingsValidationError(msg)
    if (
        kind == _PRIVATE_KEY
        and auth._audience == "issuer"  # noqa: SLF001
        and config.issuer is None
    ):
        msg = (
            "an assertion naming the issuer as its audience needs an issuer."
            ' Pass issuer=, or audience="token_endpoint" to'
            " ClientAuth.private_key."
        )
        raise SettingsValidationError(msg)


class _TokenPattern:
    """What `ClientCredentials` and `TokenExchange` share: a name, a target, a client."""

    _PREFIX: ClassVar[str]

    _name: str
    _config: _TargetConfig
    _client: OAuthClient | None
    _client_name: str

    def _hold(
        self,
        name: str,
        config: _TargetConfig,
        client: OAuthClient | str | None,
    ) -> None:
        """Hold the name, the target and the client to resolve."""
        self._name = name
        self._config = config
        if isinstance(client, OAuthClient):
            self._client = client
            self._client_name = client.name
        else:
            self._client = None
            self._client_name = client or "default"

    @property
    def name(self) -> str:
        """The name of the API the token is for."""
        return self._name

    def _resolved(self) -> OAuthClient:
        """Return the client passed in, or the one the active app registers.

        Raises:
            OutOfContextError: If no client is passed and none is registered
                in this scope.
        """
        client = self._client
        if client is not None:
            return client
        from grelmicro._app import resolve_ambient  # noqa: PLC0415

        try:
            return resolve_ambient((OAuthClient.kind, self._client_name))
        except LookupError:
            msg = (
                f"{type(self).__name__}({self._name!r}) found no OAuthClient."
                " Register one in Grelmicro(uses=[...]) and run inside"
                " `async with micro:` or after `micro.install(app)`, or pass"
                " client=."
            )
            raise OutOfContextError(msg) from None

    def _target(self) -> dict[str, str]:
        """Return the form fields that name the API."""
        config = self._config
        form: dict[str, str] = {}
        if config.audience is not None:
            form["audience"] = config.audience
        if config.resource is not None:
            form["resource"] = config.resource
        if config.scopes:
            form["scope"] = " ".join(config.scopes)
        return form


def _resolve_target[C: _TargetConfig](
    config_cls: type[C],
    prefix: str,
    name: str,
    *,
    env_load: bool | None,
    **settings: object,
) -> C:
    """Resolve a target config from keywords and the pattern's own variables."""
    env_prefix, _ = env_prefixes(prefix, name)
    scopes = settings.get("scopes")
    if isinstance(scopes, str):
        settings["scopes"] = [scopes]
    elif scopes is not None:
        settings["scopes"] = list(cast("Iterable[str]", scopes))
    return resolve_config(
        config_cls,
        explicit=None,
        kwargs=settings,
        env_prefix=env_prefix,
        env_load=env_load,
    )


class ClientCredentials(_TokenPattern):
    """A token for the service itself, to call another API.

    The name is the API it calls, and its environment namespace. It finds
    the registered `OAuthClient` through the app, or takes `client=`. The
    token is fetched on first use, cached, and refreshed shortly before it
    expires, however many requests use it.

    Example:
        ```python
        payments_token = ClientCredentials("payments-api", audience="payments-api")
        payments = httpx.AsyncClient(
            base_url="https://payments.internal", auth=payments_token.auth()
        )
        ```
    """

    def __init__(
        self,
        name: Annotated[
            str,
            Doc(
                "The API the token is for, and the environment namespace:"
                " `GREL_CLIENTCREDENTIALS_{NAME}_`."
            ),
        ],
        *,
        audience: Annotated[str | None, Doc("Sent as `audience`.")] = None,
        resource: Annotated[
            str | None, Doc("Sent as `resource`, RFC 8707.")
        ] = None,
        scopes: Annotated[
            str | Sequence[str] | None, Doc("Sent as `scope`.")
        ] = None,
        client: Annotated[
            OAuthClient | str | None,
            Doc(
                "The client to use: an `OAuthClient`, or the name of a"
                " registered one. Left out, the registered default."
            ),
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                "Whether to read environment variables. `None` follows the"
                " process-wide `GREL_ENV_LOAD` flag."
            ),
        ] = None,
    ) -> None:
        """Name the API, and how the token for it is requested.

        Raises:
            SettingsValidationError: If a setting is refused.
        """
        config = _resolve_target(
            ClientCredentialsConfig,
            "CLIENTCREDENTIALS",
            name,
            env_load=env_load,
            audience=audience,
            resource=resource,
            scopes=scopes,
        )
        self._setup(name, config, client)

    @classmethod
    def from_config(
        cls,
        name: Annotated[str, Doc("The API the token is for.")],
        config: Annotated[
            ClientCredentialsConfig, Doc("The target, taken as it is.")
        ],
        *,
        client: Annotated[
            OAuthClient | str | None, Doc("The client to use.")
        ] = None,
    ) -> Self:
        """Build from a configuration that is already whole, reading no variable."""
        instance = cls.__new__(cls)
        instance._setup(name, config, client)  # noqa: SLF001
        return instance

    def _setup(
        self,
        name: str,
        config: ClientCredentialsConfig,
        client: OAuthClient | str | None,
    ) -> None:
        """Hold the target, and build the request it sends."""
        self._hold(name, config, client)
        self._cache = _TokenCache(_CLIENT_CREDENTIALS_CACHE)
        self._request = _Request(
            "client_credentials",
            {"grant_type": "client_credentials", **self._target()},
            cacheable=True,
        )

    _config: ClientCredentialsConfig

    @property
    def config(self) -> ClientCredentialsConfig:
        """The target in effect."""
        return self._config

    async def token(self) -> AccessToken:
        """Return a valid token for the API, fetching one when needed.

        Raises:
            OutOfContextError: If no client is open to fetch it with.
            TokenUnavailableError: If no valid token is cached and none can
                be fetched.
        """
        return await self._obtain(None)

    def auth(self) -> Any:  # noqa: ANN401
        """Return an `httpx.Auth` that sends the token with every request.

        It works with `httpx.AsyncClient`, from `httpx` or `httpx2`. On a
        `401` refusing the token, it drops the token, and sends a request
        that is safe to repeat again once with a new one.

        Raises:
            DependencyNotFoundError: If neither `httpx` nor `httpx2` is
                installed.
        """
        return _auth_class()(_Source(self, None))

    async def _obtain(self, subject: VerifiedToken | None) -> AccessToken:  # noqa: ARG002
        """Return the token, from the cache or a shared fetch."""
        client = self._resolved()
        return await client._token(  # noqa: SLF001
            self._cache,
            client._identity,  # noqa: SLF001
            self._request,
            not_after=None,
        )

    async def _invalidate(
        self,
        subject: VerifiedToken | None,  # noqa: ARG002
        token: AccessToken,
    ) -> None:
        """Drop `token`, while it is still the one cached."""
        client = self._resolved()
        await client._drop(  # noqa: SLF001
            self._cache,
            client._identity,  # noqa: SLF001
            token.value,
        )


class TokenExchange(_TokenPattern):
    """A token for another API, exchanged for the token a request presented.

    The issued token still names the user as its subject. It follows RFC
    8693 token exchange, and `TokenExchange.on_behalf_of` uses the
    on-behalf-of grant instead, which Microsoft Entra ID uses.

    Only a `VerifiedToken`, which `CurrentToken` gives, can be exchanged. An
    exchanged token is cached per caller and never outlives the caller's own
    token. The cache keys on a SHA-256 digest of it, never on the token.

    Example:
        ```python
        payments_for_user = TokenExchange("payments-api", audience="payments-api")


        @app.post("/orders")
        async def create_order(token: CurrentToken) -> dict[str, str]:
            response = await payments.post(
                "/charges", auth=payments_for_user.auth(token)
            )
            return response.json()
        ```
    """

    _GRANT: ClassVar[str] = "token_exchange"

    def __init__(
        self,
        name: Annotated[
            str,
            Doc(
                "The API the token is for, and the environment namespace:"
                " `GREL_TOKENEXCHANGE_{NAME}_`."
            ),
        ],
        *,
        audience: Annotated[str | None, Doc("Sent as `audience`.")] = None,
        resource: Annotated[
            str | None, Doc("Sent as `resource`, RFC 8707.")
        ] = None,
        scopes: Annotated[
            str | Sequence[str] | None, Doc("Sent as `scope`.")
        ] = None,
        cache_size: Annotated[
            int | None, Doc("Exchanged tokens held in memory.")
        ] = None,
        client: Annotated[
            OAuthClient | str | None,
            Doc(
                "The client to use: an `OAuthClient`, or the name of a"
                " registered one. Left out, the registered default."
            ),
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                "Whether to read environment variables. `None` follows the"
                " process-wide `GREL_ENV_LOAD` flag."
            ),
        ] = None,
    ) -> None:
        """Name the API, and exchange for it with RFC 8693 token exchange.

        Raises:
            SettingsValidationError: If a setting is refused.
        """
        config = _resolve_target(
            TokenExchangeConfig,
            "TOKENEXCHANGE",
            name,
            env_load=env_load,
            audience=audience,
            resource=resource,
            scopes=scopes,
            cache_size=cache_size,
        )
        self._setup(name, config, client, grant="token_exchange")

    @classmethod
    def on_behalf_of(
        cls,
        name: Annotated[
            str,
            Doc(
                "The API the token is for, and the environment namespace:"
                " `GREL_TOKENEXCHANGE_{NAME}_`."
            ),
        ],
        *,
        scopes: Annotated[
            str | Sequence[str] | None,
            Doc("Sent as `scope`, such as `api://payments/.default`."),
        ] = None,
        cache_size: Annotated[
            int | None, Doc("Exchanged tokens held in memory.")
        ] = None,
        client: Annotated[
            OAuthClient | str | None, Doc("The client to use.")
        ] = None,
        env_load: Annotated[
            bool | None,
            Doc(
                "Whether to read environment variables. `None` follows the"
                " process-wide `GREL_ENV_LOAD` flag."
            ),
        ] = None,
    ) -> Self:
        """Exchange with the on-behalf-of grant, which names the API by scopes.

        Raises:
            SettingsValidationError: If a setting is refused, or a variable
                names an audience or a resource, which this grant does not
                send.
        """
        config = _resolve_target(
            TokenExchangeConfig,
            "TOKENEXCHANGE",
            name,
            env_load=env_load,
            scopes=scopes,
            cache_size=cache_size,
        )
        _check_on_behalf_of(config)
        instance = cls.__new__(cls)
        instance._setup(name, config, client, grant="on_behalf_of")  # noqa: SLF001
        return instance

    @classmethod
    def from_config(
        cls,
        name: Annotated[str, Doc("The API the token is for.")],
        config: Annotated[
            TokenExchangeConfig, Doc("The target, taken as it is.")
        ],
        *,
        grant: Annotated[
            Literal["token_exchange", "on_behalf_of"],
            Doc("The grant to exchange with."),
        ] = "token_exchange",
        client: Annotated[
            OAuthClient | str | None, Doc("The client to use.")
        ] = None,
    ) -> Self:
        """Build from a configuration that is already whole, reading no variable.

        Raises:
            SettingsValidationError: If `grant` is not one this knows, or the
                on-behalf-of grant is given an audience or a resource.
        """
        if grant not in ("token_exchange", "on_behalf_of"):
            msg = "grant must be 'token_exchange' or 'on_behalf_of'"
            raise SettingsValidationError(msg)
        if grant == "on_behalf_of":
            _check_on_behalf_of(config)
        instance = cls.__new__(cls)
        instance._setup(name, config, client, grant=grant)  # noqa: SLF001
        return instance

    def _setup(
        self,
        name: str,
        config: TokenExchangeConfig,
        client: OAuthClient | str | None,
        *,
        grant: str,
    ) -> None:
        """Hold the target, the grant and the cache."""
        self._hold(name, config, client)
        self._grant = grant
        self._cache = _TokenCache(config.cache_size)

    _config: TokenExchangeConfig

    @property
    def config(self) -> TokenExchangeConfig:
        """The target in effect."""
        return self._config

    async def token(
        self,
        token: Annotated[
            VerifiedToken,
            Doc("The caller's verified token, as `CurrentToken` gives it."),
        ],
    ) -> AccessToken:
        """Return a token for the API, exchanged for the caller's `token`.

        Raises:
            TypeError: If `token` is not a `VerifiedToken`.
            OutOfContextError: If no client is open to exchange it with.
            TokenUnavailableError: If no valid token is cached and the
                exchange fails or is refused.
        """
        return await self._obtain(_verified(token))

    def auth(
        self,
        token: Annotated[
            VerifiedToken,
            Doc("The caller's verified token, as `CurrentToken` gives it."),
        ],
    ) -> Any:  # noqa: ANN401
        """Return an `httpx.Auth` that sends a token exchanged for the caller's.

        Pass it per request, `client.post(..., auth=exchange.auth(token))`,
        since each request carries its own caller.

        Raises:
            TypeError: If `token` is not a `VerifiedToken`.
            DependencyNotFoundError: If neither `httpx` nor `httpx2` is
                installed.
        """
        return _auth_class()(_Source(self, _verified(token)))

    def _key(self, client: OAuthClient, subject: VerifiedToken) -> Hashable:
        """Return the cache key of `subject`, never the token itself."""
        digest = hashlib.sha256(subject.value.encode()).digest()
        return (client._identity, self._grant, digest)  # noqa: SLF001

    def _form(self, subject: VerifiedToken) -> dict[str, str]:
        """Return the exchange request for `subject`."""
        if self._grant == "on_behalf_of":
            form = {
                "grant_type": _JWT_BEARER_GRANT,
                "assertion": subject.value,
                "requested_token_use": "on_behalf_of",
            }
        else:
            form = {
                "grant_type": _TOKEN_EXCHANGE_GRANT,
                "subject_token": subject.value,
                "subject_token_type": _ACCESS_TOKEN_TYPE,
                "requested_token_type": _ACCESS_TOKEN_TYPE,
            }
        form.update(self._target())
        return form

    async def _obtain(self, subject: VerifiedToken | None) -> AccessToken:
        """Return the exchanged token, from the cache or a shared fetch."""
        verified = _verified(subject)
        client = self._resolved()
        request = _Request(
            self._grant,
            self._form(verified),
            cacheable=verified.expires_at is not None,
        )
        return await client._token(  # noqa: SLF001
            self._cache,
            self._key(client, verified),
            request,
            not_after=verified.expires_at,
        )

    async def _invalidate(
        self, subject: VerifiedToken | None, token: AccessToken
    ) -> None:
        """Drop `token`, while it is still the one cached for `subject`."""
        verified = _verified(subject)
        client = self._resolved()
        await client._drop(  # noqa: SLF001
            self._cache, self._key(client, verified), token.value
        )


def _check_on_behalf_of(config: TokenExchangeConfig) -> None:
    """Refuse an audience or a resource, which the on-behalf-of grant never sends.

    Raises:
        SettingsValidationError: If `config` names either.
    """
    if config.audience is not None or config.resource is not None:
        msg = (
            "the on-behalf-of grant names the API with scopes, and sends"
            " no audience or resource"
        )
        raise SettingsValidationError(msg)


def _verified(token: object) -> VerifiedToken:
    """Return `token`, refusing anything but a `VerifiedToken`.

    Raises:
        TypeError: If `token` is not one.
    """
    if not isinstance(token, VerifiedToken):
        msg = (
            "TokenExchange exchanges a VerifiedToken, which CurrentToken gives."
            " A token string is refused, so an unverified credential is never"
            " exchanged."
        )
        raise TypeError(msg)
    return token


class _Source:
    """What an `httpx.Auth` asks for a token, and tells about a refused one."""

    __slots__ = ("_pattern", "_subject")

    def __init__(
        self,
        pattern: ClientCredentials | TokenExchange,
        subject: VerifiedToken | None,
    ) -> None:
        """Serve the token `pattern` gets for `subject`."""
        self._pattern = pattern
        self._subject = subject

    async def token(self) -> AccessToken:
        """Return the token to send."""
        return await self._pattern._obtain(self._subject)  # noqa: SLF001

    async def invalidate(self, token: AccessToken) -> None:
        """Drop `token`, which the API refused."""
        await self._pattern._invalidate(self._subject, token)  # noqa: SLF001


def _authorization(token: AccessToken) -> str:
    """Return the `Authorization` header value for `token`."""
    return f"{token.token_type} {token.value}"


async def _async_auth_flow(self: Any, request: Any) -> AsyncGenerator[Any, Any]:  # noqa: ANN401
    """Send the token, and send a safe request again once when it is refused."""
    source: _Source = self._source
    token = await source.token()
    request.headers["Authorization"] = _authorization(token)
    response = yield request
    if response.status_code != 401 or not _refuses_token(  # noqa: PLR2004
        response.headers.get_list("www-authenticate")
    ):
        return
    await source.invalidate(token)
    if request.method not in _SAFE_METHODS or not _replayable(request):
        return
    token = await source.token()
    request.headers["Authorization"] = _authorization(token)
    yield request


def _sync_auth_flow(self: Any, request: Any) -> Any:  # noqa: ANN401, ARG001
    """Refuse a synchronous client, which cannot await a shared fetch.

    Raises:
        RuntimeError: Always.
    """
    msg = (
        "outbound tokens are fetched asynchronously, so auth() works with"
        " httpx.AsyncClient only"
    )
    raise RuntimeError(msg)


def _auth_init(self: Any, source: _Source) -> None:  # noqa: ANN401
    """Hold the source the flow asks for tokens."""
    self._source = source


@cache
def _auth_class() -> type:
    """Return an `httpx.Auth` class that both `httpx` and `httpx2` accept.

    Each line checks an auth object against its own `Auth` class, so the
    class inherits from every one installed.

    Raises:
        DependencyNotFoundError: If neither is installed.
    """
    bases: list[type] = []
    for module_name in ("httpx", "httpx2"):
        try:
            bases.append(import_module(module_name).Auth)
        except ImportError:
            continue
    if not bases:
        raise DependencyNotFoundError(module="httpx")
    return type(
        "TokenAuth",
        tuple(bases),
        {
            "__doc__": "Sends an outbound token with every request.",
            "__module__": __name__,
            "__init__": _auth_init,
            "async_auth_flow": _async_auth_flow,
            "sync_auth_flow": _sync_auth_flow,
        },
    )


def _refuses_token(challenges: Iterable[str]) -> bool:
    """Return whether a `WWW-Authenticate` challenge refuses the bearer token."""
    return any(_INVALID_TOKEN.search(challenge) for challenge in challenges)


def _replayable(request: Any) -> bool:  # noqa: ANN401
    """Return whether the request's body can be sent a second time."""
    try:
        request.content  # noqa: B018
    except Exception:  # noqa: BLE001 - each httpx line raises its own
        return False
    return True


async def _read_capped(response: Any, limit: int) -> bytes:  # noqa: ANN401
    """Read a response body, abandoning it once it passes `limit` bytes.

    Raises:
        _Oversized: If it does.
    """
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) > limit:
            raise _Oversized
    return bytes(body)


def _token_endpoint_of(
    document: bytes, issuer: str
) -> tuple[str, frozenset[str] | None]:
    """Return the token endpoint and client authentication methods metadata names.

    Raises:
        _UnusableMetadataError: If the document does not name this issuer
            exactly, or names no `https` token endpoint.
    """
    parsed = _json_object(document)
    if parsed is None:
        shape = "not a JSON object"
        raise _UnusableMetadataError(shape)
    named = parsed.get("issuer")
    if named != issuer:
        if isinstance(named, str) and "{tenantid}" in named:
            placeholder = (
                "names a {tenantid} placeholder as its issuer, so name the"
                " tenant by its ID"
            )
            raise _UnusableMetadataError(placeholder)
        other = f"answers for another issuer than {issuer}"
        raise _UnusableMetadataError(other)
    endpoint = parsed.get("token_endpoint")
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        insecure = "names no https token_endpoint"
        raise _UnusableMetadataError(insecure)
    methods = parsed.get("token_endpoint_auth_methods_supported")
    supported = (
        frozenset(method for method in methods if isinstance(method, str))
        if isinstance(methods, list)
        else None
    )
    return endpoint, supported


def _json_object(body: bytes) -> dict[str, Any] | None:
    """Return `body` as a JSON object, or `None` when it is not one.

    A body nested too deeply to parse is not one either.
    """
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _number(value: object) -> float | None:
    """Return the finite number a response states, or `None`.

    A number written as a string counts when it is plain ASCII digits. One
    too large to read, or to hold as a float, does not.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not (value.isascii() and value.isdigit()):
            return None
        try:
            value = int(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _seconds(value: object) -> float | None:
    """Return a positive number of seconds a response states, or `None`."""
    seconds = _number(value)
    return seconds if seconds is not None and seconds > 0 else None


def _lifetime(value: object) -> float | None:
    """Return how long a token lives, or `None` when the response does not say.

    Zero or less means the token is already expired, so it is used once and
    never cached.
    """
    seconds = _number(value)
    return None if seconds is None else max(seconds, 0.0)


def _retry_after(value: str | None) -> float | None:
    """Return the seconds a `Retry-After` header asks for, or `None`.

    A number too large to read asks for the longest wait honoured.
    """
    if value is None:
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        try:
            return min(float(int(text)), _RETRY_AFTER_CEILING)
        except (ValueError, OverflowError):
            return _RETRY_AFTER_CEILING
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time())


def _description(value: object) -> str | None:
    """Return an `error_description` fit to keep, or `None`."""
    if not isinstance(value, str):
        return None
    return encoded(value[:_DESCRIPTION_LIMIT])


def _again(error: TokenUnavailableError) -> TokenUnavailableError:
    """Return a fresh copy of `error`, so each raise starts a clean traceback."""
    return type(error)(
        str(error), error=error.error, description=error.description
    )


def _record(
    attributes: Mapping[str, str],
    outcome: str,
    error_type: str | None,
    started: float,
) -> None:
    """Count one token request, and record how long it took."""
    recorded = {**attributes, "grelmicro.outcome": outcome}
    if error_type is not None:
        recorded["error.type"] = error_type
    _emit.incr(FETCHES, recorded, unit="{fetch}")
    _emit.record_duration(FETCH_DURATION, monotonic() - started, recorded)


def _client_rejected(name: str, error: ClientRejectedError) -> None:
    """Write the security event of a client the authorization server refused."""
    if not security_events.isEnabledFor(logging.WARNING):
        return
    security_events.warning(
        "oauth client %s refused by the authorization server: %s",
        encoded(name),
        error.error,
        extra={
            "otel.event.name": CLIENT_REJECTED,
            "event.kind": "event",
            "event.category": ["authentication"],
            "event.type": ["start"],
            "event.outcome": "failure",
            "event.action": CLIENT_REJECTED,
            "error.type": error.error,
            "grelmicro.oauth_client.name": encoded(name),
        },
    )


@cache
def _tracer() -> Any:  # noqa: ANN401
    """Return `(trace, tracer)`, or `None` when OpenTelemetry is absent."""
    try:
        from opentelemetry import trace  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return None
    return trace, trace.get_tracer("grelmicro.security")


@contextmanager
def _span(attributes: Mapping[str, str]) -> Iterator[None]:
    """Run a token request inside one client span, when tracing is installed."""
    handles = _tracer()
    if handles is None:  # pragma: no cover
        yield
        return
    trace, tracer = handles
    with tracer.start_as_current_span(
        "oauth_client.fetch",
        kind=trace.SpanKind.CLIENT,
        attributes=dict(attributes),
    ):
        yield


def _b64u(raw: bytes) -> str:
    """Return `raw` as unpadded base64url."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_json(value: Mapping[str, object]) -> str:
    """Return `value` as a base64url-encoded compact JSON segment."""
    return _b64u(json.dumps(value, separators=(",", ":")).encode())
