"""Tests for RFC 9457 problem details."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from litestar import Litestar, get
from litestar.response import Response as LitestarResponse
from litestar.testing import TestClient as LitestarTestClient
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.status import (
    HTTP_409_CONFLICT,
    HTTP_418_IM_A_TEAPOT,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_500_INTERNAL_SERVER_ERROR,
    HTTP_503_SERVICE_UNAVAILABLE,
    HTTP_504_GATEWAY_TIMEOUT,
)
from starlette.testclient import TestClient as StarletteTestClient

from grelmicro import (
    AdmissionError,
    ComponentAlreadyRegisteredError,
    Grelmicro,
)
from grelmicro.errors import LockTimeoutError, WouldBlockError
from grelmicro.http import (
    PROBLEM_MEDIA_TYPE,
    PROBLEM_TYPE_BASE,
    ErrorResponses,
    ProblemDetail,
    problem_detail,
    send_problem,
)
from grelmicro.http._problem import body_of, retry_after_of
from grelmicro.idempotency.errors import (
    IdempotencyConflictError,
    IdempotencyKeyMakerError,
    IdempotencyWaitTimeoutError,
)
from grelmicro.integrations import faststream
from grelmicro.resilience.errors import (
    BulkheadFullError,
    CircuitBreakerError,
    DeadlineExceededError,
    RateLimitExceededError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, MutableMapping

    from starlette.requests import Request

pytestmark = [pytest.mark.timeout(5)]


def _teapot(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
    """Answer with a teapot, so an overwrite would be visible."""
    return JSONResponse({"mine": True}, status_code=HTTP_418_IM_A_TEAPOT)


class BoomError(Exception):
    """A bug in a handler, which is not a rejection."""


RETRY_AFTER = 1.4
"""Delay the rate limit rejection reports, under two whole seconds."""

DEADLINE = 2.0
"""Deadline the timeout rejection reports."""

BREAKER_WAIT = 8.0
"""Delay the open breaker reports."""


def _rate_limited() -> RateLimitExceededError:
    """Return a rejection whose message names the key it refused."""
    return RateLimitExceededError(
        key="user:top-secret", retry_after=RETRY_AFTER
    )


EXPECTED = [
    (_rate_limited(), HTTP_429_TOO_MANY_REQUESTS, "rate-limit-exceeded"),
    (
        CircuitBreakerError(name="payments", retry_after=BREAKER_WAIT),
        HTTP_503_SERVICE_UNAVAILABLE,
        "circuit-breaker-open",
    ),
    (
        BulkheadFullError(name="db", max_concurrent=4),
        HTTP_503_SERVICE_UNAVAILABLE,
        "bulkhead-full",
    ),
    (WouldBlockError("cart"), HTTP_503_SERVICE_UNAVAILABLE, "lock-unavailable"),
    (
        LockTimeoutError(name="cart", timeout=5.0),
        HTTP_503_SERVICE_UNAVAILABLE,
        "lock-unavailable",
    ),
    (
        AdmissionError("internal-backend-7"),
        HTTP_503_SERVICE_UNAVAILABLE,
        "request-refused",
    ),
    (
        DeadlineExceededError(name="db", timeout=DEADLINE),
        HTTP_504_GATEWAY_TIMEOUT,
        "deadline-exceeded",
    ),
    (
        IdempotencyConflictError(name="http", key="abc"),
        HTTP_422_UNPROCESSABLE_CONTENT,
        "idempotency-key-reused",
    ),
    (
        IdempotencyWaitTimeoutError(name="http", key="abc", timeout=10.0),
        HTTP_409_CONFLICT,
        "idempotency-in-flight",
    ),
]


@pytest.mark.parametrize(("exc", "status", "slug"), EXPECTED)
def test_every_rejection_maps_to_a_problem(
    exc: Exception, status: int, slug: str
) -> None:
    """Each rejection renders with its own stable type and status."""
    # Act
    problem = problem_detail(exc, instance="/charge")

    # Assert
    assert problem is not None
    assert problem.status == status
    assert problem.type == f"{PROBLEM_TYPE_BASE}#{slug}"
    assert problem.instance == "/charge"
    assert problem.title
    assert problem.detail


def test_an_unmapped_error_has_no_problem() -> None:
    """A server fault stays unhandled rather than dressed up as a rejection."""
    # Act & Assert
    assert problem_detail(BoomError()) is None
    assert problem_detail(IdempotencyKeyMakerError("bad key")) is None


def test_a_new_admission_subclass_is_covered() -> None:
    """MRO lookup means a rejection added later needs no registration."""

    # Arrange
    class OverQuotaError(AdmissionError):
        """A rejection grelmicro does not know about."""

    # Act
    problem = problem_detail(OverQuotaError("nope"))

    # Assert
    assert problem is not None
    assert problem.status == HTTP_503_SERVICE_UNAVAILABLE
    assert problem.type == f"{PROBLEM_TYPE_BASE}#request-refused"


@pytest.mark.parametrize("exc", [row[0] for row in EXPECTED])
def test_the_exception_message_never_reaches_the_wire(
    exc: Exception,
) -> None:
    """A rendered detail is written here, so a key or a name cannot leak."""
    # Act
    problem = problem_detail(exc)

    # Assert
    assert problem is not None
    assert str(exc) not in json.dumps(problem.model_dump())


def test_the_rate_limit_key_is_not_published() -> None:
    """The key is often a client address or a user id."""
    # Act
    problem = problem_detail(_rate_limited())

    # Assert
    assert problem is not None
    assert "top-secret" not in json.dumps(problem.model_dump())


def test_retry_after_is_carried_when_there_is_something_to_wait_for() -> None:
    """The delay is the useful half of the response."""
    # Act
    problem = problem_detail(_rate_limited())

    # Assert
    assert problem is not None
    assert problem.model_dump()["retry_after"] == RETRY_AFTER


def test_no_retry_after_when_nothing_frees_at_a_known_time() -> None:
    """A zero delay would read as "retry now", which is the opposite."""
    # Act
    full = problem_detail(BulkheadFullError(name="db", max_concurrent=4))
    forced = problem_detail(CircuitBreakerError(name="payments"))

    # Assert
    assert full is not None
    assert forced is not None
    assert "retry_after" not in full.model_dump()
    assert "retry_after" not in forced.model_dump()


def test_the_deadline_is_carried() -> None:
    """A 504 that restates the status line is worth nothing."""
    # Act
    problem = problem_detail(DeadlineExceededError(name="db", timeout=DEADLINE))

    # Assert
    assert problem is not None
    assert problem.model_dump()["timeout"] == DEADLINE


def test_extension_members_serialize_at_the_top_level() -> None:
    """RFC 9457 puts extension members beside the standard ones."""
    # Act
    problem = ProblemDetail(
        type="https://example.com/problems/insufficient-funds",
        title="Insufficient funds",
        status=HTTP_409_CONFLICT,
        balance=30,
    )

    # Assert
    assert problem.model_dump(exclude_none=True) == {
        "type": "https://example.com/problems/insufficient-funds",
        "title": "Insufficient funds",
        "status": HTTP_409_CONFLICT,
        "balance": 30,
    }


async def test_send_problem_writes_a_whole_asgi_response() -> None:
    """A middleware answers before the app runs, so it writes the response."""
    # Arrange
    sent: list[MutableMapping[str, Any]] = []

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    problem = problem_detail(_rate_limited(), instance="/charge")
    assert problem is not None

    # Act
    await send_problem(send, problem)

    # Assert
    start, body = sent
    headers = dict(start["headers"])
    assert start["status"] == HTTP_429_TOO_MANY_REQUESTS
    assert headers[b"content-type"] == PROBLEM_MEDIA_TYPE.encode()
    assert headers[b"retry-after"] == b"2"
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"content-length"] == str(len(body["body"])).encode()
    assert json.loads(body["body"])["instance"] == "/charge"


# --- The frameworks -----------------------------------------------------


@pytest.fixture
def fastapi_client() -> Iterator[TestClient]:
    """Build a FastAPI app whose handlers raise one rejection each."""
    app = FastAPI()

    @app.get("/limited")
    async def limited() -> dict[str, str]:
        raise _rate_limited()

    @app.get("/slow")
    async def slow() -> dict[str, str]:
        raise DeadlineExceededError(name="db", timeout=DEADLINE)

    @app.get("/broken")
    async def broken() -> dict[str, str]:
        raise BoomError

    Grelmicro(uses=[ErrorResponses()]).install(app)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def test_fastapi_renders_a_rejection(fastapi_client: TestClient) -> None:
    """A rejection leaves the handler as a problem detail, not a 500."""
    # Act
    response = fastapi_client.get("/limited")

    # Assert
    assert response.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.headers["retry-after"] == "2"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    body = response.json()
    assert body["type"] == f"{PROBLEM_TYPE_BASE}#rate-limit-exceeded"
    assert body["instance"] == "/limited"
    assert body["retry_after"] == RETRY_AFTER


def test_fastapi_renders_a_deadline(fastapi_client: TestClient) -> None:
    """An elapsed deadline is a 504 carrying the deadline it enforced."""
    # Act
    response = fastapi_client.get("/slow")

    # Assert
    assert response.status_code == HTTP_504_GATEWAY_TIMEOUT
    assert response.json()["timeout"] == DEADLINE
    assert "retry-after" not in response.headers


def test_fastapi_leaves_a_server_fault_alone(
    fastapi_client: TestClient,
) -> None:
    """A bug is not a rejection, so the framework still answers it."""
    # Act
    response = fastapi_client.get("/broken")

    # Assert
    assert response.status_code == HTTP_500_INTERNAL_SERVER_ERROR
    assert response.headers["content-type"] != PROBLEM_MEDIA_TYPE


def test_a_handler_you_registered_wins() -> None:
    """An explicit handler for one rejection is not overwritten."""
    # Arrange
    app = FastAPI()

    @app.get("/limited")
    async def limited() -> dict[str, str]:
        raise _rate_limited()

    Grelmicro(uses=[ErrorResponses()]).install(app)
    app.add_exception_handler(
        RateLimitExceededError,
        _teapot,
    )

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/limited")

    # Assert
    assert response.status_code == HTTP_418_IM_A_TEAPOT


def test_starlette_renders_a_rejection() -> None:
    """The Starlette integration wires the same handler."""

    # Arrange
    async def limited(request: Request) -> None:  # noqa: ARG001
        raise _rate_limited()

    app = Starlette(routes=[Route("/limited", limited)])
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with StarletteTestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/limited")

    # Assert
    assert response.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.json()["instance"] == "/limited"


def test_litestar_renders_a_rejection() -> None:
    """Litestar resolves the handler through the raised class hierarchy."""

    # Arrange
    @get("/limited")
    async def limited() -> dict[str, str]:
        raise _rate_limited()

    app = Litestar(route_handlers=[limited])
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with LitestarTestClient(app=app, raise_server_exceptions=False) as client:
        response = client.get("/limited")

    # Assert
    assert response.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    assert response.headers["retry-after"] == "2"
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["type"] == f"{PROBLEM_TYPE_BASE}#rate-limit-exceeded"
    assert body["instance"] == "/limited"


def test_litestar_leaves_a_server_fault_alone() -> None:
    """Only rejections are rendered, on Litestar as on Starlette."""

    # Arrange
    @get("/broken")
    async def broken() -> dict[str, str]:
        raise BoomError

    app = Litestar(route_handlers=[broken])
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with LitestarTestClient(app=app, raise_server_exceptions=False) as client:
        response = client.get("/broken")

    # Assert
    assert response.status_code == HTTP_500_INTERNAL_SERVER_ERROR


# --- Parity -------------------------------------------------------------


def _fastapi_rejection() -> tuple[int, dict[str, str], bytes]:
    """Answer one rejection through the FastAPI integration."""
    app = FastAPI()

    @app.get("/limited")
    async def limited() -> dict[str, str]:
        raise _rate_limited()

    Grelmicro(uses=[ErrorResponses()]).install(app)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/limited")
    return response.status_code, dict(response.headers), response.content


def _starlette_rejection() -> tuple[int, dict[str, str], bytes]:
    """Answer the same rejection through the Starlette integration."""

    async def limited(request: Request) -> None:  # noqa: ARG001
        raise _rate_limited()

    app = Starlette(routes=[Route("/limited", limited)])
    Grelmicro(uses=[ErrorResponses()]).install(app)
    with StarletteTestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/limited")
    return response.status_code, dict(response.headers), response.content


def _litestar_rejection() -> tuple[int, dict[str, str], bytes]:
    """Answer the same rejection through the Litestar integration."""

    @get("/limited")
    async def limited() -> dict[str, str]:
        raise _rate_limited()

    app = Litestar(route_handlers=[limited])
    Grelmicro(uses=[ErrorResponses()]).install(app)
    with LitestarTestClient(app=app, raise_server_exceptions=False) as client:
        response = client.get("/limited")
    return response.status_code, dict(response.headers), response.content


HTTP_FRAMEWORKS = {
    "fastapi": _fastapi_rejection,
    "starlette": _starlette_rejection,
    "litestar": _litestar_rejection,
}
"""Every framework grelmicro renders a problem detail on.

