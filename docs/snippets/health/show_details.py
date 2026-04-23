from fastapi import Depends, Request

from grelmicro.health.fastapi import health_router


def require_admin(request: Request) -> None:
    # Replace with your real auth. Raise HTTPException on failure.
    return None


# Default: hide details from everyone.
router = health_router()

# Show details to everyone (only safe on private /healthz).
router = health_router(show_details=True)

# Show details only to requests that pass every listed dependency.
# A failing dependency strips details; the endpoint still returns
# 200/503 with the base fields.
router = health_router(show_details=[Depends(require_admin)])
