"""Problem details for grelmicro rejections."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel
from typing_extensions import Doc

from grelmicro._json import json_dumps_bytes
from grelmicro.errors import AdmissionError, WouldBlockError
from grelmicro.idempotency.errors import (
    IdempotencyConflictError,
    IdempotencyWaitTimeoutError,
)
from grelmicro.resilience.errors import (
    BulkheadFullError,
    CircuitBreakerError,
    DeadlineExceededError,
    RateLimitExceededError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping

    Message = MutableMapping[str, Any]
    Send = Callable[[Message], Awaitable[None]]

__all__ = [
    "PROBLEM_MEDIA_TYPE",
    "PROBLEM_TYPE_BASE",
    "ProblemDetail",
    "problem_detail",
    "send_problem",
]

PROBLEM_MEDIA_TYPE = "application/problem+json"
"""Media type every problem detail is served with, from RFC 9457."""

PROBLEM_TYPE_BASE = "https://grelmicro.grel.info/http/problems/"
"""Where a `type` URI points, one anchor per rejection.

Fixed rather than configurable. A client branches on `type`, so the
identifier has to mean the same thing whichever service emits it.
"""

_MEDIA_TYPE_HEADER = PROBLEM_MEDIA_TYPE.encode("latin-1")

SAFETY_HEADERS = (
    ("cache-control", "no-store"),
    ("x-content-type-options", "nosniff"),
)
"""Headers every problem response carries, whichever framework renders it.

`no-store` because a refusal is about one client at one moment. A shared
cache that kept a `429` would serve it to callers who are within their
budget, and one that kept a `503` would go on refusing after the
dependency recovered.

`nosniff` because the body reflects the request path back in `instance`.
The media type already says it is data, and this stops a client that
guesses otherwise from ever treating it as markup.
"""

_SAFETY_HEADERS_RAW = tuple(
    (name.encode("latin-1"), value.encode("latin-1"))
    for name, value in SAFETY_HEADERS
)

_IN_FLIGHT_RETRY_AFTER = 1.0
"""Seconds a duplicate is told to wait for an execution still running.

