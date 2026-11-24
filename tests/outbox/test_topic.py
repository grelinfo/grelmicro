"""Outbox topic tests: the topic a payload model takes, and its 0.46 switch."""

from __future__ import annotations

import asyncio
import warnings
from typing import TYPE_CHECKING, Annotated, Any

import pytest
from opentelemetry import trace
from pydantic import BaseModel, Field

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


@pytest.fixture
def no_tracer_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave no global tracer provider while the test publishes.

    An SDK `TracerProvider.get_tracer` calls `warnings.filterwarnings`, which
    clears every warning registry, so a line that warned warns again. A
    provider another test left global would reach it on the first publish.
    """
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)


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


@pytest.mark.usefixtures("no_tracer_provider")
async def test_published_subclass_warns_once_and_keeps_the_parent_topic() -> (
    None
):
    """A published subclass warns once per caller line, and keeps the topic."""

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
            warnings.simplefilter("default")
            for order_id in (1, 2):
                await outbox.publish(
                    None, OrderPlacedV2(order_id=order_id, coupon="A")
                )
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


class _Topical(BaseModel):
    """A parent model with a namespaced topic."""

    __outbox_topic__ = "base.topic"


class _Wrapped[T](_Topical):
    """A generic payload model that inherits its topic."""

    value: T


def test_parametrized_generic_of_an_inheriting_origin_warns_for_the_origin() -> (
    None
):
    """`Wrapped[int]` warns as `Wrapped`, the topic it takes from 0.46.0."""
    router = OutboxRouter()

    with pytest.warns(Grelmicro046Warning) as caught:

        @router.handler(_Wrapped[int])
        async def handle(message: Message[_Wrapped[int]]) -> None: ...

    expected = (
        "`_Wrapped` takes its class name as its outbox topic from 0.46.0. "
        "Until then it inherits `base.topic` from `_Topical`. "
        'Set `__outbox_topic__ = "base.topic"` on `_Wrapped` to keep the '
        'current topic, or `__outbox_topic__ = "_Wrapped"` to take its class '
        "name now."
    )
    assert [str(warning.message) for warning in caught] == [expected]
    assert _topics(router) == ["base.topic"]


class _BillingTopic:
    """A plain mixin that names a topic."""

    __outbox_topic__ = "billing"


def test_topic_inherited_from_a_mixin_warns() -> None:
    """A model that takes its topic from a non-model mixin warns too."""

    class Invoice(_BillingTopic, BaseModel):
        number: int

    router = OutboxRouter()

    with pytest.warns(
        Grelmicro046Warning,
        match="Until then it inherits `billing` from `_BillingTopic`",
    ):

        @router.handler(Invoice)
        async def handle(message: Message[Invoice]) -> None: ...

    assert _topics(router) == ["billing"]


def test_suppressed_warning_returns_at_the_next_resolution() -> None:
    """A warning ignored once still warns where it is reached again."""

    class OrderPlacedV2(OrderPlaced):
        coupon: str

    async def handle(message: Message[OrderPlacedV2]) -> None: ...

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        OutboxRouter().handler(OrderPlacedV2)(handle)

    with pytest.warns(Grelmicro046Warning):
        OutboxRouter().handler(OrderPlacedV2)(handle)


@pytest.mark.parametrize(
    ("target", "topic"),
    [
        pytest.param(OrderPlaced | None, "Union", id="union"),
        pytest.param(
            Annotated[OrderPlaced, Field(strict=True)],
            "Annotated",
            id="annotated",
        ),
    ],
)
def test_non_class_target_keeps_its_topic_silently(
    target: object, topic: str
) -> None:
    """A union or an `Annotated` target keeps its topic and warns nothing."""

    def register(router: OutboxRouter) -> None:
        @router.handler(target)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        async def handle(message: Message[Any]) -> None: ...

    router = _assert_silent(register)
    assert _topics(router) == [topic]


def test_empty_topic_override_keeps_the_derived_topic() -> None:
    """`handler(topic="")` registers under the model's topic, as before."""

    def register(router: OutboxRouter) -> None:
        @router.handler(OrderPlaced, topic="")
        async def handle(message: Message[OrderPlaced]) -> None: ...

    router = _assert_silent(register)
    assert _topics(router) == ["billing.order_placed"]


class _Plain[T](BaseModel):
    """A generic payload model with no `__outbox_topic__` anywhere."""

    value: T


_PLAIN_INT = (
    "`_Plain[int]` and every other parametrization of `_Plain` take the "
    "outbox topic `_Plain` from 0.46.0. Until then the topic of "
    "`_Plain[int]` is `_Plain[int]`. Set `__outbox_topic__` on `_Plain` to "
    "pick one topic for all of them now, or "
    '`__outbox_topic__ = "_Plain[int]"` on a subclass of `_Plain[int]` to '
    "keep this topic."
)


def test_parametrized_generic_named_after_itself_warns() -> None:
    """`Plain[int]` keeps the topic `Plain[int]` and warns that it changes."""
    router = OutboxRouter()

    with pytest.warns(Grelmicro046Warning) as caught:

        @router.handler(_Plain[int])
        async def handle(message: Message[_Plain[int]]) -> None: ...

    assert [str(warning.message) for warning in caught] == [_PLAIN_INT]
    assert caught[0].filename == __file__
    assert _topics(router) == ["_Plain[int]"]


