"""Problem details for grelmicro rejections."""

from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
from math import ceil
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel
from typing_extensions import Doc

from grelmicro._json import json_dumps_bytes
from grelmicro.errors import (
    AdmissionError,
    LockTimeoutError,
    WouldBlockError,
)
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

STANDARD_MEMBERS = frozenset({"type", "title", "status", "detail", "instance"})
"""The five members RFC 9457 defines, which an extension may not displace."""

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
    """One error grelmicro knows how to put on the wire.

    An empty `slug` marks an error grelmicro did not classify, which is the
    framework's own. RFC 9457 says `about:blank` for a problem with no
    specific type, and its title is then the status phrase.
    """

    slug: str
    status: int
    title: str
    detail: str | None


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

VALIDATION_FAILED = _Kind(
    slug="validation-failed",
    status=422,
    title="Validation failed",
    detail="The request did not match the shape this endpoint accepts.",
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

    An extension named like one of the five standard members is dropped
    rather than allowed to displace it. The names are RFC 9457's, and a
    body where `status` means something else is worse than one missing a
    member the caller should not have used. Dropping is also the only
    answer that does not fail while rendering a failure.
    """
    return ProblemDetail(
        type=(
            f"{PROBLEM_TYPE_BASE}#{kind.slug}" if kind.slug else "about:blank"
        ),
        title=kind.title,
        status=kind.status,
        detail=kind.detail if detail is None else detail,
        instance=instance,
        **{
            name: value
            for name, value in (extensions or {}).items()
            if name not in STANDARD_MEMBERS
        },
    )


def _wait(seconds: float) -> dict[str, float]:
    """Return the `retry_after` member, or nothing when the wait is unknown.

    A zero delay reads as "retry now", which is the opposite of what a
    refusal means, so an unknown wait carries no member and no header.

    Rounded before the test, not after. A wait under half a millisecond,
    which a limiter reports at a window boundary under load, rounded to
    `0.0` and published the very delay this refuses to publish.
    """
    wait = round(seconds, 3)
    return {"retry_after": wait} if wait > 0 else {}


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One rejection, classified once and rendered in whichever format.

    The kind says which rejection it is, and is the same whatever standard
    the body follows. `detail` and `extensions` carry what is specific to
    this occurrence.
    """

    kind: _Kind
    detail: str | None = None
    extensions: dict[str, Any] = field(default_factory=dict)


def _from_rate_limit(exc: RateLimitExceededError) -> Occurrence:
    return Occurrence(RATE_LIMIT_EXCEEDED, extensions=_wait(exc.retry_after))


def _from_circuit_breaker(exc: CircuitBreakerError) -> Occurrence:
    return Occurrence(CIRCUIT_BREAKER_OPEN, extensions=_wait(exc.retry_after))


def _from_bulkhead(exc: BulkheadFullError) -> Occurrence:  # noqa: ARG001
    return Occurrence(BULKHEAD_FULL)


def _from_would_block(exc: WouldBlockError) -> Occurrence:  # noqa: ARG001
    return Occurrence(LOCK_UNAVAILABLE)


def _from_lock_timeout(exc: LockTimeoutError) -> Occurrence:
    # The same kind as a non-blocking refusal, because a client branching on
    # the identifier wants the one fact both carry: the lock was not granted.
    # Only the sentence differs, which is what `detail` is for.
    return Occurrence(
        LOCK_UNAVAILABLE,
        detail=(
            f"Another holder still had the lock this request needs after "
            f"{exc.timeout}s of waiting."
        ),
    )


def _from_admission(exc: AdmissionError) -> Occurrence:  # noqa: ARG001
    return Occurrence(REQUEST_REFUSED)


def _from_deadline(exc: DeadlineExceededError) -> Occurrence:
    return Occurrence(DEADLINE_EXCEEDED, extensions={"timeout": exc.timeout})


def _from_conflict(exc: IdempotencyConflictError) -> Occurrence:  # noqa: ARG001
    return Occurrence(IDEMPOTENCY_KEY_REUSED)


def _from_in_flight(exc: IdempotencyWaitTimeoutError) -> Occurrence:  # noqa: ARG001
    return Occurrence(
        IDEMPOTENCY_IN_FLIGHT,
        extensions={"retry_after": _IN_FLIGHT_RETRY_AFTER},
    )


_RULES: dict[type[BaseException], Callable[[Any], Occurrence]] = {
    RateLimitExceededError: _from_rate_limit,
    CircuitBreakerError: _from_circuit_breaker,
    BulkheadFullError: _from_bulkhead,
    LockTimeoutError: _from_lock_timeout,
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
    occurrence = classify(exc)
    if occurrence is None:
        return None
    return build(
        occurrence.kind,
        detail=occurrence.detail,
        instance=instance,
        extensions=occurrence.extensions,
    )


def classify(exc: BaseException) -> Occurrence | None:
    """Return what `exc` is, or `None` when grelmicro does not render it.

    Read through the raised exception's MRO, so a subclass lands on its own
    entry when it has one and on its base otherwise. Every format renders
    from this one answer, so a rejection means the same thing whichever
    standard the body follows.
    """
    for klass in type(exc).__mro__:
        rule = _RULES.get(klass)
        if rule is not None:
            return rule(exc)
    return None


def retry_after_of(problem: ProblemDetail) -> str | None:
    """Return the `Retry-After` value a problem detail asks for."""
    return retry_after_seconds(getattr(problem, "retry_after", None))


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
    """Serialize a problem detail, dropping the members that are unset.

    Dumped in JSON mode, so an extension member that is not a JSON native
    is converted by pydantic rather than left for the encoder. An extension
    can be anything, and whether the encoder coped with a `UUID` or a
    `Decimal` otherwise depended on which JSON library was installed, which
    is a page that works in development and fails in production.
    """
    return json_dumps_bytes(problem.model_dump(mode="json", exclude_none=True))


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


def unclassified(status: int, title: str) -> _Kind:
    """Return a kind for an error the framework raised, not grelmicro.

    Carries no slug, so it renders with `about:blank` and no documentation
    URI. There is nothing to point at: the error is the application's, and
    grelmicro only reshapes it into the format the app answers in.
    """
    return _Kind(slug="", status=status, title=title, detail=None)


def phrase_of(status: int) -> str:
    """Return the reason phrase for a status, or a plain word for an unknown.

    A service may answer with a code outside the IANA registry, and a proxy
    in front of it certainly may. Looking one up must not fail while
    rendering a failure.
    """
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def retry_after_seconds(value: object) -> str | None:
    """Return the `Retry-After` value for a wait, or `None` when there is none.

    Shared by every format, so a body someone built by hand cannot fail one
    renderer and not another. HTTP takes whole seconds, so the delay is
    rounded up: rounding down invites a retry that is refused again.

    A wait that is not a number carries no header. `bool` is a number in
    Python and never a delay, so it is excluded by name.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return str(ceil(value))
