"""TM Forum error representation, from TMF630 REST API Design Guidelines."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import Doc

from grelmicro._json import json_dumps_bytes

if TYPE_CHECKING:
    from grelmicro.http._problem import Occurrence

__all__ = ["DEFAULT_CODE_PREFIX", "TMF_MEDIA_TYPE", "TMFError", "code_of"]

TMF_MEDIA_TYPE = "application/json"
"""Media type a TM Forum error is served with.

TMF630 has no media type of its own for errors and forbids an envelope, so
the body is plain JSON. This is the one visible difference from RFC 9457
that a client selects on.
"""

DEFAULT_CODE_PREFIX = "GREL"
"""Namespace prefix for the codes grelmicro defines.

TMF630 makes `code` mandatory and leaves its values to the API, so an
application writes its own business codes into the same field. The prefix
says which system defined this one, and reads in a log line without a
lookup table.
"""


class TMFError(BaseModel):
    """An error response body, as defined by TMF630.

    Declare it as a response model to publish the shape in OpenAPI:

    ```python
    @app.post("/charge", responses={429: {"model": TMFError}})
    async def charge() -> Charge: ...
    ```

    Read more in the [Problem Details](../http/problems.md) docs.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    code: Annotated[
        str,
        Doc(
            "Application code for the error, namespaced by the prefix the "
            "component was built with."
        ),
    ]

    reason: Annotated[
        str,
        Doc("Short summary of the error kind, safe to show a client user."),
    ]

    message: Annotated[
        str | None,
        Doc("Explanation of this occurrence and what to do about it."),
    ] = None

    reference_error: Annotated[
        str | None,
        Doc("URI of the documentation describing this error kind."),
    ] = Field(default=None, alias="referenceError")

    type_: Annotated[
        str,
        Doc("Class type of the representation, `Error` for this one."),
    ] = Field(default="Error", alias="@type")


def code_of(occurrence: Occurrence, prefix: str) -> str:
    """Return the TMF code for a rejection.

    Derived from the slug that already identifies the kind, so there is no
    second catalogue of identifiers to mint, freeze, and keep in step with
    the first. The slug is already public in every `referenceError` URI.
    """
    kind = occurrence.kind
    if not kind.slug:
        # An error grelmicro did not classify is the application's, so it
        # gets no grelmicro namespace. TMF641 uses the status here.
        return str(kind.status)
    return f"{prefix}-{kind.slug.upper()}"


def render(
    occurrence: Occurrence,
    prefix: str,
    reference_error: str | None,
) -> tuple[int, TMFError]:
    """Return the status and the TM Forum body for one occurrence.

    A `retry_after` reaches the client only through the `Retry-After`
    header. TMF630 defines no extension mechanism for the body, so there is
    nowhere to restate it.

    `reference_error` is the base the documentation URI is built on, or
    `None` to leave the member out entirely.
    """
    kind = occurrence.kind
    return kind.status, TMFError(
        code=code_of(occurrence, prefix),
        reason=kind.title,
        message=_message(occurrence, kind.detail),
        reference_error=(
            f"{reference_error}#{kind.slug}"
            if reference_error is not None and kind.slug
            else None
        ),
    )


def _message(occurrence: Occurrence, default: str | None) -> str | None:
    """Return the `message` member, with any field errors folded in.

    TMF630 defines no extension member, so an `errors` list has nowhere of
    its own to go. `message` is the member for "more details and corrective
    actions related to the error which can be shown to a client user",
    which is what those entries are, so they are read into it rather than
    dropped.
    """
    text = occurrence.detail or default or None
    entries = occurrence.extensions.get("errors")
    if not entries:
        return text
    parts = [
        f"{'.'.join(str(part) for part in entry.get('loc', ())) or 'request'}"
        f": {entry.get('msg', '')}"
        for entry in entries
    ]
    listed = "; ".join(parts)
    return f"{text} {listed}" if text else listed


def body_of(error: TMFError) -> bytes:
    """Serialize a TM Forum error, dropping the members that are unset."""
    return json_dumps_bytes(
        error.model_dump(mode="json", by_alias=True, exclude_none=True)
    )
