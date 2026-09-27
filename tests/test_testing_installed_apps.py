"""Tests for testing an app that `install` wires into a framework."""

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from faststream import FastStream
from faststream.redis import RedisBroker, TestRedisBroker
from litestar import Litestar
from litestar.testing import TestClient as LitestarTestClient

from grelmicro import Grelmicro
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.coordination import Coordination, Lock
from grelmicro.errors import OutOfContextError
from grelmicro.health import HealthChecks
from grelmicro.http import IdempotentRequests
from grelmicro.integrations import fastapi as fastapi_integration
from grelmicro.integrations.litestar import install_middleware
from grelmicro.outbox import Outbox
from grelmicro.providers import Provider
from grelmicro.providers.memory import MemoryProvider
from grelmicro.providers.postgres import PostgresProvider
from grelmicro.providers.redis import RedisProvider
from grelmicro.providers.sqlite import SQLiteProvider
from grelmicro.providers.valkey import ValkeyProvider


async def test_opening_an_open_app_says_it_is_already_open() -> None:
    """The error names the double open and where the first one came from."""
    micro = Grelmicro(uses=[MemoryProvider()])

    async with micro:
        with pytest.raises(OutOfContextError, match="already open") as error:
            await micro.__aenter__()

    assert "install(app)" in str(error.value)


def test_installing_twice_on_one_app_opens_it_once() -> None:
    """A second `install` on the same app wires nothing more."""
    micro = Grelmicro(uses=[MemoryProvider()])
    app = FastAPI()
    micro.install(app)
    micro.install(app)

    with TestClient(app):
        assert micro.opened

    assert not micro.opened


def test_installing_twice_on_one_litestar_app_opens_it_once() -> None:
    """The same holds for a framework that opens the app on startup."""
    micro = Grelmicro(uses=[MemoryProvider()])
    app = Litestar()
    micro.install(app)
    micro.install(app)

    with LitestarTestClient(app):
        assert micro.opened


async def test_installing_twice_on_one_faststream_app_opens_it_once() -> None:
    """An app with no `state` is remembered by the `Grelmicro` instead."""
    micro = Grelmicro(uses=[MemoryProvider()])
    broker = RedisBroker()
    app = FastStream(broker)
    micro.install(app)
    micro.install(app)

    async with TestRedisBroker(broker):
        await app.start()
        assert micro.opened
        await app.stop()


_CLOSED_PORT = "postgresql://user:password@127.0.0.1:1/db"
"""A Postgres URL nothing listens on, so any connection attempt fails."""


async def test_a_fake_entered_before_open_never_connects() -> None:
    """The app opens on memory, and the Postgres it lists stays closed."""
    postgres = PostgresProvider(_CLOSED_PORT)
    micro = Grelmicro(uses=[postgres])

    async with micro.fake(), micro:
        assert type(micro.get(Cache).backend).__name__ == "MemoryCacheAdapter"
        async with Lock("cart"):
            pass
        assert postgres not in micro.providers


def test_a_fake_entered_before_the_client_starts_serves_requests() -> None:
    """The one fixture the guide shows: arm, then let the lifespan open."""
    micro = Grelmicro(
        uses=[PostgresProvider(_CLOSED_PORT), HealthChecks(auto_health=True)]
    )
    app = FastAPI()
    micro.install(app)

    @app.get("/checkout")
    async def checkout() -> dict[str, bool]:
        async with Lock("cart"):
            report = await micro.health.run()
        return {"ready": report["status"] == "ok"}

    with micro.fake(), TestClient(app) as client:
        assert client.get("/checkout").json() == {"ready": True}

    assert not micro.opened


async def test_a_provider_the_fake_skipped_says_how_to_keep_it() -> None:
    """Reaching a skipped Provider names `fake(keep=[...])`."""
    postgres = PostgresProvider(_CLOSED_PORT)
    redis = RedisProvider("redis://127.0.0.1:1/0")
    micro = Grelmicro(uses=[Cache(postgres), Coordination(redis)])

    async with micro.fake(), micro:
        with pytest.raises(OutOfContextError, match=r"fake\(keep="):
            _ = postgres.client
        with pytest.raises(OutOfContextError, match=r"fake\(keep="):
            _ = redis.client

    assert redis.client is not None


async def test_a_kept_provider_is_opened() -> None:
    """`keep=` opens the real Provider, so a test can reach it directly."""
    postgres = PostgresProvider(_CLOSED_PORT)
    micro = Grelmicro(uses=[postgres])

    with pytest.raises(OSError, match=r"127\.0\.0\.1"):
        async with micro.fake(keep=[postgres]), micro:
            pass  # pragma: no cover


