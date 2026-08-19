"""RFC 9457 problem details."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel
from typing_extensions import Doc

from grelmicro._json import json_dumps_bytes
from grelmicro.http._kinds import (
    DOCS_BASE,
    SAFETY_HEADERS,
    Kind,
    retry_after_seconds,
)

__all__ = [
    "ERROR_DOCS_BASE",
    "PROBLEM_MEDIA_TYPE",
    "ProblemDetail",
]

PROBLEM_MEDIA_TYPE = "application/problem+json"
"""Media type every problem detail is served with, from RFC 9457."""

ERROR_DOCS_BASE = DOCS_BASE
"""Where a `type` URI points, one anchor per rejection."""

STANDARD_MEMBERS = frozenset({"type", "title", "status", "detail", "instance"})
"""The five members RFC 9457 defines, which an extension may not displace."""


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

    Read more in the [Error Responses](../http/errors.md) docs.
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


def build(
    kind: Kind,
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
        type=(f"{ERROR_DOCS_BASE}#{kind.slug}" if kind.slug else "about:blank"),
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


def retry_after_of(problem: ProblemDetail) -> str | None:
    """Return the `Retry-After` value a problem detail asks for."""
    return retry_after_seconds(getattr(problem, "retry_after", None))


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
