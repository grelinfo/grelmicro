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

Read more in the [Problem Details](../http/problems.md) docs.
"""

from grelmicro.http._component import ErrorResponses, RenderedError
from grelmicro.http._problem import (
    PROBLEM_MEDIA_TYPE,
    PROBLEM_TYPE_BASE,
    ProblemDetail,
    problem_detail,
    send_problem,
)
from grelmicro.http._tmf import TMFError

__all__ = [
    "PROBLEM_MEDIA_TYPE",
    "PROBLEM_TYPE_BASE",
    "ErrorResponses",
    "ProblemDetail",
    "RenderedError",
    "TMFError",
    "problem_detail",
    "send_problem",
]
