"""Handler registry and topic derivation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from grelmicro.outbox._codec import decode_payload
from grelmicro.outbox._message import Message
from grelmicro.outbox.errors import (
    HandlerAlreadyRegisteredError,
    HandlerNotFoundError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from grelmicro.outbox._message import OutboxRecord


def derive_topic(target: type[Any] | str) -> str:
    """Return the topic for a payload model or a topic string.

    A string passes through. A model uses its `__outbox_topic__` attribute
    when set, otherwise its class name.
    """
    if isinstance(target, str):
        return target
    return getattr(target, "__outbox_topic__", target.__name__)


@dataclass(frozen=True, slots=True)
class HandlerEntry:
    """A registered handler and its optional payload model."""

    topic: str
    fn: Callable[[Message[Any]], Awaitable[None]]
    model: type[Any] | None


class OutboxRegistry:
    """Maps topics to handlers and builds validated messages."""

    def __init__(self) -> None:
        """Initialize an empty registry."""
        self._handlers: dict[str, HandlerEntry] = {}

    def register(
        self,
        target: type[Any] | str,
        fn: Callable[[Message[Any]], Awaitable[None]],
        *,
        topic: str | None = None,
    ) -> None:
        """Register `fn` for a payload model or a topic string.

        Raises:
            HandlerAlreadyRegisteredError: If the topic is already taken.
        """
        resolved = topic or derive_topic(target)
        if resolved in self._handlers:
            msg = f"Topic {resolved!r} already has a handler"
            raise HandlerAlreadyRegisteredError(msg)
        model = target if isinstance(target, type) else None
        self._handlers[resolved] = HandlerEntry(resolved, fn, model)

    def topics(self) -> list[str]:
        """Return every registered topic."""
        return list(self._handlers)

    def get(self, topic: str) -> HandlerEntry:
        """Return the handler entry for a topic.

        Raises:
            HandlerNotFoundError: If no handler is registered for the topic.
        """
        entry = self._handlers.get(topic)
        if entry is None:
            msg = f"No handler registered for topic {topic!r}"
            raise HandlerNotFoundError(msg)
        return entry

    def build_message(self, record: OutboxRecord) -> Message[Any]:
        """Validate a record's payload and build the handler `Message`."""
        entry = self.get(record.topic)
        data = (
            decode_payload(entry.model, record.payload)
            if entry.model is not None
            else None
        )
        return Message(
            id=record.id,
            topic=record.topic,
            key=record.key,
            data=data,
            payload=record.payload,
            headers=record.headers,
            attempts=record.attempts,
        )
