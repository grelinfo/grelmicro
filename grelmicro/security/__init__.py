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
    JWKSConfig,
    JWKSFetcher,
    JWKSUnavailableError,
    JWKSVerifier,
    fetch_with_httpx,
)
from grelmicro.security.jwt import (
    ALGORITHMS,
    JWTClaims,
    JWTConfig,
    JWTKey,
    JWTPolicy,
    JWTVerifier,
    TokenRejectedError,
    TokenVerifier,
)

__all__ = [
    "ABUSIVE_REASONS",
    "ALGORITHMS",
    "ClientAddress",
    "ClientAddressMiddleware",
    "ClientAddressReason",
    "ClientBannedError",
    "ClientBans",
    "ClientBansConfig",
    "JWKSConfig",
    "JWKSFetcher",
    "JWKSUnavailableError",
    "JWKSVerifier",
    "JWTClaims",
    "JWTConfig",
    "JWTKey",
    "JWTPolicy",
    "JWTVerifier",
    "TokenRejectedError",
    "TokenVerifier",
    "TrustedProxies",
    "fetch_with_httpx",
    "resolve_client_address",
]
