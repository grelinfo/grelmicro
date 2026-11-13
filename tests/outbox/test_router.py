"""Outbox router tests: handlers declared apart from the outbox."""

from __future__ import annotations

import asyncio
import gc
import weakref

import pytest
from pydantic import BaseModel

from grelmicro.outbox import Message, Outbox, OutboxRouter
from grelmicro.outbox.errors import (
    HandlerAlreadyRegisteredError,
    OutboxRouterError,
)
from grelmicro.outbox.memory import MemoryOutboxAdapter
from grelmicro.resilience import retry

pytestmark = [pytest.mark.timeout(5)]


class OrderPlaced(BaseModel):
    """A sample payload model."""

    order_id: int


async def test_included_router_handler_receives_message() -> None:
    """A handler on an included router receives a message the outbox publishes."""
    router = OutboxRouter()
    received: asyncio.Queue[Message[OrderPlaced]] = asyncio.Queue()

    @router.handler(OrderPlaced)
    async def charge(message: Message[OrderPlaced]) -> None:
        await received.put(message)

    outbox = Outbox(MemoryOutboxAdapter())
    outbox.include_router(router)

    async with outbox:
        await outbox.publish(None, OrderPlaced(order_id=42))
        message = await received.get()

    assert message.topic == "OrderPlaced"
    assert message.data == OrderPlaced(order_id=42)


async def test_nested_router_in_constructor_receives_message() -> None:
    """A handler two routers deep, wired with routers=, receives its message."""
    inner = OutboxRouter()
    received: asyncio.Queue[Message[object]] = asyncio.Queue()

    @inner.handler("email.welcome")
    async def send_welcome(message: Message[object]) -> None:
        await received.put(message)

    outbox = Outbox(
        MemoryOutboxAdapter(), routers=[OutboxRouter(routers=[inner])]
    )

    async with outbox:
        await outbox.publish(None, "email.welcome", {"to": "alice"})
        message = await received.get()

    assert message.payload == {"to": "alice"}


async def test_handler_added_after_include_receives_message() -> None:
    """A handler declared on a router after it was included still receives."""
    router = OutboxRouter()
    outbox = Outbox(MemoryOutboxAdapter(), routers=[router])
    received: asyncio.Queue[Message[object]] = asyncio.Queue()

    @router.handler("email.welcome")
    async def send_welcome(message: Message[object]) -> None:
        await received.put(message)

    async with outbox:
        await outbox.publish(None, "email.welcome", {"to": "alice"})
        message = await received.get()

    assert message.topic == "email.welcome"


def _router_for(topic: str) -> OutboxRouter:
    """Return a router with one handler for `topic`."""
    router = OutboxRouter()

    @router.handler(topic)
    async def handle(message: Message[object]) -> None: ...

    return router


def test_duplicate_topic_across_routers_raises_on_include() -> None:
    """A second router on a taken topic raises when it joins the outbox."""
    outbox = Outbox(MemoryOutboxAdapter(), routers=[_router_for("job")])

    with pytest.raises(HandlerAlreadyRegisteredError, match="'job'"):
        outbox.include_router(_router_for("job"))


def test_refused_include_keeps_none_of_the_router() -> None:
    """A refused include registers no topic of that router."""
    outbox = Outbox(MemoryOutboxAdapter(), routers=[_router_for("taken")])
    router = OutboxRouter(routers=[_router_for("free"), _router_for("taken")])

    with pytest.raises(HandlerAlreadyRegisteredError):
        outbox.include_router(router)

    outbox.include_router(_router_for("free"))


def test_duplicate_handler_on_included_router_raises() -> None:
    """A handler on an included router raises when its topic is taken."""
    router = OutboxRouter()
    outbox = Outbox(MemoryOutboxAdapter(), routers=[router])

    @outbox.handler("job")
    async def one(message: Message[object]) -> None: ...

    with pytest.raises(HandlerAlreadyRegisteredError):

        @router.handler("job")
        async def two(message: Message[object]) -> None: ...


def test_duplicate_topic_inside_detached_routers_raises_on_include() -> None:
    """Two routers on one topic raise once their tree joins an outbox."""
    tree = OutboxRouter(routers=[_router_for("job"), _router_for("job")])

    with pytest.raises(HandlerAlreadyRegisteredError):
        Outbox(MemoryOutboxAdapter(), routers=[tree])


