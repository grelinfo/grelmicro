"""Handler registry and topic derivation."""

from __future__ import annotations

import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from grelmicro._wrapping import named
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
    when set, otherwise its class name. A model that inherits
    `__outbox_topic__` from a parent warns once with `Grelmicro046Warning`,
    since it takes its class name from 0.46.0.
    """
    if isinstance(target, str):
        return target
    topic: str = getattr(target, "__outbox_topic__", target.__name__)
    _warn_inherited_topic(target, topic)
    return topic


_CHECKED_MODELS: weakref.WeakSet[type[Any]] = weakref.WeakSet()
"""The payload models already checked for an inherited topic."""


def _warn_inherited_topic(model: type[Any], topic: str) -> None:
    """Warn once per model that inherits `__outbox_topic__` from a parent.

    A parametrized generic model such as `Event[int]` is checked as its
    generic origin `Event`. The warning reports the first line outside
    grelmicro.
    """
    metadata = getattr(model, "__pydantic_generic_metadata__", None)
    origin: type[Any] = (metadata and metadata["origin"]) or model
    if "__outbox_topic__" in vars(origin) or origin in _CHECKED_MODELS:
        return
    parent = next(
        (base for base in origin.__mro__ if "__outbox_topic__" in vars(base)),
        None,
    )
    if parent is not None:
        from grelmicro._deprecation import inherited_outbox_topic  # noqa: PLC0415

        inherited_outbox_topic(
            model=origin.__name__, parent=parent.__name__, topic=topic
        ).warn(outside=True)
    _CHECKED_MODELS.add(origin)


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
        new: dict[str, HandlerEntry] = {}
        for entry in entries:
            existing = self._handlers.get(entry.topic) or new.get(entry.topic)
            if existing is not None:
                msg = (
                    f"Topic {entry.topic!r} already has the handler "
                    f"{named(existing.fn)}, so {named(entry.fn)} is refused"
                )
                raise HandlerAlreadyRegisteredError(msg)
            new[entry.topic] = entry

    def add(self, entries: Iterable[HandlerEntry]) -> None:
        """Register every entry for its topic, once `check` passed for them."""
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
