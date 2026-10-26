"""FastAPI conditional requests: `ConditionalRequest`, `Conditional`, `ConditionalRequired`.

`grelmicro.integrations.fastapi` loads this on first access to one of its
names.
"""

from typing import TYPE_CHECKING, Annotated

try:
    # FastAPI reads the dependencies' annotations at runtime, from this
    # module, so they are imported here.
    from fastapi import Depends as _Depends
    from fastapi import Header as _Header

    HAS_FASTAPI = True
except ImportError:  # pragma: no cover - the reimport test walks this
    HAS_FASTAPI = False

from typing_extensions import Doc

from grelmicro.http import PreconditionRequiredError, check_freshness
from grelmicro.http._conditional import _UNSET as _UNSET_VERSION
from grelmicro.http._conditional import (
    _check_sent_precondition,
    declare_precondition_required,
)

__all__ = ["Conditional", "ConditionalRequest", "ConditionalRequired"]


class ConditionalRequest:
    """The conditional guards, as a dependency FastAPI injects.

    The same two calls as `check_freshness` and `check_precondition`, for a
    handler that would rather have them injected than imported:

    ```python
    from grelmicro.integrations.fastapi import Conditional


    @app.patch("/carts/{cart_id}")
    async def update(cart_id: int, conditional: Conditional) -> Cart:
        cart = await load(cart_id)
        conditional.check(cart.version)
        return await save(cart)
    ```

    It is the same implementation underneath, so a service can use
    whichever reads better. The plain functions work on Starlette and
    Litestar too, where there is no dependency injection to hang this on.

    `check` reads the headers this was handed, so a route that injects it
    is guarded whether or not `ConditionalRequests()` is registered.
    `fresh` needs the component: recording a version is how the middleware
    knows what tag to put on the response, and without one there is
    nothing to record it in, so it raises `OutOfContextError`.

    Declaring it puts `If-Match` and `If-None-Match` in the schema for
    that operation alone, so Swagger offers the fields exactly where a
    handler reads them, whatever the path-based rules say.
    """

    def __init__(
        self,
        if_match: Annotated[
            str | None,
            Doc("What the client sent in `If-Match`, if anything."),
        ] = None,
        if_none_match: Annotated[
            str | None,
            Doc("What the client sent in `If-None-Match`, if anything."),
        ] = None,
    ) -> None:
        """Take what the client sent, so a handler can read it raw too."""
        self.if_match = if_match
        self.if_none_match = if_none_match

    def check(
        self,
        version: Annotated[
            object,
            Doc("What identifies the version the resource carries now."),
        ] = _UNSET_VERSION,
        *,
        etag: Annotated[
            str | None,
            Doc("An entity tag that is already one, quotes included."),
        ] = None,
        require: Annotated[
            bool,
            Doc("Answer `428` when the request carries no precondition."),
        ] = True,
    ) -> None:
        """Refuse a write whose precondition no longer holds.

        Raises:
            PreconditionFailedError: If the resource is not in the state
                the request's `If-Match` or `If-None-Match` asked for.
            PreconditionRequiredError: If `require` and none was sent.
        """
        # From the headers this was handed, not from the request scope, so
        # a route that injects it answers the same whether or not
        # `ConditionalRequests()` is registered.
        _check_sent_precondition(
            self.if_match,
            self.if_none_match,
            version,
            etag=etag,
            require=require,
        )

    def fresh(
        self,
        version: Annotated[
            object,
            Doc("What identifies the version the resource carries now."),
        ] = _UNSET_VERSION,
        *,
        etag: Annotated[
            str | None,
            Doc("An entity tag that is already one, quotes included."),
        ] = None,
    ) -> bool:
        """Answer this read with an entity tag, and say whether it changed."""
        return check_freshness(version, etag=etag)


if TYPE_CHECKING:
    # The runtime values are `Annotated` markers FastAPI reads. A checker
    # only needs to know what the handler receives.
    Conditional = ConditionalRequest
    """The conditional guards, injected.

    ```python
    from grelmicro.integrations.fastapi import Conditional


    @app.patch("/carts/{cart_id}")
    async def update(cart_id: int, conditional: Conditional) -> Cart:
        cart = await load(cart_id)
        conditional.check(cart.version)
        return await save(cart)
    ```

    A ready-made annotation, so a handler declares one word rather than
    `Annotated[ConditionalRequest, Depends(...)]`.
    """

    ConditionalRequired = ConditionalRequest
    """The conditional guards, injected, on a route that requires a precondition.

    ```python
    from grelmicro.integrations.fastapi import ConditionalRequired


    @app.put("/carts/{cart_id}")
    async def replace(cart_id: int, conditional: ConditionalRequired) -> Cart:
        cart = await load(cart_id)
        conditional.check(cart.version)
        return await save(cart)
    ```

    A request carrying neither `If-Match` nor `If-None-Match` is answered
    `428` before the handler runs. The route declares
    `precondition_required`, so the OpenAPI schema marks `If-Match`
    required on its methods, `POST` included.
    """
elif HAS_FASTAPI:

    def _conditional(
        if_match: Annotated[
            str | None,
            _Header(
                alias="If-Match",
                description=(
                    "Entity tag of the version being updated, from the "
                    "`ETag` of an earlier read. The write is refused if "
                    "the resource changed since."
                ),
            ),
        ] = None,
        if_none_match: Annotated[
            str | None,
            _Header(
                alias="If-None-Match",
                description=(
                    "Entity tag the client already holds. The service "
                    "answers `304 Not Modified` while it still matches."
                ),
            ),
        ] = None,
    ) -> ConditionalRequest:
        """Bind the guards to what this request carries.

        The headers are declared here rather than read from the scope, so
        the operation that injects this documents them, and Swagger offers
        the fields exactly where a handler reads them.
        """
        return ConditionalRequest(if_match, if_none_match)

    def _required_conditional(
        conditional: Annotated[ConditionalRequest, _Depends(_conditional)],
    ) -> ConditionalRequest:
        """Refuse a request that carries no precondition at all.

        Declared rather than called, so the schema marks `If-Match`
        required on this operation, and the client is answered `428`
        rather than a validation error.

        Raises:
            PreconditionRequiredError: If neither precondition header
                arrived. Answers `428`.
        """
        if conditional.if_match is None and conditional.if_none_match is None:
            raise PreconditionRequiredError
        return conditional

    declare_precondition_required(_required_conditional)

    Conditional = Annotated[ConditionalRequest, _Depends(_conditional)]
    ConditionalRequired = Annotated[
        ConditionalRequest, _Depends(_required_conditional)
    ]
else:  # pragma: no cover - the reimport test walks this
    Conditional = ConditionalRequest
    ConditionalRequired = ConditionalRequest