FastStream is absent because it serves no HTTP. It carries no
`install_problem_details`, so `micro.install(app)` skips it.
"""


def test_every_http_framework_answers_identically() -> None:
    """The same rejection is the same response, whichever framework serves it.

    Not merely the same status and shape. The status line, every header, and
    the body byte for byte, so a client cannot tell which framework answered
    and a service can move between them without its callers noticing.
    """
    # Act
    answers = {name: rejection() for name, rejection in HTTP_FRAMEWORKS.items()}

    # Assert
    assert len(answers) == len(HTTP_FRAMEWORKS)
    reference = answers["fastapi"]
    for name, answer in answers.items():
        status, headers, body = answer
        expected_status, expected_headers, expected_body = reference
        assert status == expected_status, name
        assert body == expected_body, name
        assert headers == expected_headers, name


def test_faststream_takes_no_problem_details() -> None:
    """A framework that serves no HTTP is skipped, not special-cased by name."""
    # Assert
    assert not hasattr(faststream, "install_problem_details")


# --- What the review found ----------------------------------------------


def test_a_handler_registered_before_install_wins() -> None:
    """Build the app, register handlers, wire grelmicro is the natural order.

    Overwriting there would take a handler away from an app that upgrades,
    without saying so.
    """
    # Arrange
    app = FastAPI()

    @app.get("/limited")
    async def limited() -> dict[str, str]:
        raise _rate_limited()

    app.add_exception_handler(AdmissionError, _teapot)
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/limited")

    # Assert
    assert response.status_code == HTTP_418_IM_A_TEAPOT


def test_litestar_keeps_a_handler_passed_to_the_constructor() -> None:
    """`Litestar(exception_handlers=...)` wins the same way."""

    # Arrange
    @get("/limited")
    async def limited() -> dict[str, str]:
        raise _rate_limited()

    def mine(request: object, exc: Exception) -> LitestarResponse[bytes]:  # noqa: ARG001
        return LitestarResponse(content=b"", status_code=HTTP_418_IM_A_TEAPOT)

    app = Litestar(
        route_handlers=[limited],
        exception_handlers={AdmissionError: mine},
    )
    Grelmicro(uses=[ErrorResponses()]).install(app)

    # Act
    with LitestarTestClient(app=app, raise_server_exceptions=False) as client:
        response = client.get("/limited")

    # Assert
    assert response.status_code == HTTP_418_IM_A_TEAPOT


def test_an_extension_member_that_is_not_json_native_still_serializes() -> None:
    """Extensions are open, so the body cannot depend on the JSON library.

    Whether the encoder coped with a `Decimal` otherwise depended on which
    one was installed, which is a page that works in development and fails
    in production.
    """
    # Arrange
    problem = ProblemDetail(
        title="Insufficient funds",
        status=HTTP_409_CONFLICT,
        balance=Decimal("30.00"),
        ref=UUID("f9144aa2-d19d-4ef6-94f2-5606eb268960"),
    )

    # Act
    body = json.loads(body_of(problem))

    # Assert
    assert body["balance"] == "30.00"
    assert body["ref"] == "f9144aa2-d19d-4ef6-94f2-5606eb268960"


def test_a_sub_millisecond_wait_carries_nothing() -> None:
    """Rounding a tiny wait to zero would publish the "retry now" refused.

    A limiter reports one at a window boundary under load.
    """
    # Act
    problem = problem_detail(
        RateLimitExceededError(key="k", retry_after=0.0004)
    )

    # Assert
    assert problem is not None
    assert "retry_after" not in problem.model_dump()
    assert retry_after_of(problem) is None


def test_a_retry_after_that_is_not_a_number_carries_no_header() -> None:
    """A refusal must not fail while refusing."""
    # Act & Assert
    assert (
        retry_after_of(
            ProblemDetail(
                title="t", status=HTTP_429_TOO_MANY_REQUESTS, retry_after="30"
            )
        )
        is None
    )
    assert (
        retry_after_of(
            ProblemDetail(
                title="t", status=HTTP_429_TOO_MANY_REQUESTS, retry_after=True
            )
        )
        is None
    )


def test_both_lock_refusals_share_a_type_and_differ_in_detail() -> None:
    """A client branches on the fact they share, and reads which happened.

    Not getting the lock is one outcome, so it is one `type`. Whether the
    call refused to wait or waited and ran out is per-occurrence, which is
    what `detail` is for.
    """
    # Act
    refused = problem_detail(WouldBlockError("cart"))
    waited = problem_detail(LockTimeoutError(name="cart", timeout=5.0))

    # Assert
    assert refused is not None
    assert waited is not None
    assert refused.type == waited.type
    assert refused.detail != waited.detail
    assert "5.0s" in (waited.detail or "")
    # Neither carries a delay: a lock frees when its holder is done.
    assert "retry_after" not in waited.model_dump()


# --- The component ------------------------------------------------------


def test_the_component_renders_a_rejection() -> None:
    """`render` is what an integration calls, whatever the format."""
    # Act
    rendered = ErrorResponses().render(_rate_limited(), instance="/charge")

    # Assert
    assert rendered is not None
    assert rendered.status == HTTP_429_TOO_MANY_REQUESTS
    assert rendered.media_type == PROBLEM_MEDIA_TYPE
    assert rendered.headers["retry-after"] == "2"
    assert json.loads(rendered.body)["instance"] == "/charge"


def test_the_component_renders_nothing_for_a_server_fault() -> None:
    """An error grelmicro did not raise stays the framework's to answer."""
    # Act & Assert
    assert ErrorResponses().render(BoomError()) is None


def test_the_factory_and_the_bare_constructor_agree() -> None:
    """`ErrorResponses()` is RFC 9457, said or unsaid."""
    # Act
    bare = ErrorResponses().render(_rate_limited())
    named = ErrorResponses.problem_details().render(_rate_limited())

    # Assert
    assert bare == named


def test_two_formats_cannot_both_be_registered() -> None:
    """One rendering answers for the whole app.

    The exclusion rides on the shared `kind` and the `singleton` flag, and
    the message says the reason that applies rather than the process-global
    one the guard was first written for.
    """
    # Act & Assert
    with pytest.raises(
        ComponentAlreadyRegisteredError, match="One rendering answers"
    ):
        Grelmicro(uses=[ErrorResponses(), ErrorResponses(name="second")])
