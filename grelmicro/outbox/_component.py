"""Outbox component for the Grelmicro app object."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Self

from typing_extensions import Doc

from grelmicro._component import instantiate_if_class
from grelmicro._config import default_env_prefix, resolve_config
from grelmicro.metrics import _emit
from grelmicro.outbox._codec import encode_payload
from grelmicro.outbox._config import OutboxConfig
from grelmicro.outbox._message import OutboxRecord
from grelmicro.outbox._otel import inject_trace_context
from grelmicro.outbox._registry import OutboxRegistry, derive_topic
from grelmicro.outbox._relay import Relay
from grelmicro.outbox._uuid import uuid7
from grelmicro.outbox.errors import OutboxSettingsValidationError
from grelmicro.providers._base import Provider

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from types import TracebackType

    from grelmicro.outbox._message import Message
    from grelmicro.outbox._protocol import OutboxBackend


class Outbox:
    """Outbox component: stages messages and runs their handlers.

    Registered as `micro.outbox` after `Grelmicro(uses=[Outbox(...)])`.
    Accepts a `Provider` or an `OutboxBackend`. When given a Provider, the
    component calls `provider.outbox()` to build the matching adapter.

    Example:
        ```python
        from grelmicro import Grelmicro
        from grelmicro.outbox import Message, Outbox
        from grelmicro.providers.postgres import PostgresProvider

        postgres = PostgresProvider("postgresql://localhost:5432/app")
        outbox = Outbox(postgres)


        @outbox.handler("email.welcome")
        async def send_welcome(message: Message) -> None:
            await mailer.send(to=message.payload["to"])


        micro = Grelmicro(uses=[outbox])
        ```

    Read more in the [Outbox](../outbox.md) docs.
    """

    kind: ClassVar[str] = "outbox"

    def __init__(  # noqa: PLR0913
        self,
        source: Annotated[
            Provider | OutboxBackend | type[Provider | OutboxBackend],
            Doc(
                """
                A `Provider` (e.g. `PostgresProvider`) or an `OutboxBackend`.
                When a Provider is given, the component calls
                `provider.outbox()` to build the matching adapter.
                """,
            ),
        ],
        *,
        name: Annotated[str, Doc("Registration name.")] = "default",
        config: Annotated[
            OutboxConfig | None,
            Doc("A pre-built `OutboxConfig`. Mutually exclusive with kwargs."),
        ] = None,
        relay: Annotated[
            bool | None, Doc("Run the relay on this replica.")
        ] = None,
        table: Annotated[str | None, Doc("Table that stores messages.")] = None,
        poll_interval: Annotated[
            float | None, Doc("Seconds between fallback polls.")
        ] = None,
        batch_size: Annotated[
            int | None, Doc("Claim ceiling per cycle.")
        ] = None,
        lease_duration: Annotated[
            float | None, Doc("Seconds a claimed message stays invisible.")
        ] = None,
        max_attempts: Annotated[
            int | None, Doc("Attempts before dead-lettering.")
        ] = None,
        retry_base: Annotated[
            float | None, Doc("Base backoff in seconds.")
        ] = None,
        retry_max: Annotated[
            float | None, Doc("Maximum backoff in seconds.")
        ] = None,
        retry_jitter: Annotated[
            float | None, Doc("Jitter fraction applied to the backoff.")
        ] = None,
        concurrency: Annotated[
            int | None, Doc("Maximum handlers running at once.")
        ] = None,
        dead_letter: Annotated[
            bool | None, Doc("Move exhausted messages to the dead state.")
        ] = None,
        keep_delivered: Annotated[
            bool | timedelta | None,
            Doc(
                "Keep delivered rows instead of deleting them. A `timedelta` "
                "keeps them for that long, then the relay purges them."
            ),
        ] = None,
        auto_migrate: Annotated[
            bool | None, Doc("Create the table on first connect.")
        ] = None,
        notify: Annotated[
            bool | None, Doc("Use LISTEN/NOTIFY for low-latency wakeups.")
        ] = None,
        env_load: Annotated[
            bool | None, Doc("Read missing settings from the environment.")
        ] = None,
        shutdown_timeout: Annotated[
            float, Doc("Seconds to let in-flight handlers drain on shutdown.")
        ] = 30.0,
    ) -> None:
        """Initialize the component and resolve its config and backend."""
        self._name = name
        self._config = resolve_config(
            OutboxConfig,
            explicit=config,
            kwargs={
                "relay": relay,
                "table": table,
                "poll_interval": poll_interval,
                "batch_size": batch_size,
                "lease_duration": lease_duration,
                "max_attempts": max_attempts,
                "retry_base": retry_base,
                "retry_max": retry_max,
                "retry_jitter": retry_jitter,
                "concurrency": concurrency,
                "dead_letter": dead_letter,
                "keep_delivered": keep_delivered,
                "auto_migrate": auto_migrate,
                "notify": notify,
            },
            env_prefix=default_env_prefix("OUTBOX", name),
            env_load=env_load,
            error_type=OutboxSettingsValidationError,
        )
        source = instantiate_if_class(source)
        if isinstance(source, Provider):
            self._backend = source.outbox(
                table=self._config.table,
                auto_migrate=self._config.auto_migrate,
                notify=self._config.notify,
            )
        else:
            self._backend = source
        self._registry = OutboxRegistry()
        self._relay: Relay | None = None
        self._shutdown_timeout = shutdown_timeout

    @property
    def name(self) -> str:
        """Return the registration name."""
        return self._name

    @property
    def config(self) -> OutboxConfig:
        """Return the resolved configuration."""
        return self._config

    @property
    def backend(self) -> OutboxBackend:
        """The underlying `OutboxBackend`."""
        return self._backend

    @classmethod
    def current(
        cls,
        name: Annotated[str, Doc("Registration name.")] = "default",
    ) -> Outbox:
        """Return the registered `Outbox` from the active `Grelmicro` app.

        Lets a producer publish without holding the constructed instance or
        a config-bound module singleton:

        ```python
        await Outbox.current().publish(conn, WelcomeEmail(to=email))
        ```

        Raises:
            OutOfContextError: No active app, or no `Outbox` registered under
                `name`. Run inside `async with micro:` or after
                `micro.install(app)`, with an `Outbox` in `uses=[...]`.
        """
        from grelmicro._app import (  # noqa: PLC0415
            ComponentNotRegisteredError,
            Grelmicro,
            NoActiveAppError,
        )
        from grelmicro.errors import OutOfContextError  # noqa: PLC0415

        try:
            return Grelmicro.current().get(cls.kind, name)
        except (NoActiveAppError, ComponentNotRegisteredError):
            msg = (
                f"Outbox({name!r}) is not available: no active app, or no "
                f"Outbox registered under {name!r}. Run inside "
                f"`async with micro:` or after `micro.install(app)`, with an "
                f"Outbox registered in uses=[...]."
            )
            raise OutOfContextError(msg) from None

    def handler(
        self,
        target: Annotated[
            type[Any] | str,
            Doc("A payload model or a topic string to bind the handler to."),
        ],
        *,
        topic: Annotated[
            str | None,
            Doc("Override the derived topic."),
        ] = None,
    ) -> Callable[
        [Callable[[Message[Any]], Awaitable[None]]],
        Callable[[Message[Any]], Awaitable[None]],
    ]:
        """Register an async handler for a payload model or a topic."""

        def decorator(
            fn: Callable[[Message[Any]], Awaitable[None]],
        ) -> Callable[[Message[Any]], Awaitable[None]]:
            self._registry.register(target, fn, topic=topic)
            return fn

        return decorator

    async def publish(
        self,
        handle: Annotated[
            object,
            Doc("Your connection or session, already inside a transaction."),
        ],
        target: Annotated[
            object,
            Doc("A payload model instance, or a topic string with `payload`."),
        ],
        payload: Annotated[
            Mapping[str, Any] | None,
            Doc("The payload dict when `target` is a topic string."),
        ] = None,
        *,
        key: Annotated[str | None, Doc("Ordering or partition key.")] = None,
        headers: Annotated[
            Mapping[str, Any] | None, Doc("Metadata to carry with the message.")
        ] = None,
        dedup_key: Annotated[
            str | None, Doc("Producer-side deduplication key.")
        ] = None,
        delay: Annotated[
            float | timedelta | None,
            Doc("Hold the message back for this long before delivery."),
        ] = None,
    ) -> bool:
        """Stage a message inside the caller's transaction.

        Returns True when the message is staged, False when a `dedup_key`
        collides and the message is skipped.
        """
        topic, body = _resolve_payload(target, payload)
        message_headers = dict(headers) if headers else {}
        inject_trace_context(message_headers)
        record = OutboxRecord(
            id=uuid7(),
            topic=topic,
            payload=body,
            key=key,
            headers=message_headers,
            dedup_key=dedup_key,
            available_at=_resolve_available_at(delay),
        )
        staged = await self._backend.enqueue(handle, record)
        if staged:
            _emit.incr("grelmicro.outbox.published", topic=topic)
        return staged

    async def redrive(self, *, topic: str | None = None) -> int:
        """Move dead messages back to pending. Returns the count moved."""
        return await self._backend.redrive(topic=topic)

    async def purge(
        self,
        *,
        older_than: Annotated[
            timedelta | float | None,
            Doc("Only purge terminal rows older than this. None purges all."),
        ] = None,
    ) -> int:
        """Delete delivered and dead rows. Returns the count removed.

        Use it to trim the table once delivered or dead messages are no
        longer needed. Pending and in-flight messages are never touched.
        """
        seconds = (
            older_than.total_seconds()
            if isinstance(older_than, timedelta)
            else older_than
        )
        return await self._backend.purge(before_seconds=seconds)

    async def __aenter__(self) -> Self:
        """Open the backend and start the relay when enabled."""
        await self._backend.__aenter__()
        try:
            if self._config.relay:
                self._relay = await Relay(
                    backend=self._backend,
                    registry=self._registry,
                    config=self._config,
                    shutdown_timeout=self._shutdown_timeout,
                ).__aenter__()
        except BaseException:
            await self._backend.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        """Stop the relay and close the backend.

        The backend is closed even when the relay shutdown raises, so its
        pool and listener never leak.
        """
        try:
            if self._relay is not None:
                await self._relay.__aexit__(exc_type, exc, tb)
                self._relay = None
        finally:
            await self._backend.__aexit__(exc_type, exc, tb)
        return None


def _resolve_payload(
    target: object, payload: Mapping[str, Any] | None
) -> tuple[str, Mapping[str, Any]]:
    """Return the topic and payload dict from the publish arguments."""
    if isinstance(target, str):
        if payload is None:
            msg = "publish with a topic string needs a payload dict"
            raise TypeError(msg)
        return target, payload
    if payload is not None:
        msg = "publish with a payload model does not take a separate payload"
        raise TypeError(msg)
    return derive_topic(type(target)), encode_payload(target)


def _resolve_available_at(delay: float | timedelta | None) -> datetime | None:
    """Return the absolute time a delayed message becomes available."""
    if delay is None:
        return None
    span = delay if isinstance(delay, timedelta) else timedelta(seconds=delay)
    return datetime.now(UTC) + span
