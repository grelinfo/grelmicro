"""Outbox configuration."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field
from typing_extensions import Doc


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
        float,
        Doc(
            "Seconds a claimed message stays invisible. Handlers must finish within it."
        ),
        Field(gt=0),
    ] = 30
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
        bool,
        Doc("Keep delivered rows instead of deleting them."),
    ] = False
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
