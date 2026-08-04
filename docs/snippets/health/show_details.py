from fastapi import Depends, Request

from grelmicro.clientip import TrustedProxies, resolve_client_address
from grelmicro.integrations.fastapi import health_router

# Your own proxies. Required: without it a caller's own header is believed.
trusted = TrustedProxies(["10.0.0.0/8"])


def from_private_network(request: Request) -> bool:
    # Resolve rather than reading `request.client.host`. Behind a proxy the
    # raw peer is the proxy's own private address, so checking it directly
    # reports every caller as private and shows details to the public.
    client = resolve_client_address(request.scope, trusted)
    # `forwarded` means a trusted proxy vouched for this address. Without
    # it the address is the proxy's own, and every caller looks private.
    if client is None or not client.forwarded:
        return False
    return client.ip.is_private


# Hide details from everyone (default).
router = health_router()

# Show details to everyone (private /healthz only).
router = health_router(show_details=True)

# Show details when the dependency returns True.
router = health_router(show_details=Depends(from_private_network))
