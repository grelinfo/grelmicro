# Client IP

- **Start here**: [Client IP guide](../clientip.md)
- **Common recipes**: `resolve_client_address(request.scope, trusted)` returns the address a trusted proxy vouched for. `ClientAddressMiddleware` resolves it once per request so every consumer reads one value.

::: grelmicro.clientip
    options:
      members:
        - TrustedProxies
        - ClientAddress
        - ClientAddressReason
        - ClientAddressMiddleware
        - resolve_client_address
