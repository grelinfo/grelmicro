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
"""

from grelmicro.http._component import (
    ErrorResponses,
    RenderedError,
    merge_headers,
    send_error,
)
from grelmicro.http._conditional import (
    ConditionalRequests,
    ConditionalRequestsMiddleware,
    check_freshness,
    check_precondition,
    etag_of,
)
from grelmicro.http._idempotency import (
    IdempotencyMiddleware,
    IdempotentRequests,
    StoredResponse,
)
from grelmicro.http._problem import (
    ERROR_DOCS_BASE,
    PROBLEM_MEDIA_TYPE,
    ProblemDetail,
)
from grelmicro.http._tmf import TMFError
from grelmicro.http.errors import (
    PreconditionError,
    PreconditionFailedError,
    PreconditionRequiredError,
)

__all__ = [
    "ERROR_DOCS_BASE",
    "PROBLEM_MEDIA_TYPE",
    "ConditionalRequests",
    "ConditionalRequestsMiddleware",
    "ErrorResponses",
    "IdempotencyMiddleware",
    "IdempotentRequests",
    "PreconditionError",
    "PreconditionFailedError",
    "PreconditionRequiredError",
    "ProblemDetail",
    "RenderedError",
    "StoredResponse",
    "TMFError",
    "check_freshness",
    "check_precondition",
    "etag_of",
    "merge_headers",
    "send_error",
]
