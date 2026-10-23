"""Security.

Checks a service runs on an inbound request, and the tokens it sends on an
outbound one. grelmicro validates what arrives and authenticates what it
sends. It never issues credentials.

`TrustedProxies` names your own proxies and `resolve_client_address`
returns the address one of them vouched for, so a spoofed
`X-Forwarded-For` never becomes a rate limiter key or an audit record.
`ClientAddressMiddleware` resolves it once per request.

`OAuthClient` registers the service with its authorization server, and
`ClientCredentials` and `TokenExchange` get the tokens it calls other APIs
with.

Read more in the [Security](../security/index.md) docs.

Each name loads its submodule on first access.
"""

import typing as _typing

if _typing.TYPE_CHECKING:
    from grelmicro.security.bans import (
        ABUSIVE_REASONS,
        ClientBannedError,
        ClientBans,
        ClientBansConfig,
    )
    from grelmicro.security.clientip import (
        ClientAddress,
        ClientAddressMiddleware,
        ClientAddressReason,
        TrustedProxies,
        resolve_client_address,
    )
    from grelmicro.security.jwks import (
        Fetcher,
        SigningKeysUnavailableError,
        fetch_with_httpx,
    )
    from grelmicro.security.jwt import (
        ALGORITHMS,
        DiscoveryConfig,
        JWKSConfig,
        JWTClaims,
        JWTKey,
        JWTKeysConfig,
        JWTPolicy,
        JWTVerifier,
        TokenRejectedError,
        TokenRejectedReason,
        TokenVerifier,
        unverified_header,
    )
    from grelmicro.security.oauth import (
        AccessToken,
        ClientAuth,
        ClientCredentials,
        ClientCredentialsConfig,
        ClientRejectedError,
        OAuthClient,
        OAuthClientConfig,
        TokenExchange,
        TokenExchangeConfig,
        TokenUnavailableError,
    )
    from grelmicro.security.principal import Principal, VerifiedToken

__all__ = [
    "ABUSIVE_REASONS",
    "ALGORITHMS",
    "AccessToken",
    "ClientAddress",
    "ClientAddressMiddleware",
    "ClientAddressReason",
    "ClientAuth",
    "ClientBannedError",
    "ClientBans",
    "ClientBansConfig",
    "ClientCredentials",
    "ClientCredentialsConfig",
    "ClientRejectedError",
    "DiscoveryConfig",
    "Fetcher",
    "JWKSConfig",
    "JWTClaims",
    "JWTKey",
    "JWTKeysConfig",
    "JWTPolicy",
    "JWTVerifier",
    "OAuthClient",
    "OAuthClientConfig",
    "Principal",
    "SigningKeysUnavailableError",
    "TokenExchange",
    "TokenExchangeConfig",
    "TokenRejectedError",
    "TokenRejectedReason",
    "TokenUnavailableError",
    "TokenVerifier",
    "TrustedProxies",
    "VerifiedToken",
    "fetch_with_httpx",
    "resolve_client_address",
    "unverified_header",
]

_LAZY: dict[str, str] = {
    "ABUSIVE_REASONS": "grelmicro.security.bans",
    "ALGORITHMS": "grelmicro.security.jwt",
    "AccessToken": "grelmicro.security.oauth",
    "ClientAddress": "grelmicro.security.clientip",
    "ClientAddressMiddleware": "grelmicro.security.clientip",
    "ClientAddressReason": "grelmicro.security.clientip",
    "ClientAuth": "grelmicro.security.oauth",
    "ClientBannedError": "grelmicro.security.bans",
    "ClientBans": "grelmicro.security.bans",
    "ClientBansConfig": "grelmicro.security.bans",
    "ClientCredentials": "grelmicro.security.oauth",
    "ClientCredentialsConfig": "grelmicro.security.oauth",
    "ClientRejectedError": "grelmicro.security.oauth",
    "DiscoveryConfig": "grelmicro.security.jwt",
    "Fetcher": "grelmicro.security.jwks",
    "JWKSConfig": "grelmicro.security.jwt",
    "JWTClaims": "grelmicro.security.jwt",
    "JWTKey": "grelmicro.security.jwt",
    "JWTKeysConfig": "grelmicro.security.jwt",
    "JWTPolicy": "grelmicro.security.jwt",
    "JWTVerifier": "grelmicro.security.jwt",
    "OAuthClient": "grelmicro.security.oauth",
    "OAuthClientConfig": "grelmicro.security.oauth",
    "Principal": "grelmicro.security.principal",
    "SigningKeysUnavailableError": "grelmicro.security.jwks",
    "TokenExchange": "grelmicro.security.oauth",
    "TokenExchangeConfig": "grelmicro.security.oauth",
    "TokenRejectedError": "grelmicro.security.jwt",
    "TokenRejectedReason": "grelmicro.security.jwt",
    "TokenUnavailableError": "grelmicro.security.oauth",
    "TokenVerifier": "grelmicro.security.jwt",
    "TrustedProxies": "grelmicro.security.clientip",
    "VerifiedToken": "grelmicro.security.principal",
    "fetch_with_httpx": "grelmicro.security.jwks",
    "resolve_client_address": "grelmicro.security.clientip",
    "unverified_header": "grelmicro.security.jwt",
}
"""The submodule each public name loads from."""


def __getattr__(name: str) -> object:
    """Load `name` from its submodule and cache it on the package."""
    module = _LAZY.get(name)
    if module is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    import importlib  # noqa: PLC0415

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the loaded attributes and every public name."""
    return sorted({*globals(), *__all__})
