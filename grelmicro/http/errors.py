"""HTTP Errors."""

from grelmicro.errors import GrelmicroError


class OpsServerError(GrelmicroError):
    """Raised when the ops server cannot serve.

    The app registers nothing the server can answer, no `Grelmicro` app is
    active, or the port is taken.
    """


class PreconditionError(GrelmicroError):
    """Base error for a conditional request that could not proceed.

    Catch it to handle both halves of optimistic concurrency with one
    `except`: a precondition that failed, and one the service required and
    the client did not send.
    """


class PreconditionFailedError(PreconditionError):
    """Raised when the client's precondition does not match the resource.

    The `If-Match` header carried an entity tag that is not the one the
    resource has now, so another writer landed in between and the write
    would have overwritten their change. Answers `412`.

    Raise it yourself when the conditional write itself comes back empty,
    which is the case a check before the write cannot catch:

    ```python
    result = await session.execute(
        update(Cart)
        .where(Cart.id == cart_id, Cart.version == expected)
        .values(items=items, version=expected + 1)
    )
    if result.rowcount == 0:
        raise PreconditionFailedError
    ```
    """

    def __init__(self) -> None:
        """Initialize the error."""
        super().__init__(
            "The precondition of this request does not match the current "
            "state of the resource."
        )


class PreconditionRequiredError(PreconditionError):
    """Raised when a write that must be conditional carried no precondition.

    The service requires `If-Match` on this write, so a client that sends
    none is told to fetch the resource and come back with its entity tag,
    rather than being allowed to overwrite whatever is there. Answers
    `428`.
    """

    def __init__(self) -> None:
        """Initialize the error."""
        super().__init__(
            "This request must be conditional. Send an If-Match header "
            "carrying the entity tag of the version you are updating."
        )
