"""Security.

Checks a service runs on an inbound request. grelmicro validates what
arrives, it never issues credentials.

`TrustedProxies` names your own proxies and `resolve_client_address`
returns the address one of them vouched for, so a spoofed
`X-Forwarded-For` never becomes a rate limiter key or an audit record.
`ClientAddressMiddleware` resolves it once per request.

Read more in the [Security](../security/index.md) docs.
"""

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
from grelmicro.security.principal import Principal

__all__ = [
    "ABUSIVE_REASONS",
    "ALGORITHMS",
    "ClientAddress",
    "ClientAddressMiddleware",
    "ClientAddressReason",
    "ClientBannedError",
    "ClientBans",
    "ClientBansConfig",
    "DiscoveryConfig",
    "Fetcher",
    "JWKSConfig",
    "JWTClaims",
    "JWTKey",
    "JWTKeysConfig",
    "JWTPolicy",
    "JWTVerifier",
    "Principal",
    "SigningKeysUnavailableError",
    "TokenRejectedError",
    "TokenRejectedReason",
    "TokenVerifier",
    "TrustedProxies",
    "fetch_with_httpx",
    "resolve_client_address",
    "unverified_header",
]
