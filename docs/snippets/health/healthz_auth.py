from fastapi import Depends, Request

from grelmicro.health.fastapi import health_router


def require_admin(request: Request) -> None:
    # Replace with your real auth. Raise HTTPException on failure.
    return None


# Gate /healthz behind auth. /livez and /readyz remain open for
# orchestrators and load balancers.
router = health_router(
    show_details=True,
    healthz_dependencies=[Depends(require_admin)],
)
