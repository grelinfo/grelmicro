"""Handler registry and topic derivation."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

from grelmicro._wrapping import named
from grelmicro.outbox._codec import decode_payload
from grelmicro.outbox._message import Message
from grelmicro.outbox.errors import (
    HandlerAlreadyRegisteredError,
    HandlerNotFoundError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Hashable, Iterable

    from grelmicro._deprecation import FutureChange
    from grelmicro.outbox._message import OutboxRecord


def derive_topic(target: type[Any] | str) -> str:
    """Return the topic for a payload model or a topic string.

    A string passes through. A model uses its `__outbox_topic__` attribute
    when set, otherwise its class name. A model that inherits
    `__outbox_topic__` from a parent warns with `Grelmicro046Warning` at the
    first line outside grelmicro, since it takes its class name from 0.46.0.
    """
    if isinstance(target, str):
        return target
    if not isinstance(target, type):
        return getattr(target, "__outbox_topic__", target.__name__)
    # mypy reads `type[Any]` as unhashable, and every class is hashable.
    topic, change = _model_topic(cast("Hashable", target))
    if change is not None:
        change.warn(outside=True)
    return topic


@lru_cache(maxsize=512)
def _model_topic(model: type[Any]) -> tuple[str, FutureChange | None]:
    """Return the topic of `model`, and the change to warn for it, if any.

    A parametrized generic model such as `Event[int]` is checked as its
    generic origin `Event`, and the change names the origin. With no
    `__outbox_topic__` on `Event` or a parent, the change is the one of its
    topic, which becomes `Event` for every parametrization.
    """
    topic: str = getattr(model, "__outbox_topic__", model.__name__)
    metadata = getattr(model, "__pydantic_generic_metadata__", None)
    origin: type[Any] = (metadata and metadata["origin"]) or model
    if "__outbox_topic__" in vars(origin):
        return topic, None
    parent = next(
        (base for base in origin.__mro__ if "__outbox_topic__" in vars(base)),
        None,
    )
    if parent is None and origin is model:
        return topic, None
    from grelmicro._deprecation import (  # noqa: PLC0415
        generic_outbox_topic,
        inherited_outbox_topic,
    )

    if parent is None:
        return topic, generic_outbox_topic(model=topic, origin=origin.__name__)
    return topic, inherited_outbox_topic(
        model=origin.__name__, parent=parent.__name__, topic=topic
    )


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
