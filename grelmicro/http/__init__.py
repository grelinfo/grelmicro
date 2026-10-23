"""HTTP.

Puts grelmicro's rejections on the wire. `AdmissionError` already covers
every "turned away" case, and `ProblemDetail` renders it as the
`application/problem+json` body of RFC 9457, with the field the client
needs next: how long to wait, or that there is nothing to wait for.

Register `ErrorResponses()` to opt in, and `micro.install(app)` wires the
handler, so a rate limiter, a bulkhead, an open circuit breaker, or an
elapsed deadline answers the client without a single `except` in a route
handler. Without it grelmicro changes nothing about how your framework
answers an error.

The format comes from the factory you call, never from a variable. RFC 9457
problem details are the default, and `ErrorResponses.tmf()` renders the TM
Forum format for a service answering to a TM Forum Open API platform.

`IdempotencyMiddleware` replays a stored response when a request repeats
its idempotency key, and `IdempotentRequests()` registers it so
`micro.install(app)` adds it.

`ConditionalRequests()` answers conditional requests: it puts an `ETag` on
responses, answers `304` to a read that already holds one, and lets
`check_precondition(etag_of(version))` refuse a write whose `If-Match` no
longer matches.

Read more in the [Error Responses](../http/errors.md) and [Idempotency
Middleware](../http/idempotency.md) docs.

Each name loads its submodule on first access.
"""

import typing as _typing

if _typing.TYPE_CHECKING:
    from grelmicro.http._authentication import (
        AuthenticatedRequests,
        AuthenticatedRequestsConfig,
        AuthenticatedRequestsMiddleware,
    )
    from grelmicro.http._component import (
        ErrorResponses,
        RenderedError,
        merge_headers,
        send_error,
    )
    from grelmicro.http._conditional import (
        ConditionalRequests,
        ConditionalRequestsConfig,
        ConditionalRequestsMiddleware,
        check_freshness,
        check_precondition,
        etag_of,
    )
    from grelmicro.http._idempotency import (
        IdempotencyMiddleware,
        IdempotentRequests,
        IdempotentRequestsConfig,
        StoredResponse,
    )
    from grelmicro.http._problem import (
        ERROR_DOCS_BASE,
        PROBLEM_MEDIA_TYPE,
        ProblemDetail,
    )
    from grelmicro.http._ratelimit import (
        RateLimitedRequests,
        RateLimitedRequestsConfig,
        RateLimitMiddleware,
    )
    from grelmicro.http._response_cache import (
        CachedResponses,
        CachedResponsesConfig,
        CachedResponsesMiddleware,
    )
    from grelmicro.http._route import route_template
    from grelmicro.http._routes import Gate, RouteDeclaration
    from grelmicro.http._server import OpsServer, OpsServerConfig
    from grelmicro.http._tmf import TMFError
    from grelmicro.http.errors import (
        OpsServerError,
        PreconditionError,
        PreconditionFailedError,
        PreconditionRequiredError,
    )

__all__ = [
    "ERROR_DOCS_BASE",
    "PROBLEM_MEDIA_TYPE",
    "AuthenticatedRequests",
    "AuthenticatedRequestsConfig",
    "AuthenticatedRequestsMiddleware",
    "CachedResponses",
    "CachedResponsesConfig",
    "CachedResponsesMiddleware",
    "ConditionalRequests",
    "ConditionalRequestsConfig",
    "ConditionalRequestsMiddleware",
    "ErrorResponses",
    "Gate",
    "IdempotencyMiddleware",
    "IdempotentRequests",
    "IdempotentRequestsConfig",
    "OpsServer",
    "OpsServerConfig",
    "OpsServerError",
    "PreconditionError",
    "PreconditionFailedError",
    "PreconditionRequiredError",
    "ProblemDetail",
    "RateLimitMiddleware",
    "RateLimitedRequests",
    "RateLimitedRequestsConfig",
    "RenderedError",
    "RouteDeclaration",
    "StoredResponse",
    "TMFError",
    "check_freshness",
    "check_precondition",
    "etag_of",
    "merge_headers",
    "route_template",
    "send_error",
]

_LAZY: dict[str, str] = {
    "ERROR_DOCS_BASE": "grelmicro.http._problem",
    "PROBLEM_MEDIA_TYPE": "grelmicro.http._problem",
    "AuthenticatedRequests": "grelmicro.http._authentication",
    "AuthenticatedRequestsConfig": "grelmicro.http._authentication",
    "AuthenticatedRequestsMiddleware": "grelmicro.http._authentication",
    "CachedResponses": "grelmicro.http._response_cache",
    "CachedResponsesConfig": "grelmicro.http._response_cache",
    "CachedResponsesMiddleware": "grelmicro.http._response_cache",
    "ConditionalRequests": "grelmicro.http._conditional",
    "ConditionalRequestsConfig": "grelmicro.http._conditional",
    "ConditionalRequestsMiddleware": "grelmicro.http._conditional",
    "ErrorResponses": "grelmicro.http._component",
    "Gate": "grelmicro.http._routes",
    "IdempotencyMiddleware": "grelmicro.http._idempotency",
    "IdempotentRequests": "grelmicro.http._idempotency",
    "IdempotentRequestsConfig": "grelmicro.http._idempotency",
    "OpsServer": "grelmicro.http._server",
    "OpsServerConfig": "grelmicro.http._server",
    "OpsServerError": "grelmicro.http.errors",
    "PreconditionError": "grelmicro.http.errors",
    "PreconditionFailedError": "grelmicro.http.errors",
    "PreconditionRequiredError": "grelmicro.http.errors",
    "ProblemDetail": "grelmicro.http._problem",
    "RateLimitMiddleware": "grelmicro.http._ratelimit",
    "RateLimitedRequests": "grelmicro.http._ratelimit",
    "RateLimitedRequestsConfig": "grelmicro.http._ratelimit",
    "RenderedError": "grelmicro.http._component",
    "RouteDeclaration": "grelmicro.http._routes",
    "StoredResponse": "grelmicro.http._idempotency",
    "TMFError": "grelmicro.http._tmf",
    "check_freshness": "grelmicro.http._conditional",
    "check_precondition": "grelmicro.http._conditional",
    "etag_of": "grelmicro.http._conditional",
    "merge_headers": "grelmicro.http._component",
    "route_template": "grelmicro.http._route",
    "send_error": "grelmicro.http._component",
}
"""The submodule each public name loads from."""


def __getattr__(name: str) -> object:
    """Load `name` from its submodule and cache it on the package."""
    module = _LAZY.get(name)
    if module is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    import importlib  # noqa: PLC0415

    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the loaded attributes and every public name."""
    return sorted({*globals(), *__all__})
