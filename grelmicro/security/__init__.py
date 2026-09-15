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
