"""Outbox message and record types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from uuid import UUID


@dataclass(frozen=True, slots=True)
class Message[T]:
    """A staged message delivered to a handler.

    `data` holds the validated payload model for a typed handler and is
    None for a topic handler. `payload` holds the raw payload dict for
    both. Use `id` as the idempotency key for the side effect.
    """

    id: UUID
    topic: str
    key: str | None
    data: T | None
    payload: Mapping[str, Any]
    headers: Mapping[str, Any]
    attempts: int


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    """The stored form of a message, exchanged between relay and backend."""

    id: UUID
    topic: str
    payload: Mapping[str, Any]
    key: str | None = None
    headers: Mapping[str, Any] = field(default_factory=dict)
    dedup_key: str | None = None
    attempts: int = 0
    available_at: datetime | None = None
