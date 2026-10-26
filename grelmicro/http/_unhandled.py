"""Where idempotency learns that the handler of a request raised.

An integration reads this on every request that raised, so it imports
nothing.
"""

__all__ = ["UNHANDLED_KEY", "Unhandled"]

UNHANDLED_KEY = "grelmicro.unhandled"
"""Where idempotency leaves the `Unhandled` of a request it may store."""


class Unhandled:
    """Whether the handler of a request raised an unhandled exception.

    An integration whose framework renders that exception into a `500`
    before the middleware around the route sees it sets `raised`, and
    idempotency then stores nothing for the request. Every copy of the
    scope the request is handed on with shares it.
    """

    __slots__ = ("raised",)

    def __init__(self) -> None:
        """Start with nothing raised."""
        self.raised = False
