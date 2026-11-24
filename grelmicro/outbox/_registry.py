"""Handler registry and topic derivation."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from grelmicro._wrapping import named
from grelmicro.outbox._codec import decode_payload
from grelmicro.outbox._message import Message
from grelmicro.outbox.errors import (
    HandlerAlreadyRegisteredError,
    HandlerNotFoundError,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from grelmicro._deprecation import FutureChange
    from grelmicro.outbox._message import OutboxRecord


def derive_topic(target: type[Any] | str) -> str:
    """Return the topic for a payload model or a topic string.

    A string passes through. A model uses its `__outbox_topic__` attribute
    when set, otherwise its class name, read at each call. A model that
    inherits `__outbox_topic__` from a parent warns with `Grelmicro046Warning`
    at the first line outside grelmicro, since it takes its class name from
    0.46.0.
    """
    if isinstance(target, str):
        return target
    if not isinstance(target, type):
        return getattr(target, "__outbox_topic__", target.__name__)
    read = (
        type.__getattribute__ if type(target) in _PLAIN_METACLASSES else getattr
    )
    try:
        topic: str = read(target, "__outbox_topic__")
    except AttributeError:
        topic = target.__name__
        change = _named_change(target)
    else:
        if "__outbox_topic__" in target.__dict__:
            return topic
        change = _inherited_change(target, topic)
    if change is not None:
        change.warn(outside=True)
    return topic


_PLAIN_METACLASSES = (type, type(BaseModel))
"""Metaclasses whose `__outbox_topic__` reads the same through
`type.__getattribute__` as through `getattr`. Any other metaclass reads it
through `getattr`."""


def _origin(model: type[Any]) -> type[Any]:
    """Return the generic origin of `model`, such as `Event` of `Event[int]`.

    A model that is no parametrized generic is its own origin.
    """
    metadata = getattr(model, "__pydantic_generic_metadata__", None)
    return (metadata and metadata["origin"]) or model


def _named_change(model: type[Any]) -> FutureChange | None:
    """Return the change of `model`, which takes its class name, if any.

    A parametrized generic model such as `Event[int]` takes the topic `Event`
    for every parametrization from 0.46.0.
    """
    origin = _origin(model)
    if origin is model:
        return None
    return _generic_outbox_topic(model.__name__, origin.__name__)


def _inherited_change(model: type[Any], topic: str) -> FutureChange | None:
    """Return the change of `model`, which inherits `topic`, if any.

    A parametrized generic model such as `Event[int]` is checked as its
    generic origin `Event`, and the change names the origin.
    """
    origin = _origin(model)
    if "__outbox_topic__" in vars(origin):
        return None
    parent = next(
        (base for base in origin.__mro__ if "__outbox_topic__" in vars(base)),
        None,
    )
    if parent is None:
        return None
    return _inherited_outbox_topic(origin.__name__, parent.__name__, topic)


@lru_cache(maxsize=512)
def _generic_outbox_topic(model: str, origin: str) -> FutureChange:
    """Return the change of the parametrization `model` of `origin`."""
    from grelmicro._deprecation import generic_outbox_topic  # noqa: PLC0415

    return generic_outbox_topic(model=model, origin=origin)


@lru_cache(maxsize=512)
def _inherited_outbox_topic(
    model: str, parent: str, topic: str
) -> FutureChange:
    """Return the change of `model`, which inherits `topic` from `parent`."""
    from grelmicro._deprecation import inherited_outbox_topic  # noqa: PLC0415

    return inherited_outbox_topic(model=model, parent=parent, topic=topic)


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
