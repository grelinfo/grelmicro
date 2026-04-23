from fastapi import Depends, Request

from grelmicro.health.fastapi import health_router


def require_admin(request: Request) -> None:
    # Replace with your real auth. Raise HTTPException on failure.
    return None


# Default: details always stripped. Use ?details=true to show per-request.
router = health_router()

# Always include details in the response.
router = health_router(show_details="always")

# Include details only when the request passes `details_dependencies`.
router = health_router(
    show_details="when-authorized",
    details_dependencies=[Depends(require_admin)],
)
