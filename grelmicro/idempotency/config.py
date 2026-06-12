"""Idempotency Config."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, PositiveFloat
from typing_extensions import Doc


class IdempotencyConfig(BaseModel, frozen=True, extra="forbid"):
    """Frozen snapshot of the `Idempotency` declarative settings.

    Carries the settings that round-trip in serialized form. Runtime
    dependencies (`cache`, `serializer`, `fingerprint`) stay as
    constructor kwargs on `Idempotency` since they are object references
    or callables, not values.
    """

    ttl: Annotated[
        PositiveFloat,
        Doc(
            """
            Lifetime in seconds of a stored response. A repeated key
            within this window replays the stored response. After it
            elapses, the key executes fresh.
            """,
        ),
    ] = 86400