@pytest.mark.usefixtures("no_tracer_provider")
async def test_published_parametrized_generic_warns_once_per_line() -> None:
    """A published `Plain[int]` warns once per caller line, at each line."""
    async with Outbox(MemoryOutboxAdapter()) as outbox:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("default")
            for value in (1, 2):
                await outbox.publish(None, _Plain[int](value=value))
            await outbox.publish(None, _Plain[int](value=3))

    assert [str(warning.message) for warning in caught] == [_PLAIN_INT] * 2
    assert {warning.filename for warning in caught} == {__file__}


def test_named_subclass_of_a_parametrized_generic_is_silent() -> None:
    """A named subclass of `Plain[int]` takes its own class name, as before."""

    class PlainInt(_Plain[int]):
        pass

    class Kept(_Plain[int]):
        __outbox_topic__ = "_Plain[int]"

    def register(router: OutboxRouter) -> None:
        @router.handler(PlainInt)
        async def plain(message: Message[PlainInt]) -> None: ...

        @router.handler(Kept)
        async def kept(message: Message[Kept]) -> None: ...

    router = _assert_silent(register)
    assert _topics(router) == ["PlainInt", "_Plain[int]"]


def test_topic_assigned_after_first_use_takes_effect() -> None:
    """A model reads `__outbox_topic__` again at each registration."""

    class Refund(BaseModel):
        amount: int

    async def handle(message: Message[Refund]) -> None: ...

    first = _assert_silent(lambda router: router.handler(Refund)(handle))
    Refund.__outbox_topic__ = "billing.refund"  # type: ignore[attr-defined]  # ty: ignore[missing-attribute]
    second = _assert_silent(lambda router: router.handler(Refund)(handle))
    del Refund.__outbox_topic__  # type: ignore[attr-defined]  # ty: ignore[missing-attribute]
    third = _assert_silent(lambda router: router.handler(Refund)(handle))

    assert _topics(first) == ["Refund"]
    assert _topics(second) == ["billing.refund"]
    assert _topics(third) == ["Refund"]


def test_parent_topic_assigned_after_first_use_takes_effect() -> None:
    """A subclass reads the topic its parent holds at each registration."""

    class Base(BaseModel):
        __outbox_topic__ = "billing.first"

    class Child(Base):
        pass

    async def handle(message: Message[Child]) -> None: ...

    first = OutboxRouter()
    second = OutboxRouter()
    with pytest.warns(Grelmicro046Warning, match="inherits `billing.first`"):
        first.handler(Child)(handle)
    Base.__outbox_topic__ = "billing.second"
    with pytest.warns(Grelmicro046Warning, match="inherits `billing.second`"):
        second.handler(Child)(handle)
    Child.__outbox_topic__ = "billing.second"
    third = _assert_silent(lambda router: router.handler(Child)(handle))

    assert _topics(first) == ["billing.first"]
    assert _topics(second) == ["billing.second"]
    assert _topics(third) == ["billing.second"]


def test_parametrized_generic_with_its_own_topic_is_silent() -> None:
    """A topic set on `Plain[int]` itself is its topic, and warns nothing."""

    class Measure[T](BaseModel):
        value: T

    Measure[int].__outbox_topic__ = "integer-measures"  # type: ignore[attr-defined]  # ty: ignore[missing-attribute]

    def register(router: OutboxRouter) -> None:
        @router.handler(Measure[int])
        async def handle(message: Message[Measure[int]]) -> None: ...

    router = _assert_silent(register)
    assert _topics(router) == ["integer-measures"]


class _TopicMeta(type(BaseModel)):  # type: ignore[misc]
    """A model metaclass that names a topic."""

    __outbox_topic__ = "meta.topic"


def test_topic_from_a_metaclass_is_silent() -> None:
    """A topic that only the metaclass holds is the topic, and warns nothing."""

    class Audited(BaseModel, metaclass=_TopicMeta):
        entry: int

    def register(router: OutboxRouter) -> None:
        @router.handler(Audited)
        async def handle(message: Message[Audited]) -> None: ...

    router = _assert_silent(register)
    assert _topics(router) == ["meta.topic"]


class _PropertyTopicMeta(type(BaseModel)):  # type: ignore[misc]
    """A model metaclass that names a topic with a property."""

    @property
    def __outbox_topic__(cls) -> str:
        return f"meta.{cls.__name__}"


class _GetattrTopicMeta(type(BaseModel)):  # type: ignore[misc]
    """A model metaclass that names a topic in `__getattr__`."""

    def __getattr__(cls, name: str) -> object:
        if name == "__outbox_topic__":
            return f"meta.{cls.__name__}"
        return super().__getattr__(name)  # ty: ignore[missing-attribute]


class _GetattributeTopicMeta(type(BaseModel)):  # type: ignore[misc]
    """A model metaclass that names a topic in `__getattribute__`."""

    def __getattribute__(cls, name: str) -> object:
        if name == "__outbox_topic__":
            return f"meta.{type.__getattribute__(cls, '__name__')}"
        return super().__getattribute__(name)


@pytest.mark.parametrize(
    "metaclass",
    [_PropertyTopicMeta, _GetattrTopicMeta, _GetattributeTopicMeta],
    ids=["property", "getattr", "getattribute"],
)
def test_topic_computed_by_a_metaclass_is_silent(metaclass: type) -> None:
    """A topic the metaclass computes is the topic, and warns nothing."""

    class Audited(BaseModel, metaclass=metaclass):
        entry: int

    def register(router: OutboxRouter) -> None:
        @router.handler(Audited)
        async def handle(message: Message[Audited]) -> None: ...

    router = _assert_silent(register)
    assert _topics(router) == ["meta.Audited"]
