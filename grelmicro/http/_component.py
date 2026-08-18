"""Error response component."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import TYPE_CHECKING, Annotated, ClassVar, Self

from pydantic import BaseModel
from typing_extensions import Doc

from grelmicro.http import _tmf
from grelmicro.http._problem import (
    PROBLEM_MEDIA_TYPE,
    PROBLEM_TYPE_BASE,
    SAFETY_HEADERS,
    ProblemDetail,
    body_of,
    classify,
    framework_headers_of,
    problem_detail,
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
    exc: BaseException, instance: str | None
) -> RenderedError | None:
    """Render a rejection as an RFC 9457 problem detail."""
    problem = problem_detail(exc, instance=instance)
    if problem is None:
        return None
    return RenderedError(
        status=problem.status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=framework_headers_of(problem),
        body=body_of(problem),
    )


def _render_tmf(
    prefix: str, reference_error: str | None
) -> Callable[[BaseException, str | None], RenderedError | None]:
    """Bind a TM Forum renderer to the prefix and documentation base it uses."""

    def render(
        exc: BaseException, instance: str | None
    ) -> RenderedError | None:
        rendered = _tmf.render(exc, instance, prefix, reference_error)
        if rendered is None:
            return None
        status, error = rendered
        headers = dict(SAFETY_HEADERS)
        # TMF630 defines no extension member, so the delay reaches the
        # client through the header or not at all.
        occurrence = classify(exc)
        if occurrence is not None:  # pragma: no branch
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
        self._render: Callable[
            [BaseException, str | None], RenderedError | None
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
        instance._render = _render_tmf(code_prefix, reference_error)
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
        return self._render(exc, instance)

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