async def test_a_provider_a_real_component_still_borrows_is_opened() -> None:
    """`Outbox` is not faked, so the Postgres it borrows still opens."""
    postgres = PostgresProvider(_CLOSED_PORT)
    micro = Grelmicro(uses=[Cache(postgres), Outbox(postgres)])

    with pytest.raises(OSError, match=r"127\.0\.0\.1"):
        async with micro.fake(), micro:
            pass  # pragma: no cover


async def test_the_next_open_without_a_fake_is_the_real_app() -> None:
    """Registrations come back when the faked app closes."""
    redis = RedisProvider("redis://127.0.0.1:1/0")
    micro = Grelmicro(uses=[redis])

    async with micro.fake(), micro:
        pass

    assert type(micro.get(Cache).backend).__name__ == "RedisCacheAdapter"
    assert redis in micro.providers


async def test_a_fake_on_an_open_app_needs_async_with() -> None:
    """Only `async with` can swap components on an app already open."""
    micro = Grelmicro(uses=[MemoryProvider()])

    async with micro:
        with pytest.raises(OutOfContextError, match=r"async with micro\.fake"):
            with micro.fake():
                pass  # pragma: no cover


async def test_a_faked_app_skips_the_backend_scope_check() -> None:
    """Memory is the point of a fake, even in a production-like tier."""
    micro = Grelmicro(
        uses=[RedisProvider("redis://127.0.0.1:1/0")], environment="production"
    )

    async with micro.fake(), micro:
        assert micro.opened


@pytest.mark.parametrize(
    "snippet", ["installed_app.py", "plain_app.py", "app_factory.py"]
)
def test_the_documented_fixtures_pass(snippet: str) -> None:
    """The fixtures the guide shows run as a suite of their own."""
    path = Path(__file__).parent.parent / "docs/snippets/testing" / snippet
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            str(path),
            "-q",
            "-n0",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GREL_ENVIRONMENT": "test"},
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_two_apps_installed_on_one_starlette_app_both_open() -> None:
    """A second `Grelmicro` on the same app opens with the first."""
    first = Grelmicro(uses=[MemoryProvider()])
    second = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    app = FastAPI()
    first.install(app)
    second.install(app)

    with TestClient(app):
        assert first.opened
        assert second.opened


def test_two_apps_installed_on_one_litestar_app_both_open() -> None:
    """The binding is added once, whichever `Grelmicro` comes second."""
    first = Grelmicro(uses=[MemoryProvider()])
    second = Grelmicro(uses=[Cache(MemoryCacheAdapter())])
    app = Litestar()
    first.install(app)
    second.install(app)

    with LitestarTestClient(app):
        assert first.opened
        assert second.opened


def test_a_litestar_middleware_installed_twice_wraps_once() -> None:
    """The integration adds a component's middleware once, however called."""
    app = Litestar()
    component = IdempotentRequests()
    middleware, _ = component.asgi_middleware()
    install_middleware(app, [component])
    install_middleware(app, [component])

    layers = 0
    handler: object = app.asgi_handler
    while handler is not None:
        layers += isinstance(handler, middleware)
        handler = getattr(handler, "app", None)

    assert layers == 1


async def test_a_sqlite_provider_the_fake_skipped_says_how_to_keep_it(
    tmp_path: Path,
) -> None:
    """SQLite names the fix too, instead of opening its file."""
    sqlite = SQLiteProvider(path=str(tmp_path / "app.db"))
    micro = Grelmicro(uses=[Cache(sqlite)])

    async with micro.fake(), micro:
        with pytest.raises(OutOfContextError, match=r"fake\(keep="):
            _ = sqlite.client


async def test_readiness_left_from_a_real_open_skips_a_faked_provider(
    tmp_path: Path,
) -> None:
    """A check registered on an earlier real open does not probe under fake."""
    redis = RedisProvider("redis://127.0.0.1:1/0")
    sqlite = SQLiteProvider(path=str(tmp_path / "app.db"))
    health = HealthChecks(auto_health=True)
    health.add_provider(sqlite)
    micro = Grelmicro(uses=[redis, Cache(sqlite), health])

    async with micro:
        assert "provider:redis" in (await health.run())["checks"]

    async with micro.fake(), micro:
        report = await health.run()

    assert report["status"] == "ok"
    assert "provider:redis" not in report["checks"]
    assert "provider:sqlite" not in report["checks"]


