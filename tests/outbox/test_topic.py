"""Outbox topic tests: the topic a payload model takes, and its 0.46 switch."""

from __future__ import annotations

import asyncio
import warnings
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel

from grelmicro.errors import Grelmicro046Warning
from grelmicro.outbox import Message, Outbox, OutboxRouter
from grelmicro.outbox.memory import MemoryOutboxAdapter

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = [pytest.mark.timeout(5)]


class OrderPlaced(BaseModel):
    """A payload model with a namespaced topic."""

    __outbox_topic__ = "billing.order_placed"

    order_id: int


class Signup(BaseModel):
    """A payload model that takes its class name as its topic."""

    user_id: int


class Event[T](BaseModel):
    """A generic payload model with a namespaced topic."""

    __outbox_topic__ = "events.generic"

    value: T


def _inherited(name: str) -> str:
    """Return the warning message for `name`, a subclass of `OrderPlaced`."""
    return (
        f"`{name}` takes its class name as its outbox topic from 0.46.0. "
        "Until then it inherits `billing.order_placed` from `OrderPlaced`. "
        'Set `__outbox_topic__ = "billing.order_placed"` on '
        f"`{name}` to keep the current topic, or "
        f'`__outbox_topic__ = "{name}"` to take its class name now.'
    )


def _topics(router: OutboxRouter) -> list[str]:
    """Return the topics of the handlers `router` holds."""
    return [entry.topic for entry in router._handlers]


def _assert_silent(register: Callable[[OutboxRouter], object]) -> OutboxRouter:
    """Assert `register` on a fresh router warns nothing, and return it."""
    router = OutboxRouter()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        register(router)
    assert [str(warning.message) for warning in caught] == []
    return router


def test_subclass_inheriting_a_topic_warns_at_the_handler_line() -> None:
    """A handler on a subclass that inherits its topic warns at the caller."""

    # Arrange
    class OrderPlacedV2(OrderPlaced):
        coupon: str

    router = OutboxRouter()

    # Act
    with pytest.warns(Grelmicro046Warning) as caught:

        @router.handler(OrderPlacedV2)
        async def charge(message: Message[OrderPlacedV2]) -> None: ...

    # Assert
    assert [str(warning.message) for warning in caught] == [
        _inherited("OrderPlacedV2")
    ]
    assert caught[0].filename == __file__
    assert _topics(router) == ["billing.order_placed"]


async def test_published_subclass_warns_once_and_keeps_the_parent_topic() -> (
    None
):
    """A published subclass warns once, at the caller, and keeps the topic."""

    # Arrange
    class OrderPlacedV2(OrderPlaced):
        coupon: str

    router = OutboxRouter()
    received: asyncio.Queue[Message[OrderPlaced]] = asyncio.Queue()

    @router.handler(OrderPlaced)
    async def charge(message: Message[OrderPlaced]) -> None:
        await received.put(message)

    outbox = Outbox(MemoryOutboxAdapter(), routers=[router])

    # Act
    async with outbox:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            await outbox.publish(None, OrderPlacedV2(order_id=1, coupon="A"))
            await outbox.publish(None, OrderPlacedV2(order_id=2, coupon="B"))
        first = await received.get()
        second = await received.get()

    # Assert
    assert [str(warning.message) for warning in caught] == [
        _inherited("OrderPlacedV2")
    ]
    assert caught[0].category is Grelmicro046Warning
    assert caught[0].filename == __file__
    assert {first.topic, second.topic} == {"billing.order_placed"}
    assert sorted(
        [first.data, second.data], key=lambda data: data.order_id if data else 0
    ) == [OrderPlaced(order_id=1), OrderPlaced(order_id=2)]


async def test_pending_subclass_row_reaches_the_parent_handler() -> None:
    """A row stored under the parent's topic reaches its handler added later."""

    # Arrange
    class OrderPlacedV2(OrderPlaced):
        coupon: str

    router = OutboxRouter()
    received: asyncio.Queue[Message[OrderPlaced]] = asyncio.Queue()

    @router.handler(OrderPlaced)
    async def charge(message: Message[OrderPlaced]) -> None:
        await received.put(message)

    outbox = Outbox(MemoryOutboxAdapter())

    # Act
    async with outbox:
        with pytest.warns(Grelmicro046Warning):
            await outbox.publish(None, OrderPlacedV2(order_id=7, coupon="A"))
        outbox.include_router(router)
        message = await received.get()

    # Assert
    assert message.topic == "billing.order_placed"
    assert message.data == OrderPlaced(order_id=7)


def test_subclass_with_its_own_topic_is_silent() -> None:
    """A subclass that sets `__outbox_topic__`, even the parent's, warns nothing."""

    class Same(OrderPlaced):
        __outbox_topic__ = "billing.order_placed"

    class Own(OrderPlaced):
        __outbox_topic__ = "billing.order_placed_v2"

    def register(router: OutboxRouter) -> None:
        @router.handler(Own)
        async def own(message: Message[Own]) -> None: ...

        @router.handler(Same)
        async def same(message: Message[Same]) -> None: ...

    router = _assert_silent(register)
    assert _topics(router) == [
        "billing.order_placed_v2",
        "billing.order_placed",
    ]


async def test_published_subclass_with_its_own_topic_is_silent() -> None:
    """A subclass that sets the parent's topic itself publishes with no warning."""

    class Same(OrderPlaced):
        __outbox_topic__ = "billing.order_placed"

    async with Outbox(MemoryOutboxAdapter()) as outbox:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            await outbox.publish(None, Same(order_id=1))

    assert [str(warning.message) for warning in caught] == []


def test_subclass_named_after_its_class_is_silent() -> None:
    """A subclass of a model with no `__outbox_topic__` takes its class name."""

    class SignupV2(Signup):
        source: str

    def register(router: OutboxRouter) -> None:
        @router.handler(SignupV2)
        async def welcome(message: Message[SignupV2]) -> None: ...

    router = _assert_silent(register)
    assert _topics(router) == ["SignupV2"]


def test_subclass_with_a_topic_override_is_silent() -> None:
    """`handler(topic=)` names the topic, so the model's topic is not read."""

    class OrderPlacedV2(OrderPlaced):
        coupon: str

    def register(router: OutboxRouter) -> None:
        @router.handler(OrderPlacedV2, topic="billing.v2")
        async def charge(message: Message[OrderPlacedV2]) -> None: ...

    router = _assert_silent(register)
    assert _topics(router) == ["billing.v2"]


def test_parametrized_generic_reads_its_origin_topic_silently() -> None:
    """`Event[int]` takes the topic of `Event` and warns nothing."""

    def register(router: OutboxRouter) -> None:
        @router.handler(Event[int])
        async def handle(message: Message[Event[int]]) -> None: ...

    router = _assert_silent(register)
    assert _topics(router) == ["events.generic"]


def test_subclass_of_a_parametrized_generic_warns() -> None:
    """A subclass of `Event[int]` inherits the topic of `Event` and warns."""

    class IntEvent(Event[int]):
        pass

    router = OutboxRouter()

    with pytest.warns(
        Grelmicro046Warning,
        match="Until then it inherits `events.generic` from `Event`",
    ):

        @router.handler(IntEvent)
        async def handle(message: Message[IntEvent]) -> None: ...

    assert _topics(router) == ["events.generic"]
