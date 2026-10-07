"""Outbox configuration."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator
from typing_extensions import Doc

from grelmicro._duration import Duration, Retention

KEEP_FOREVER = "none"
"""The text `keep_delivered` reads as `None`, keep delivered rows for good."""

_BOOL_WORDS = frozenset(
    {"t", "f", "y", "n", "true", "false", "yes", "no", "on", "off"}
)
"""Bool spellings `keep_delivered` refuses, `"1"` and `"0"` aside."""

_NOT_A_BOOL = (
    "keep_delivered takes a duration, 0 to delete on delivery, or None "
    f"({KEEP_FOREVER!r} from text) to keep forever, not a bool"
)


class OutboxConfig(BaseModel, frozen=True, extra="forbid"):
    """Outbox settings.

    Plain `BaseModel` (env-free). Component defaults resolve from the
    environment under `GREL_OUTBOX_` unless fields are set directly.
    """

    table: Annotated[
        str,
        Doc("Table that stores staged messages."),
    ] = "grelmicro_outbox"
    relay: Annotated[
        bool,
        Doc("Run the background relay on this replica."),
    ] = True
    poll_interval: Annotated[
        float,
        Doc(
            "Seconds between fallback polls. The NOTIFY wake handles the fast path."
        ),
        Field(gt=0),
    ] = 1.5
    batch_size: Annotated[
        int,
        Doc("Claim ceiling per cycle, capped by free handler slots."),
        Field(gt=0),
    ] = 100
    lease_duration: Annotated[
        Duration,
        Doc(
            """
            How long a claimed message stays invisible, in whole seconds or
            as a `timedelta`. Handlers must finish within it.

            A float is refused. From text, such as an environment
            variable, it reads whole seconds (`"60"`) or an ISO 8601
            duration (`"PT0.5S"`).
            """,
        ),
    ] = timedelta(seconds=30)
    max_attempts: Annotated[
        int,
        Doc("Attempts before a message is dead-lettered."),
        Field(gt=0),
    ] = 10
    retry_base: Annotated[
        float,
        Doc("Base backoff in seconds."),
        Field(gt=0),
    ] = 1
    retry_max: Annotated[
        float,
        Doc("Maximum backoff in seconds."),
        Field(gt=0),
    ] = 300
    retry_jitter: Annotated[
        float,
        Doc("Jitter fraction applied to the backoff, from 0 to 1."),
        Field(ge=0, le=1),
    ] = 1
    concurrency: Annotated[
        int,
        Doc("Maximum handlers running at once in each relay."),
        Field(gt=0),
    ] = 50
    dead_letter: Annotated[
        bool,
        Doc(
            "Move a message to the dead state after `max_attempts`. When "
            "False, a failing message is retried forever on the backoff."
        ),
    ] = True
    keep_delivered: Annotated[
        Retention | None,
        Doc(
            """
            How long delivered rows are kept, in whole seconds or as a
            `timedelta`. The relay purges a row once it is that old. `0`
            deletes a row on delivery, and `None` keeps it for good.

            A float or a bool is refused. From text, such as an environment
            variable, it reads whole seconds (`"60"`, `"0"`), an ISO 8601
            duration (`"PT0.5S"`), or `"none"` to keep rows for good.
            """,
        ),
    ] = timedelta(0)
    auto_migrate: Annotated[
        bool,
        Doc("Create the table on first connect."),
    ] = True
    notify: Annotated[
        bool,
        Doc(
            "Use LISTEN/NOTIFY for low-latency wakeups. Disable behind PgBouncer."
        ),
    ] = True

    @field_validator("keep_delivered", mode="before")
    @classmethod
    def _read_keep_delivered(cls, value: Any) -> Any:  # noqa: ANN401
        """Read `"none"` as `None`, and refuse a bool or its spelling in text."""
        text = value.strip().lower() if isinstance(value, str) else None
        if text == KEEP_FOREVER:
            return None
        if isinstance(value, bool) or text in _BOOL_WORDS:
            raise ValueError(_NOT_A_BOOL)
        return value
