# Security

- **Start here**: [Security guide](../security/index.md)
- **Common recipes**: `resolve_client_address(request.scope, trusted)` returns the address a trusted proxy vouched for. `ClientAddressMiddleware` resolves it once per request so every consumer reads one value. `JWTVerifier.keys(JWTKey.pem(...), audience="my-api").verify_header(header)` returns the claims of the bearer token a caller presented. `ClientCredentials("payments-api", audience="payments-api").auth()` sends a token for the service to another API, fetched from the registered `OAuthClient`.

::: grelmicro.security
    options:
      members:
        - TrustedProxies
        - ClientAddress
        - ClientAddressReason
        - ClientAddressMiddleware
        - resolve_client_address
        - JWTVerifier
        - JWTKeysConfig
        - JWTPolicy
        - JWTKey
        - JWTClaims
        - Principal
        - TokenRejectedError
        - TokenRejectedReason
        - unverified_header
        - DiscoveryConfig
        - JWKSConfig
        - SigningKeysUnavailableError
        - Fetcher
        - fetch_with_httpx
        - ClientBans
        - ClientBansConfig
        - ClientBannedError
        - OAuthClient
        - OAuthClientConfig
        - ClientAuth
        - ClientCredentials
        - ClientCredentialsConfig
        - TokenExchange
        - TokenExchangeConfig
        - AccessToken
        - VerifiedToken
        - TokenUnavailableError
        - ClientRejectedError
