"""Type assertions for the names the integration modules serve until 0.45.0.

Each name loads from its home with a warning at runtime, and type checkers
read the same object as its home module.
"""

from grelmicro import http, resilience
from grelmicro.integrations.fastapi import RateLimiter
from grelmicro.integrations.litestar import RouteDeclaration
from grelmicro.integrations.starlette import ErrorResponses


def moved_names() -> None:
    """Read a moved name with its home's type."""
    limiter: type[resilience.RateLimiter] = RateLimiter
    declaration: type[http.RouteDeclaration] = RouteDeclaration
    errors: type[http.ErrorResponses] = ErrorResponses
    print(limiter, declaration, errors)  # noqa: T201
