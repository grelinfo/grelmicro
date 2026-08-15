"""Security.

Checks a service runs on an inbound request. grelmicro validates what
arrives, it never issues credentials.

`TrustedProxies` names your own proxies and `resolve_client_address`
returns the address one of them vouched for, so a spoofed
`X-Forwarded-For` never becomes a rate limiter key or an audit record.
`ClientAddressMiddleware` resolves it once per request.

Read more in the [Security](../security/index.md) docs.
"""

from grelmicro.security.clientip import (
    ClientAddress,
    ClientAddressMiddleware,
    ClientAddressReason,
    TrustedProxies,
    resolve_client_address,
)

__all__ = [
    "ClientAddress",
    "ClientAddressMiddleware",
    "ClientAddressReason",
    "TrustedProxies",
    "resolve_client_address",
]