The wait that elapsed says nothing about how much longer the first
execution needs, so the hint is short and fixed: a client that comes back
either reads the stored response or is told to wait again.
"""


class ProblemDetail(BaseModel, extra="allow"):
    """An error response body, as defined by RFC 9457.

    The five standard members are declared, and anything else passed is kept
    as an extension member, which is where the useful part of a problem
    detail lives:

    ```python
    from grelmicro.http import ProblemDetail

    problem = ProblemDetail(
        type="https://example.com/problems/insufficient-funds",
        title="Insufficient funds",
        status=409,
        balance=30,
    )
    ```

    Declare it as a response model to publish the shape in OpenAPI:

    ```python
    @app.post("/charge", responses={429: {"model": ProblemDetail}})
    async def charge() -> Charge: ...
    ```

    Read more in the [Problem Details](../http/problems.md) docs.
    """

    type: Annotated[
        str,
        Doc(
            "URI identifying the problem kind. Stable, so a client can branch "
            "on it without reading the prose."
        ),
    ] = "about:blank"

    title: Annotated[
        str,
        Doc(
            "Short summary of the problem kind, the same for every occurrence."
        ),
    ]

    status: Annotated[int, Doc("HTTP status code of the response.")]

    detail: Annotated[
        str | None,
        Doc("Explanation of this occurrence, safe to show a client."),
    ] = None

    instance: Annotated[
        str | None,
        Doc("URI reference identifying the occurrence, the request path here."),
    ] = None


@dataclass(frozen=True, slots=True)
class _Kind:
    """One rejection grelmicro knows how to put on the wire."""

    slug: str
    status: int
    title: str
    detail: str


RATE_LIMIT_EXCEEDED = _Kind(
    slug="rate-limit-exceeded",
    status=429,
    title="Rate limit exceeded",
    detail=(
        "The client sent more requests than the rate limit allows. Wait for "
        "the interval in the Retry-After header before sending another."
    ),
)

CIRCUIT_BREAKER_OPEN = _Kind(
    slug="circuit-breaker-open",
    status=503,
    title="Circuit breaker open",
    detail=(
        "A dependency this request needs is failing, so calls to it are "
        "refused until it recovers."
    ),
)

BULKHEAD_FULL = _Kind(
    slug="bulkhead-full",
    status=503,
    title="Concurrency limit reached",
    detail=(
        "The service is already running as many of these calls at once as it "
        "allows. Nothing frees at a known time, so there is no delay to wait."
    ),
)

LOCK_UNAVAILABLE = _Kind(
    slug="lock-unavailable",
    status=503,
    title="Lock held elsewhere",
    detail=(
        "Another holder has the lock this request needs, and the request "
        "asked not to wait for it."
    ),
)

REQUEST_REFUSED = _Kind(
    slug="request-refused",
    status=503,
    title="Request refused",
    detail="The service refused the request before running it.",
)

DEADLINE_EXCEEDED = _Kind(
    slug="deadline-exceeded",
    status=504,
    title="Deadline exceeded",
    detail="The request did not finish within the deadline the service allows.",
)

IDEMPOTENCY_KEY_INVALID = _Kind(
    slug="idempotency-key-invalid",
    status=400,
    title="Idempotency key invalid",
    detail="The idempotency key is missing or malformed.",
)

IDEMPOTENCY_KEY_REUSED = _Kind(
    slug="idempotency-key-reused",
    status=422,
    title="Idempotency key reused",
    detail=(
        "This idempotency key was already used with a different request "
        "payload. Use a fresh key, or resend the original payload."
    ),
)

IDEMPOTENCY_IN_FLIGHT = _Kind(
    slug="idempotency-in-flight",
    status=409,
    title="Idempotent request in flight",
    detail=(
        "A request with this idempotency key is still running. Retry after "
        "the delay in the Retry-After header to read its response."
    ),
)

REQUEST_BODY_TOO_LARGE = _Kind(
    slug="request-body-too-large",
    status=413,
    title="Request body too large",
    detail="The request body is larger than the service reads.",
)


def build(
    kind: _Kind,
    *,
    detail: str | None = None,
    instance: str | None = None,
    extensions: dict[str, Any] | None = None,
) -> ProblemDetail:
    """Render a kind as a problem detail, with any extension members.

    Extensions arrive as a mapping rather than as keyword arguments, so a
    member named `detail` or `instance` cannot quietly take the place of
    the standard one.
    """
    return ProblemDetail(
        type=f"{PROBLEM_TYPE_BASE}#{kind.slug}",
        title=kind.title,
        status=kind.status,
        detail=kind.detail if detail is None else detail,
        instance=instance,
        **(extensions or {}),
    )


def _wait(seconds: float) -> dict[str, float]:
    """Return the `retry_after` member, or nothing when the wait is unknown.

    A zero delay reads as "retry now", which is the opposite of what a
    refusal means, so an unknown wait carries no member and no header.
    """
    return {"retry_after": round(seconds, 3)} if seconds > 0 else {}


def _from_rate_limit(
    exc: RateLimitExceededError, instance: str | None
) -> ProblemDetail:
    return build(
        RATE_LIMIT_EXCEEDED,
        instance=instance,
        extensions=_wait(exc.retry_after),
    )


def _from_circuit_breaker(
    exc: CircuitBreakerError, instance: str | None
) -> ProblemDetail:
    return build(
        CIRCUIT_BREAKER_OPEN,
        instance=instance,
        extensions=_wait(exc.retry_after),
    )


def _from_bulkhead(
    exc: BulkheadFullError,  # noqa: ARG001
    instance: str | None,
) -> ProblemDetail:
    return build(BULKHEAD_FULL, instance=instance)


def _from_would_block(
    exc: WouldBlockError,  # noqa: ARG001
    instance: str | None,
) -> ProblemDetail:
    return build(LOCK_UNAVAILABLE, instance=instance)


def _from_admission(
    exc: AdmissionError,  # noqa: ARG001
    instance: str | None,
) -> ProblemDetail:
    return build(REQUEST_REFUSED, instance=instance)


def _from_deadline(
    exc: DeadlineExceededError, instance: str | None
) -> ProblemDetail:
    return build(
        DEADLINE_EXCEEDED,
        instance=instance,
        extensions={"timeout": exc.timeout},
    )


def _from_conflict(
    exc: IdempotencyConflictError,  # noqa: ARG001
    instance: str | None,
) -> ProblemDetail:
    return build(IDEMPOTENCY_KEY_REUSED, instance=instance)


def _from_in_flight(
    exc: IdempotencyWaitTimeoutError,  # noqa: ARG001
    instance: str | None,
) -> ProblemDetail:
    return build(
        IDEMPOTENCY_IN_FLIGHT,
        instance=instance,
        extensions={"retry_after": _IN_FLIGHT_RETRY_AFTER},
    )


_RULES: dict[
    type[BaseException], Callable[[Any, str | None], ProblemDetail]
] = {
    RateLimitExceededError: _from_rate_limit,
    CircuitBreakerError: _from_circuit_breaker,
    BulkheadFullError: _from_bulkhead,
    WouldBlockError: _from_would_block,
    AdmissionError: _from_admission,
    DeadlineExceededError: _from_deadline,
    IdempotencyConflictError: _from_conflict,
    IdempotencyWaitTimeoutError: _from_in_flight,
}
"""Builder per rejection, read through the raised exception's MRO.