async def test_one_router_serves_two_outboxes() -> None:
    """A module-level router joins each outbox an app factory builds."""
    router = OutboxRouter()
    received: asyncio.Queue[Message[object]] = asyncio.Queue()
    first = Outbox(MemoryOutboxAdapter(), routers=[router])
    second = Outbox(MemoryOutboxAdapter(), routers=[router])

    @router.handler("job")
    async def handle(message: Message[object]) -> None:
        await received.put(message)

    for outbox in (first, second):
        async with outbox:
            await outbox.publish(None, "job", {"n": 1})
            await received.get()


def test_refused_router_list_keeps_none_of_it() -> None:
    """A refused routers= list leaves every router free to include again."""
    free = _router_for("free")

    with pytest.raises(HandlerAlreadyRegisteredError):
        Outbox(MemoryOutboxAdapter(), routers=[free, _router_for("free")])

    outbox = Outbox(MemoryOutboxAdapter(), routers=[free])
    with pytest.raises(HandlerAlreadyRegisteredError):
        outbox.include_router(_router_for("free"))


def test_late_handler_refused_by_one_outbox_joins_none() -> None:
    """A handler whose topic one outbox already holds joins no outbox."""
    router = OutboxRouter()
    first = Outbox(MemoryOutboxAdapter(), routers=[router])
    second = Outbox(MemoryOutboxAdapter(), routers=[router])

    @second.handler("job")
    async def taken(message: Message[object]) -> None: ...

    with pytest.raises(HandlerAlreadyRegisteredError):

        @router.handler("job")
        async def handle(message: Message[object]) -> None: ...

    @first.handler("job")
    async def free(message: Message[object]) -> None: ...


def test_router_reached_twice_in_one_outbox_raises() -> None:
    """A router under one outbox through two routers is refused."""
    shared = OutboxRouter()
    tree = OutboxRouter(
        routers=[OutboxRouter(routers=[shared]), OutboxRouter(routers=[shared])]
    )

    @shared.handler("job")
    async def handle(message: Message[object]) -> None: ...

    with pytest.raises(OutboxRouterError, match="already included"):
        Outbox(MemoryOutboxAdapter(), routers=[tree])


def test_router_included_twice_in_one_outbox_raises() -> None:
    """Including a router an outbox already holds is refused."""
    router = OutboxRouter()
    outbox = Outbox(MemoryOutboxAdapter(), routers=[router])

    with pytest.raises(OutboxRouterError, match="already included"):
        outbox.include_router(router)


def test_discarded_outbox_is_freed_by_its_router() -> None:
    """A router does not keep alive an outbox it was included in."""
    router = OutboxRouter()
    outbox = Outbox(MemoryOutboxAdapter(), routers=[router])
    ref = weakref.ref(outbox)

    del outbox
    gc.collect()

    assert ref() is None


def test_duplicate_topic_names_both_handlers() -> None:
    """The duplicate-topic error names the held handler and the refused one."""
    outbox = Outbox(MemoryOutboxAdapter())

    @outbox.handler("job")
    async def first_handler(message: Message[object]) -> None: ...

    with pytest.raises(HandlerAlreadyRegisteredError) as caught:

        @outbox.handler("job")
        async def second_handler(message: Message[object]) -> None: ...

    assert "first_handler" in str(caught.value)
    assert "second_handler" in str(caught.value)


def test_router_including_itself_raises() -> None:
    """A router cannot include itself or a router that includes it."""
    outer = OutboxRouter()
    inner = OutboxRouter()
    outer.include_router(inner)

    with pytest.raises(OutboxRouterError):
        outer.include_router(outer)
    with pytest.raises(OutboxRouterError):
        inner.include_router(outer)


def test_decorator_above_router_handler_raises() -> None:
    """A grelmicro decorator above `@router.handler` names the right order."""
    router = OutboxRouter()

    with pytest.raises(TypeError, match="already registered as an outbox"):

        @retry(when=ValueError, attempts=2)
        @router.handler("job")
        async def handle(message: Message[object]) -> None: ...


def test_outbox_cannot_be_included() -> None:
    """An outbox is the root of its routers and is never included."""
    with pytest.raises(OutboxRouterError):
        OutboxRouter(routers=[Outbox(MemoryOutboxAdapter())])
