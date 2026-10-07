"""Idempotency Config."""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from pydantic import BaseModel
from typing_extensions import Doc

from grelmicro._duration import Duration


class IdempotencyConfig(BaseModel, frozen=True, extra="forbid"):
    """Frozen snapshot of the `Idempotency` declarative settings.

    Carries the settings that round-trip in serialized form. Runtime
    dependencies (`cache`, `serializer`, `fingerprint`) stay as
    constructor kwargs on `Idempotency` since they are object references
    or callables, not values.
    """

    ttl: Annotated[
        Duration,
        Doc(
            """
            Lifetime of a stored response, in whole seconds or as a
            `timedelta`. A repeated key within this window replays the
            stored response. After it elapses, the key executes fresh.

            A float is refused. From text it reads whole seconds
            (`"3600"`) or an ISO 8601 duration (`"PT1H"`). It reads back
            as a `timedelta`, and is at most 100 years.
            """,
        ),
    ] = timedelta(days=1)
