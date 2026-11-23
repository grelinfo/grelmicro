"""Outbox router: handlers declared apart from the outbox."""

from __future__ import annotations

import threading
import weakref
from typing import TYPE_CHECKING, Annotated, Any

from typing_extensions import Doc

from grelmicro._markers import Registered, mark_registered
from grelmicro.outbox._registry import (
    HandlerEntry,
    OutboxRegistry,
    derive_topic,
)
from grelmicro.outbox.errors import OutboxRouterError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Iterator

    from grelmicro.outbox._message import Message


_WIRING = threading.Lock()
"""Held while a router's handlers or included routers change, so threads
building apps from the same module-level router never see half a change."""


class OutboxRouter:
    """Outbox Router.

    Groups outbox handlers, for example to declare the handlers of one
    feature in its own module. A router needs no provider, so that module
    imports nothing from the app. Include it in the `Outbox`, or in another
    `OutboxRouter`.

    Example:
        ```python
        from grelmicro.outbox import Message, OutboxRouter

        router = OutboxRouter()


        @router.handler("email.welcome")
        async def send_welcome(message: Message) -> None:
            await mailer.send(to=message.payload["to"])
        ```

    Read more in the [Outbox](../outbox/consumer.md#split-handlers-across-modules)
    docs.
    """

    def __init__(
        self,
        *,
        routers: Annotated[
            Iterable[OutboxRouter] | None,
            Doc(
                "Routers to include, the same as `include_router` for each. "
                "A refused list includes none of them."
            ),
        ] = None,
    ) -> None:
        """Initialize the router.

        Raises:
            OutboxRouterError: If a router in `routers` cannot be included.
            HandlerAlreadyRegisteredError: If two routers in `routers` share
                a topic, or one holds a topic already taken where this router
                is included.
        """
        self._handlers: list[HandlerEntry] = []
        self._routers: list[OutboxRouter] = []
        self._parents: weakref.WeakSet[OutboxRouter] = weakref.WeakSet()
        self._include(routers or [])

    def handler(
        self,
        target: Annotated[
            type[Any] | str,
            Doc("A payload model or a topic string to bind the handler to."),
        ],
        *,
        topic: Annotated[
            str | None,
            Doc(
                "Override the derived topic. None or an empty string keeps "
                "the derived topic."
            ),
        ] = None,
    ) -> Callable[
        [Callable[[Message[Any]], Awaitable[None]]],
        Callable[[Message[Any]], Awaitable[None]],
    ]:
        """Register an async handler for a payload model or a topic.

        The handler joins every `Outbox` this router is included in, at
        once, or none of them.

        Raises:
            HandlerAlreadyRegisteredError: If the topic is already taken in
                this router, or where this router is included.
        """

        def decorator(
            fn: Callable[[Message[Any]], Awaitable[None]],
        ) -> Callable[[Message[Any]], Awaitable[None]]:
            entry = HandlerEntry(
                topic or derive_topic(target),
                fn,
                target if isinstance(target, type) else None,
            )
            with _WIRING:
                _add_to_all(self._tops(), [entry])
                if self._owned_registry() is None:
                    self._handlers.append(entry)
            mark_registered(fn, Registered.OUTBOX_HANDLER, self)
            return fn

        return decorator

    def include_router(
        self,
        router: Annotated[OutboxRouter, Doc("The router to include.")],
    ) -> None:
        """Include another router's handlers in this router.

        A router can be included in several routers or outboxes, for
        example in each app an app factory builds.

        Raises:
            OutboxRouterError: If `router` is an `Outbox`, is this router
                or one that includes it, is already included in this router,
                or is already in an `Outbox` this router is included in.
            HandlerAlreadyRegisteredError: If a topic of `router` is
                already taken in this router, or where this router is
                included. Nothing of `router` is included then.
        """
        self._include([router])

    def _include(self, routers: Iterable[OutboxRouter]) -> None:
        """Include every router in `routers`, or none of them."""
        listed = list(routers)
        with _WIRING:
            self._include_locked(listed)

    def _include_locked(self, routers: list[OutboxRouter]) -> None:
        """Include every router in `routers`, or none of them, under the lock."""
        included = {id(router) for router in self._routers}
        for router in routers:
            if id(router) in included:
                msg = "This router is already included in this router"
                raise OutboxRouterError(msg)
            included.add(id(router))
            if router._owned_registry() is not None:  # noqa: SLF001
                msg = (
                    "An Outbox is the root of its routers and is never included"
                )
                raise OutboxRouterError(msg)
            if any(node is self for node in router._subtree()):  # noqa: SLF001
                msg = "A router cannot include itself or a router including it"
                raise OutboxRouterError(msg)
        tops = self._tops()
        roots = [top for top in tops if top._owned_registry() is not None]  # noqa: SLF001
        for root in roots:
            held = {id(node) for node in root._subtree()}  # noqa: SLF001
            for router in routers:
                for node in router._subtree():  # noqa: SLF001
                    if id(node) in held:
                        msg = "This router is already included in this outbox"
                        raise OutboxRouterError(msg)
                    held.add(id(node))
        entries = [
            entry
            for router in routers
            for entry in router._entries()  # noqa: SLF001
        ]
        _add_to_all(tops, entries)
        for router in routers:
            self._routers.append(router)
            router._parents.add(self)  # noqa: SLF001

    def _entries(self) -> list[HandlerEntry]:
        """Return this router's handlers and those of every router it includes."""
        return [entry for node in self._subtree() for entry in node._handlers]  # noqa: SLF001

    def _subtree(self) -> Iterator[OutboxRouter]:
        """Yield this router, then every router it includes, depth first."""
        yield self
        for router in self._routers:
            yield from router._subtree()  # noqa: SLF001

    def _tops(self) -> list[OutboxRouter]:
        """Return every router above this one that no router includes.

        That is every `Outbox` this router is included in, and every router
        no router includes, this router itself when none includes it.
        """
        tops: list[OutboxRouter] = []
        pending: list[OutboxRouter] = [self]
        seen: set[int] = set()
        while pending:
            node = pending.pop()
            if id(node) in seen:
                continue
            seen.add(id(node))
            parents = list(node._parents)  # noqa: SLF001
            if not parents or node._owned_registry() is not None:  # noqa: SLF001
                tops.append(node)
            pending.extend(parents)
        return tops

    def _owned_registry(self) -> OutboxRegistry | None:
        """Return the registry the relay reads, held by an `Outbox` only."""
        return None


def _add_to_all(tops: list[OutboxRouter], entries: list[HandlerEntry]) -> None:
    """Add `entries` under every top router, or under none if one refuses them.

    An `Outbox` checks them against its registry, and a router nothing
    includes against the handlers of its tree, so a taken topic raises the
    same error either way. Only an `Outbox` registry receives them.

    Raises:
        HandlerAlreadyRegisteredError: If a topic is already taken under a
            top router, or two entries share one.
    """
    registries: list[OutboxRegistry] = []
    for top in tops:
        registry = top._owned_registry()  # noqa: SLF001
        if registry is None:
            OutboxRegistry().check([*top._entries(), *entries])  # noqa: SLF001
        else:
            registry.check(entries)
            registries.append(registry)
    for registry in registries:
        registry.add(entries)