async def test_a_provider_the_test_opens_itself_works_under_fake() -> None:
    """Only a Provider nothing opened refuses, so a test can open its own."""
    redis = RedisProvider("redis://127.0.0.1:1/0")
    micro = Grelmicro(uses=[redis])

    async with micro.fake(), micro:
        async with redis:
            assert redis.client is not None
        with pytest.raises(OutOfContextError, match=r"fake\(keep="):
            _ = redis.client


async def test_a_provider_two_faked_apps_skip_stays_closed_until_both_close() -> (
    None
):
    """One faked app closing leaves the other's mark in place."""
    postgres = PostgresProvider(_CLOSED_PORT)
    first = Grelmicro(uses=[Cache(postgres)])
    second = Grelmicro(uses=[Cache(postgres)])

    async with first.fake(), first:
        async with second.fake(), second:
            pass
        with pytest.raises(OutOfContextError, match=r"fake\(keep="):
            _ = postgres.client

    with pytest.raises(OutOfContextError, match="outside of the context"):
        _ = postgres.client


def test_a_failed_install_can_be_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app is marked only once the wiring holds."""
    micro = Grelmicro(uses=[MemoryProvider()])
    app = FastAPI()

    def refuse(*_: object, **__: object) -> None:
        msg = "wiring refused"
        raise RuntimeError(msg)

    with monkeypatch.context() as patched:
        patched.setattr(fastapi_integration, "install", refuse)
        with pytest.raises(RuntimeError, match="wiring refused"):
            micro.install(app)
    micro.install(app)

    with TestClient(app):
        assert micro.opened


def test_a_provider_that_cannot_tell_it_is_open_counts_as_closed() -> None:
    """The default answer, for a third-party Provider with no open state."""

    class Opaque(Provider):
        short_name = "opaque"

        async def __aexit__(self, *_: object) -> None:
            return None

    provider = Opaque()
    assert not provider._left_closed()
    provider._skips = 1
    assert provider._left_closed()


async def test_one_micro_installs_on_two_faststream_apps() -> None:
    """An app factory building a new FastStream app per test wires each."""
    micro = Grelmicro(uses=[MemoryProvider()])
    micro.install(FastStream(RedisBroker()))
    broker = RedisBroker()
    second = FastStream(broker)
    micro.install(second)

    async with TestRedisBroker(broker):
        await second.start()
        assert micro.opened
        await second.stop()


async def test_a_valkey_provider_the_test_opens_itself_works_under_fake() -> (
    None
):
    """Valkey tracks its own open scope, like Redis."""
    valkey = ValkeyProvider("valkey://127.0.0.1:1/0")
    micro = Grelmicro(uses=[valkey])

    async with micro.fake(), micro:
        async with valkey:
            assert valkey.client is not None
        with pytest.raises(OutOfContextError, match=r"fake\(keep="):
            _ = valkey.client


async def test_a_provider_nothing_faked_used_stays_open() -> None:
    """A Provider the handlers query directly is not the fake's to close."""
    postgres = PostgresProvider(_CLOSED_PORT)
    redis = RedisProvider("redis://127.0.0.1:1/0")
    micro = Grelmicro(uses=[postgres, redis, Cache(redis)])

    with pytest.raises(OSError, match=r"127\.0\.0\.1"):
        async with micro.fake(), micro:
            pass  # pragma: no cover


async def test_a_faked_run_leaves_no_readiness_check_behind(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each run checks the Providers it opens, faked or real."""
    health = HealthChecks(auto_health=True)
    micro = Grelmicro(uses=[Cache(PostgresProvider(_CLOSED_PORT)), health])

    with caplog.at_level(logging.WARNING, logger="grelmicro"):
        for _ in range(2):
            async with micro.fake(), micro:
                assert "provider:memory" in (await health.run())["checks"]

    assert "already registered" not in caplog.text
    assert "provider:memory" not in (await health.run())["checks"]


async def test_a_registration_made_while_faked_outlives_the_run() -> None:
    """`micro.use(...)` on a faked app is kept, as on a real run."""
    micro = Grelmicro(uses=[RedisProvider("redis://127.0.0.1:1/0")])
    sessions = Cache(MemoryCacheAdapter(), name="sessions")

    async with micro.fake(), micro:
        micro.use(sessions)

    assert micro.get(Cache, "sessions") is sessions
    assert type(micro.get(Cache).backend).__name__ == "RedisCacheAdapter"
