# Security

- **Start here**: [Security guide](../security/index.md)
- **Common recipes**: `resolve_client_address(request.scope, trusted)` returns the address a trusted proxy vouched for. `ClientAddressMiddleware` resolves it once per request so every consumer reads one value. `JWTVerifier(JWTConfig(...)).verify_header(header)` returns the claims of the bearer token a caller presented.

::: grelmicro.security
    options:
      members:
        - TrustedProxies
        - ClientAddress
        - ClientAddressReason
        - ClientAddressMiddleware
        - resolve_client_address
        - JWTVerifier
        - JWTConfig
        - JWTPolicy
        - JWTKey
        - JWTClaims
        - TokenRejectedError
        - JWKSVerifier
        - JWKSConfig
        - JWKSFetcher
        - JWKSUnavailableError
        - fetch_with_httpx
        - ClientBans
        - ClientBansConfig
        - ClientBannedError
