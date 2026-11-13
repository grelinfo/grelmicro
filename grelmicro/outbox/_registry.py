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
    from collections.abc import Awaitable, Callable, Iterable

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

    def check(self, entries: Iterable[HandlerEntry]) -> None:
        """Raise if an entry's topic is taken, or two entries share one.

        Raises:
            HandlerAlreadyRegisteredError: If a topic is already taken, or
                two entries share one.
        """
        held = dict(self._handlers)
        for entry in entries:
            existing = held.get(entry.topic)
            if existing is not None:
                msg = (
                    f"Topic {entry.topic!r} already has the handler "
                    f"{_name(existing.fn)}, so {_name(entry.fn)} is refused"
                )
                raise HandlerAlreadyRegisteredError(msg)
            held[entry.topic] = entry

    def add(self, entries: Iterable[HandlerEntry]) -> None:
        """Register every entry for its topic, or none of them.

        Raises:
            HandlerAlreadyRegisteredError: If a topic is already taken, or
                two entries share one.
        """
        entries = list(entries)
        self.check(entries)
        self._handlers.update((entry.topic, entry) for entry in entries)

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


def _name(fn: object) -> str:
    """Return the dotted name of a handler function."""
    module = getattr(fn, "__module__", None)
    qualname = getattr(fn, "__qualname__", repr(fn))
    return f"{module}.{qualname}" if module else qualname
