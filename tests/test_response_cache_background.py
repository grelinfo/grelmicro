"""`CachedResponses` answers a miss before the framework runs background work."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from fastapi import BackgroundTasks, FastAPI
from litestar import Litestar, get
from litestar import Response as LitestarResponse
from litestar.background_tasks import BackgroundTask as LitestarBackgroundTask
from litestar.exceptions import LitestarException
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse
from starlette.routing import Route

from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.http import CachedResponses

if TYPE_CHECKING:
    from collections.abc import (
        AsyncGenerator,
        Awaitable,
        Callable,
        MutableMapping,
    )

pytestmark = [pytest.mark.timeout(10)]

HTTP_200_OK = 200
TTL = timedelta(seconds=60)
ANSWERED_WITHIN = 2.0
"""Seconds a read may take to be answered before the test calls it held."""


def _fastapi(work: Callable[[], Awaitable[None]], calls: list[int]) -> Any:  # noqa: ANN401
    """Return a FastAPI app whose `/reads` adds `work` as a background task."""
    app = FastAPI()

    @app.get("/reads")
    async def reads(tasks: BackgroundTasks) -> dict[str, int]:
        calls.append(1)
        tasks.add_task(work)
        return {"calls": len(calls)}

    return app


def _starlette(work: Callable[[], Awaitable[None]], calls: list[int]) -> Any:  # noqa: ANN401
    """Return a Starlette app whose `/reads` answers with `work` behind it."""

    async def reads(request: Any) -> JSONResponse:  # noqa: ANN401, ARG001
        calls.append(1)
        return JSONResponse(
            {"calls": len(calls)}, background=BackgroundTask(work)
        )

    return Starlette(routes=[Route("/reads", reads)])


def _litestar(work: Callable[[], Awaitable[None]], calls: list[int]) -> Any:  # noqa: ANN401
    """Return a Litestar app whose `/reads` answers with `work` behind it."""

    @get("/reads")
    async def reads() -> LitestarResponse[dict[str, int]]:
        calls.append(1)
        return LitestarResponse(
            {"calls": len(calls)}, background=LitestarBackgroundTask(work)
        )

    return Litestar([reads])


FRAMEWORKS = [
    pytest.param(_fastapi, RuntimeError, id="fastapi"),
    pytest.param(_starlette, RuntimeError, id="starlette"),
    pytest.param(_litestar, LitestarException, id="litestar"),
]
"""Each framework, and what its app raises for a background task that fails."""


def _cached(
    build: Callable[..., Any],
    work: Callable[[], Awaitable[None]],
    calls: list[int],
) -> Any:  # noqa: ANN401
    """Return the app `build` makes, with `/reads` cached."""
    app = build(work, calls)
    Grelmicro(
        uses=[
            Cache(MemoryCacheAdapter()),
            CachedResponses(include={"/reads": TTL}),
        ]
    ).install(app)
    return app


@asynccontextmanager
async def _started(app: Any) -> AsyncGenerator[dict[str, Any]]:  # noqa: ANN401
    """Run the app's lifespan around the body, and yield its state."""
    state: dict[str, Any] = {}
    to_app, app_reads = anyio.create_memory_object_stream[Any](2)
    app_writes, from_app = anyio.create_memory_object_stream[Any](2)

    async def receive() -> Any:  # noqa: ANN401
        return await app_reads.receive()

    async def send(message: Any) -> None:  # noqa: ANN401
        await app_writes.send(message)

    scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": state}
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(app, scope, receive, send)
        await to_app.send({"type": "lifespan.startup"})
        started = await from_app.receive()
        assert started["type"] == "lifespan.startup.complete"
        yield state
        await to_app.send({"type": "lifespan.shutdown"})
        await from_app.receive()


class _Read:
    """One `GET /reads`, with what the client was sent."""

    def __init__(
        self,
        app: Any,  # noqa: ANN401
        state: dict[str, Any],
        answered: anyio.Event | None = None,
    ) -> None:
        """Prepare the read against a started app."""
        self._app = app
        self._state = state
        self.sent: list[MutableMapping[str, Any]] = []
        self.answered = answered or anyio.Event()
        """Set once the last body message reached the client."""

    @property
    def status(self) -> int:
        """Return the status the client was sent."""
        return int(self.sent[0]["status"])

    @property
    def headers(self) -> dict[bytes, bytes]:
        """Return the headers the client was sent."""
        return dict(self.sent[0]["headers"])

    async def __call__(self) -> None:
        """Send the read and let the app run to its end."""

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: MutableMapping[str, Any]) -> None:
            self.sent.append(message)
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                self.answered.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/reads",
            "raw_path": b"/reads",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "state": dict(self._state),
        }
        await self._app(scope, receive, send)


@pytest.mark.parametrize(("build", "raised"), FRAMEWORKS)
async def test_a_slow_background_task_never_delays_the_cached_read(
    build: Callable[..., Any],
    raised: type[Exception],  # noqa: ARG001
) -> None:
    """The miss is answered when its body completes, not when the app returns."""
    # Arrange
    release = anyio.Event()
    answered_first: list[bool] = []
    calls: list[int] = []
    answered = anyio.Event()

    async def work() -> None:
        answered_first.append(answered.is_set())
        await release.wait()

    app = _cached(build, work, calls)

    # Act
    async with _started(app) as state, anyio.create_task_group() as tasks:
        read = _Read(app, state, answered)
        tasks.start_soon(read)
        with anyio.fail_after(ANSWERED_WITHIN):
            await read.answered.wait()
        release.set()

    # Assert
    assert read.status == HTTP_200_OK
    assert answered_first == [True]


@pytest.mark.parametrize(("build", "raised"), FRAMEWORKS)
async def test_a_failing_background_task_keeps_the_answer_and_the_entry(
    build: Callable[..., Any],
    raised: type[Exception],
) -> None:
    """The client gets the handler's status, and the next read is a hit."""
    # Arrange
    calls: list[int] = []

    async def work() -> None:
        msg = "the background task broke"
        raise RuntimeError(msg)

    app = _cached(build, work, calls)

    # Act
    async with _started(app) as state:
        miss = _Read(app, state)
        with pytest.raises(raised):
            await miss()
        hit = _Read(app, state)
        await hit()

    # Assert
    assert miss.status == HTTP_200_OK
    assert hit.status == HTTP_200_OK
    assert b"age" in hit.headers
    assert len(calls) == 1


@pytest.mark.parametrize(("build", "raised"), FRAMEWORKS)
async def test_a_background_task_never_holds_the_next_caller_of_the_key(
    build: Callable[..., Any],
    raised: type[Exception],  # noqa: ARG001
) -> None:
    """A caller folded on the same key is answered from the stored entry."""
    # Arrange
    release = anyio.Event()
    calls: list[int] = []

    async def work() -> None:
        await release.wait()

    app = _cached(build, work, calls)

    # Act
    async with _started(app) as state, anyio.create_task_group() as tasks:
        first = _Read(app, state)
        second = _Read(app, state)
        tasks.start_soon(first)
        with anyio.fail_after(ANSWERED_WITHIN):
            await first.answered.wait()
        tasks.start_soon(second)
        with anyio.fail_after(ANSWERED_WITHIN):
            await second.answered.wait()
        release.set()

    # Assert
    assert first.status == second.status == HTTP_200_OK
    assert b"age" in second.headers
    assert len(calls) == 1
