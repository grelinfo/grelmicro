"""HTTP.

Puts grelmicro's rejections on the wire. `AdmissionError` already covers
every "turned away" case, and `ProblemDetail` renders it as the
`application/problem+json` body of RFC 9457, with the field the client
needs next: how long to wait, or that there is nothing to wait for.

Register `ProblemDetails()` to opt in, and `micro.install(app)` wires the
handler, so a rate limiter, a bulkhead, an open circuit breaker, or an
elapsed deadline answers the client without a single `except` in a route
handler. Without it grelmicro changes nothing about how your framework
answers an error.

Read more in the [Problem Details](../http/problems.md) docs.
"""

from grelmicro.http._component import ProblemDetails
from grelmicro.http._problem import (
    PROBLEM_MEDIA_TYPE,
    PROBLEM_TYPE_BASE,
    ProblemDetail,
    problem_detail,
    send_problem,
)

__all__ = [
    "PROBLEM_MEDIA_TYPE",
    "PROBLEM_TYPE_BASE",
    "ProblemDetail",
    "ProblemDetails",
    "problem_detail",
    "send_problem",
]
