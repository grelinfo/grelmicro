"""Error response component."""

from __future__ import annotations

from dataclasses import dataclass, replace
from http import HTTPStatus
from math import ceil
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro.http import _tmf
from grelmicro.http._problem import (
    PROBLEM_MEDIA_TYPE,
    PROBLEM_TYPE_BASE,
    SAFETY_HEADERS,
    VALIDATION_FAILED,
    Occurrence,
    ProblemDetail,
    body_of,
    build,
    classify,
    framework_headers_of,
    unclassified,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from pydantic import BaseModel

__all__ = ["ErrorResponses", "RenderedError"]


@dataclass(frozen=True, slots=True)
class RenderedError:
    """One rejection, ready to send, in whichever format was chosen.

    What every integration needs and nothing more, so a framework wires the
    same three values whatever standard the body follows.
    """

    status: Annotated[int, Doc("HTTP status code of the response.")]
    media_type: Annotated[str, Doc("Content type the body is written with.")]
    headers: Annotated[
        dict[str, str],
        Doc("Headers beside the content type, `Retry-After` included."),
    ]
    body: Annotated[bytes, Doc("Serialized body.")]


def _render_problem_details(
    occurrence: Occurrence, instance: str | None
) -> RenderedError:
    """Render an occurrence as an RFC 9457 problem detail."""
    problem = build(
        occurrence.kind,
        detail=occurrence.detail,
        instance=instance,
        extensions=occurrence.extensions,
    )
    return RenderedError(
        status=problem.status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=framework_headers_of(problem),
        body=body_of(problem),
    )


def _render_tmf(
    prefix: str, reference_error: str | None
) -> Callable[[Occurrence, str | None], RenderedError]:
    """Bind a TM Forum renderer to the prefix and documentation base it uses."""

    def render(
        occurrence: Occurrence,
        instance: str | None,  # noqa: ARG001
    ) -> RenderedError:
        # TMF630 has no member for the occurrence and forbids an envelope,
        # so `instance` is accepted and dropped.
        status, error = _tmf.render(occurrence, prefix, reference_error)
        headers = dict(SAFETY_HEADERS)
        # TMF630 defines no extension member either, so the delay reaches
        # the client through the header or not at all.
        wait = occurrence.extensions.get("retry_after")
        if wait is not None:
            headers["retry-after"] = str(ceil(wait))
        return RenderedError(
            status=status,
            media_type=_tmf.TMF_MEDIA_TYPE,
            headers=headers,
            body=_tmf.body_of(error),
        )

    return render


class ErrorResponses:
    """Answer every grelmicro rejection in a standard error format.

    Register it to opt in, and `micro.install(app)` wires the handler into
    FastAPI, Starlette, or Litestar. Without it grelmicro registers nothing,
    and a rejection reaches the framework's own error handling exactly as any
    other exception does.

    A rate limiter over budget, a full bulkhead, an open circuit breaker, a
    lock held elsewhere, or an elapsed deadline then answers the client with
    a body carrying what it can act on, instead of becoming a `500`.

    The bare constructor renders [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html)
    problem details, which is the format an HTTP API is expected to speak.
    `ErrorResponses.tmf()` renders the TM Forum format instead:

    ```python
    from fastapi import FastAPI

    from grelmicro import Grelmicro
    from grelmicro.http import ErrorResponses

    micro = Grelmicro(uses=[ErrorResponses()])
    app = FastAPI()
    micro.install(app)
    ```

    A format is chosen by the factory you call, never by a variable, because
    it is the shape of the response and not a value to tune. One rendering
    answers for the whole app, so registering two is refused.

    A framework that serves no HTTP, such as FastStream, ignores it.

    Read more in the [Problem Details](../http/problems.md) docs.
    """

    kind: ClassVar[str] = "error_responses"
    singleton: ClassVar[bool] = True
    singleton_reason: ClassVar[str] = (
        "One rendering answers for the whole app, so two formats cannot both "
        "be registered"
    )

    def __init__(
        self,
        *,
        name: Annotated[
            str,
            Doc("Registration name. Only one may be registered."),
        ] = "default",
    ) -> None:
        """Render rejections as RFC 9457 problem details."""
        self._name = name
        self._from_occurrence: Callable[
            [Occurrence, str | None], RenderedError
        ] = _render_problem_details
        self._media_type = PROBLEM_MEDIA_TYPE
        self._model: type[BaseModel] = ProblemDetail

    @classmethod
    def tmf(
        cls,
        *,
        code_prefix: Annotated[
            str,
            Doc(
                """
                Namespace for the `code` member, which TMF630 makes
                mandatory and leaves to the API. An application writes its
                own business codes into the same field, so the prefix says
                which system defined this one. Set it to fold grelmicro's
                codes into an operator's catalogue.
                """
            ),
        ] = _tmf.DEFAULT_CODE_PREFIX,
        reference_error: Annotated[
            str | None,
            Doc(
                """
                Base the `referenceError` documentation URI is built on.
                Pass `None` to leave the member out, for a service whose
                responses must name no address outside it. Pass your own
                base to point at your documentation instead of grelmicro's.
                """
            ),
        ] = PROBLEM_TYPE_BASE,
        name: Annotated[
            str,
            Doc("Registration name. Only one may be registered."),
        ] = "default",
    ) -> Self:
        """Render rejections in the TM Forum error format of TMF630.

        For a service that answers to a telco or OSS platform built on TM
        Forum Open APIs, where the client expects `code` and `reason`
        rather than `type` and `title`.

        The status codes are the same ones RFC 9457 mode returns. TMF630
        mandates the IANA registry and names `422`, `429` and `503` itself,
        so nothing is remapped and `Retry-After` keeps its meaning.

        ```python
        micro = Grelmicro(uses=[ErrorResponses.tmf(code_prefix="SBB")])
        ```

        Two members do not survive the format. There is no equivalent of
        `instance`, and TMF630 defines no extension mechanism, so a
        `retry_after` reaches the client only as the `Retry-After` header.

        `reference_error=None` leaves the documentation URI out, for a
        service whose responses must name no address outside it.
        """
        instance = cls(name=name)
        instance._from_occurrence = _render_tmf(code_prefix, reference_error)
        instance._media_type = _tmf.TMF_MEDIA_TYPE
        instance._model = _tmf.TMFError
        return instance

    @classmethod
    def problem_details(
        cls,
        *,
        name: Annotated[
            str,
            Doc("Registration name. Only one may be registered."),
        ] = "default",
    ) -> Self:
        """Render rejections as RFC 9457 problem details.

        The same as the bare constructor, for a wiring that says which
        format it speaks rather than leaving it to the default.
        """
        return cls(name=name)

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    @property
    def media_type(self) -> str:
        """Return the content type every rendered error is served with."""
        return self._media_type

    @property
    def model(self) -> type[BaseModel]:
        """Return the body model, for publishing the shape in OpenAPI.

        `document_idempotency(app)` reads it, so a schema describes the
        format the app actually answers in rather than assuming one.
        """
        return self._model

    def render_status(
        self,
        status: Annotated[int, Doc("HTTP status code of the response.")],
        *,
        detail: Annotated[
            str | None,
            Doc("Explanation of this occurrence, safe to show a client."),
        ] = None,
        instance: Annotated[
            str | None,
            Doc("Request path recorded as the occurrence."),
        ] = None,
        extensions: Annotated[
            dict[str, Any] | None,
            Doc(
                "Extra members, such as the field errors of a validation failure."
            ),
        ] = None,
    ) -> RenderedError:
        """Return the response for an error the framework raised.

        The rejections grelmicro raises go through `render`, which knows
        what each one is. This renders one it does not know, so an app's own
        `HTTPException` and its request validation failures answer in the
        same format as everything else.

        The status and the message stay as the framework set them. Only the
        shape changes.
        """
        kind = unclassified(status, HTTPStatus(status).phrase)
        return self._from_occurrence(
            Occurrence(kind, detail=detail, extensions=extensions or {}),
            instance,
        )

    def render_validation(
        self,
        field_errors: Annotated[
            list[dict[str, Any]],
            Doc("One entry per part of the request that did not match."),
        ],
        *,
        status: Annotated[
            int,
            Doc("Status the framework chose. Kept, not second-guessed."),
        ],
        instance: Annotated[
            str | None,
            Doc("Request path recorded as the occurrence."),
        ] = None,
    ) -> RenderedError:
        """Return the response for a request that failed validation.

        A known kind, so a client branches on the identifier rather than on
        the status, and reads the same identifier whichever framework
        validated the request.

        The status stays the framework's. FastAPI answers `422` and Litestar
        `400`, and which is right is a question those projects have already
        answered for their users. `422` is the more precise code for a
        request that is well formed but semantically wrong, and RFC 9110
        section 15.5.21 now defines it, but grelmicro reshapes an answer
        rather than overruling it.
        """
        return self._from_occurrence(
            Occurrence(
                replace(VALIDATION_FAILED, status=status),
                extensions={"errors": field_errors},
            ),
            instance,
        )

    def render(
        self,
        exc: Annotated[BaseException, Doc("The rejection to render.")],
        *,
        instance: Annotated[
            str | None,
            Doc("Request path recorded as the occurrence."),
        ] = None,
    ) -> RenderedError | None:
        """Return the response for `exc`, or `None` when grelmicro has none.

        An error grelmicro did not raise to turn a caller away returns
        `None`, so the framework answers it as it always did.
        """
        occurrence = classify(exc)
        if occurrence is None:
            return None
        return self._from_occurrence(occurrence, instance)

    async def __aenter__(self) -> Self:
        """Open the component.

        Nothing to open. The wiring happens in `micro.install(app)`, which
        reads the registration and adds the handler to the framework before
        it serves. This is the declaration that it should.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Close the component. Nothing to close."""
        return None
