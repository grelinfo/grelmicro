"""Tests for the TM Forum error format.

TMF630 was read directly to settle three things these tests hold. The status
codes are the same ones RFC 9457 mode returns, because TMF630 mandates the
IANA registry and names `422`, `429` and `503` itself. The `code` member is
mandatory and its values are left to the API. And `referenceError` is the
only place a documentation URI belongs.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_429_TOO_MANY_REQUESTS,
    HTTP_503_SERVICE_UNAVAILABLE,
    HTTP_504_GATEWAY_TIMEOUT,
)

from grelmicro import Grelmicro
from grelmicro.http import ERROR_DOCS_BASE, ErrorResponses, TMFError
from grelmicro.http._tmf import TMF_MEDIA_TYPE
from grelmicro.resilience.errors import (
    BulkheadFullError,
    DeadlineExceededError,
    RateLimitExceededError,
)

pytestmark = [pytest.mark.timeout(5)]

RETRY_AFTER = 1.4
"""Delay the rate limit rejection reports."""


def _rate_limited() -> RateLimitExceededError:
    """Return a rejection whose message names the key it refused."""
    return RateLimitExceededError(
        key="user:top-secret", retry_after=RETRY_AFTER
    )


def _body(component: ErrorResponses, exc: BaseException) -> dict[str, object]:
    """Render `exc` and return its decoded body."""
    rendered = component.render(exc, instance="/charge")
    assert rendered is not None
    return json.loads(rendered.body)  # type: ignore[no-any-return]


def test_the_body_follows_tmf630() -> None:
    """The mandatory members are there, under TMF's names."""
    # Act
    body = _body(ErrorResponses.tmf(), _rate_limited())

    # Assert
    assert body == {
        "code": "GREL-RATE-LIMIT-EXCEEDED",
        "reason": "Rate limit exceeded",
        "message": (
            "The client sent more requests than the rate limit allows. Wait "
            "for the interval in the Retry-After header before sending another."
        ),
        "referenceError": (f"{ERROR_DOCS_BASE}#rate-limit-exceeded"),
        "@type": "Error",
    }


def test_the_media_type_is_plain_json() -> None:
    """TMF630 has no media type of its own and forbids an envelope."""
    # Act
    rendered = ErrorResponses.tmf().render(_rate_limited())

    # Assert
    assert rendered is not None
    assert rendered.media_type == TMF_MEDIA_TYPE


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (_rate_limited(), HTTP_429_TOO_MANY_REQUESTS),
        (
            BulkheadFullError(name="db", max_concurrent=4),
            HTTP_503_SERVICE_UNAVAILABLE,
        ),
        (
            DeadlineExceededError(name="db", timeout=2.0),
            HTTP_504_GATEWAY_TIMEOUT,
        ),
    ],
)
def test_the_status_is_the_same_in_both_formats(
    exc: BaseException, status: int
) -> None:
    """TMF630 mandates the IANA registry, so nothing is remapped.

    Collapsing a `429` into a code from a shorter list would destroy the
    `Retry-After` contract a client acts on.
    """
    # Act
    tmf = ErrorResponses.tmf().render(exc)
    rfc = ErrorResponses().render(exc)

    # Assert
    assert tmf is not None
    assert rfc is not None
    assert tmf.status == rfc.status == status


def test_the_delay_survives_as_a_header() -> None:
    """TMF630 defines no extension member, so the header is the only place."""
    # Act
    rendered = ErrorResponses.tmf().render(_rate_limited())

    # Assert
    assert rendered is not None
    assert rendered.headers["retry-after"] == "2"
    assert "retry_after" not in json.loads(rendered.body)


def test_the_code_prefix_is_configurable() -> None:
    """An operator folds grelmicro's codes into their own catalogue."""
    # Act
    body = _body(ErrorResponses.tmf(code_prefix="SBB"), _rate_limited())

    # Assert
    assert body["code"] == "SBB-RATE-LIMIT-EXCEEDED"


def test_the_reference_error_can_be_left_out() -> None:
    """A service whose responses must name no address outside it."""
    # Act
    body = _body(ErrorResponses.tmf(reference_error=None), _rate_limited())

    # Assert
    assert "referenceError" not in body


def test_the_reference_error_can_point_at_your_own_docs() -> None:
    """The URI is the operator's to choose, the code stays grelmicro's."""
    # Act
    body = _body(
        ErrorResponses.tmf(reference_error="https://docs.example.com/errors/"),
        _rate_limited(),
    )

    # Assert
    assert body["referenceError"] == (
        "https://docs.example.com/errors/#rate-limit-exceeded"
    )


def test_the_exception_message_never_reaches_the_wire() -> None:
    """The rule holds in every format, not just RFC 9457."""
    # Arrange
    exc = _rate_limited()

    # Act
    body = _body(ErrorResponses.tmf(), exc)

    # Assert
    assert str(exc) not in json.dumps(body)
    assert "top-secret" not in json.dumps(body)


def test_a_server_fault_is_still_left_alone() -> None:
    """Only rejections render, whichever format is registered."""

    # Arrange
    class BoomError(Exception):
        """A bug in a handler."""

    # Act & Assert
    assert ErrorResponses.tmf().render(BoomError()) is None


def test_the_format_reaches_the_wire_through_install() -> None:
    """Registering the TMF factory is all it takes."""
    # Arrange
    app = FastAPI()

    @app.get("/limited")
    async def limited() -> dict[str, str]:
        raise _rate_limited()

    Grelmicro(uses=[ErrorResponses.tmf()]).install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/limited")

    # Assert
    assert response.status_code == HTTP_429_TOO_MANY_REQUESTS
    assert response.headers["content-type"] == TMF_MEDIA_TYPE
    assert response.headers["retry-after"] == "2"
    assert response.json()["code"] == "GREL-RATE-LIMIT-EXCEEDED"


def test_the_model_publishes_the_tmf_names() -> None:
    """A generated client decodes `code` and `reason`, not `type` and `title`."""
    # Act
    schema = TMFError.model_json_schema(by_alias=True)

    # Assert
    assert set(schema["required"]) == {"code", "reason"}
    assert "referenceError" in schema["properties"]
    assert "@type" in schema["properties"]


def test_the_component_publishes_the_shape_it_answers_in() -> None:
    """`document_idempotency` reads these, so a schema matches the wire."""
    # Act
    rfc = ErrorResponses()
    tmf = ErrorResponses.tmf()

    # Assert
    assert tmf.media_type == TMF_MEDIA_TYPE
    assert tmf.model is TMFError
    assert rfc.media_type != tmf.media_type
    assert rfc.model is not tmf.model


# --- What the second review found ---------------------------------------


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        (["bad", "worse"], "bad, worse"),
        ({"errors": "oops"}, "oops"),
        ([{"field": "amount"}], "{'field': 'amount'}"),
        ([{"loc": ["body", "amount"], "msg": "nope"}], "body.amount: nope"),
    ],
)
def test_any_structured_detail_renders_in_tmf(
    detail: object, expected: str
) -> None:
    """`errors` carries whatever a handler wrote, not only field errors.

    TMF has no member for a list, so the entries are read into `message`.
    Reading them must not fail while rendering a failure.
    """
    # Arrange
    app = FastAPI()

    @app.get("/boom")
    async def boom() -> dict[str, str]:
        raise HTTPException(HTTP_400_BAD_REQUEST, detail=detail)

    Grelmicro(uses=[ErrorResponses.tmf()]).install(app)

    # Act
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")

    # Assert
    assert response.status_code == HTTP_400_BAD_REQUEST
    assert response.json()["message"] == expected
