"""Handler control-flow signals."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from typing_extensions import Doc


class Retry(Exception):  # noqa: N818
    """Raise from a handler to reschedule the message.

    The relay reschedules the message after `delay`, or on the configured
    backoff when `delay` is None. The attempt still counts toward
    `max_attempts`.
    """

    def __init__(
        self,
        *,
        delay: Annotated[
            float | timedelta | None,
            Doc("Time until the next attempt. None uses the backoff."),
        ] = None,
    ) -> None:
        """Initialize the signal."""
        if isinstance(delay, timedelta):
            delay = delay.total_seconds()
        self.delay = delay
        super().__init__(f"retry in {delay}s" if delay is not None else "retry")


class Cancel(Exception):  # noqa: N818
    """Raise from a handler to dead-letter the message now.

    The relay moves the message to the dead state without further
    attempts and records `reason` as its last error.
    """

    def __init__(
        self,
        reason: Annotated[
            str,
            Doc("Why the message is dead-lettered. Stored as the last error."),
        ],
    ) -> None:
        """Initialize the signal."""
        self.reason = reason
        super().__init__(reason)
