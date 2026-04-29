from ipaddress import ip_address

from fastapi import Depends, Request

from grelmicro.health.fastapi import health_router


def from_private_network(request: Request) -> bool:
    return bool(request.client and ip_address(request.client.host).is_private)


# Hide details from everyone (default).
router = health_router()

# Show details to everyone (private /healthz only).
router = health_router(show_details=True)

# Show details when the dependency returns True.
router = health_router(show_details=Depends(from_private_network))