A subclass lands on its own entry when it has one and on its base
otherwise, so a rejection nobody anticipated still renders as one.
"""

HANDLED = (
    AdmissionError,
    DeadlineExceededError,
    IdempotencyConflictError,
    IdempotencyWaitTimeoutError,
)
"""Exception classes a framework handler is registered for.

Every other error stays unhandled on purpose. An error grelmicro did not
raise to turn a caller away is a server fault, and the framework already
answers those with a `500`.
"""


def problem_detail(
    exc: Annotated[
        BaseException,
        Doc("The exception to render."),
    ],
    *,
    instance: Annotated[
        str | None,
        Doc(
            "Request path recorded as the occurrence, usually `scope['path']`."
        ),
    ] = None,
) -> ProblemDetail | None:
    """Return the problem detail for `exc`, or `None` when grelmicro has none.

    Every rejection under `AdmissionError` maps, whichever primitive raised
    it, along with `DeadlineExceededError` and the idempotency rejections.

    The rendered `detail` is written by grelmicro and never carries the
    exception message, which can name a rate limit key, a breaker, or a
    backend.
    """
    for klass in type(exc).__mro__:
        rule = _RULES.get(klass)
        if rule is not None:
            return rule(exc, instance)
    return None


def retry_after_of(problem: ProblemDetail) -> str | None:
    """Return the `Retry-After` value, or `None` when there is nothing to wait.

    HTTP takes whole seconds there, so the delay is rounded up: rounding
    down invites a retry that is refused again.
    """
    retry_after = getattr(problem, "retry_after", None)
    return None if retry_after is None else str(ceil(retry_after))


def headers_of(problem: ProblemDetail) -> list[tuple[bytes, bytes]]:
    """Return the raw response headers a problem detail asks for."""
    headers = [(b"content-type", _MEDIA_TYPE_HEADER), *_SAFETY_HEADERS_RAW]
    retry_after = retry_after_of(problem)
    if retry_after is not None:
        headers.append((b"retry-after", retry_after.encode("latin-1")))
    return headers


def framework_headers_of(problem: ProblemDetail) -> dict[str, str]:
    """Return the headers a framework response sets beside its media type."""
    headers = dict(SAFETY_HEADERS)
    retry_after = retry_after_of(problem)
    if retry_after is not None:
        headers["retry-after"] = retry_after
    return headers


def body_of(problem: ProblemDetail) -> bytes:
    """Serialize a problem detail, dropping the members that are unset."""
    return json_dumps_bytes(problem.model_dump(exclude_none=True))


async def send_problem(
    send: Annotated[
        Send,
        Doc("The ASGI `send` callable of the request being refused."),
    ],
    problem: Annotated[
        ProblemDetail,
        Doc("The problem detail to write as the whole response."),
    ],
) -> None:
    """Write a problem detail as a complete ASGI response.

    For a middleware that refuses a request itself, before the app runs. An
    error raised inside a route handler is rendered by the exception handler
    `micro.install(app)` registers instead.
    """
    body = body_of(problem)
    headers = headers_of(problem)
    headers.append((b"content-length", str(len(body)).encode("latin-1")))
    await send(
        {
            "type": "http.response.start",
            "status": problem.status,
            "headers": headers,
        }
    )
    await send({"type": "http.response.body", "body": body})
