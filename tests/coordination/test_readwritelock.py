"""Test Read-Write Lock."""

import asyncio
import threading
from collections.abc import AsyncGenerator
from datetime import timedelta
from types import TracebackType
from typing import Self

import pytest
from pytest_mock import MockerFixture

from grelmicro import Grelmicro
from grelmicro._config import reconfigure_all
from grelmicro.coordination import (
    Coordination,
    LockAcquireError,
    LockExtendError,
    LockNotOwnedError,
    LockOwnedCheckError,
    LockReentrantError,
    LockReleaseError,
    LockUpgradeError,
    ReadWriteLock,
    ReadWriteLockConfig,
)
from grelmicro.coordination._protocol import (
    ReadWriteLockState,
    WriteGrant,
)
from grelmicro.coordination.errors import (
    CoordinationBackendError,
)
from grelmicro.coordination.memory import MemoryReadWriteLockAdapter
from grelmicro.errors import (
    EventLoopDeadlockError,
    LockTimeoutError,
    OutOfContextError,
    SettingsValidationError,
    WouldBlockError,
)

pytestmark = [pytest.mark.timeout(10, func_only=True)]

_READERS = 3
_THREAD_READERS = 2
_RETAKEN_FENCE = 2
_ENV_LEASE_DURATION = 12
_RECONFIGURED_LEASE_DURATION = timedelta(seconds=30)


@pytest.fixture
async def backend() -> AsyncGenerator[MemoryReadWriteLockAdapter]:
    """In-process backend for the primitive under test."""
    async with MemoryReadWriteLockAdapter() as backend:
        yield backend


@pytest.fixture
def lock(backend: MemoryReadWriteLockAdapter) -> ReadWriteLock:
    """Read-write lock bound to the in-process backend."""
    return ReadWriteLock(
        "catalog", backend=backend, lease_duration=5, retry_interval=0.001
    )


class _FailingBackend:
    """Backend whose every call raises."""

    _loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def acquire_read(
        self, *, name: str, token: str, duration: timedelta
    ) -> int | None:
        raise RuntimeError(name or token or duration)

    async def acquire_write(
        self, *, name: str, token: str, duration: timedelta, intent: bool = True
    ) -> WriteGrant | None:
        raise RuntimeError(name or token or duration or intent)

    async def release_read(self, *, name: str, token: str) -> bool:
        raise RuntimeError(name or token)

    async def release_write(self, *, name: str, token: str) -> bool:
        raise RuntimeError(name or token)

    async def cancel_intent(self, *, name: str, token: str) -> bool:
        raise RuntimeError(name or token)

    async def downgrade(
        self, *, name: str, token: str, duration: timedelta
    ) -> int | None:
        raise RuntimeError(name or token or duration)

    async def state(self, *, name: str) -> ReadWriteLockState:
        raise RuntimeError(name)

    async def owned_read(self, *, name: str, token: str) -> bool:
        raise RuntimeError(name or token)

    async def owned_write(self, *, name: str, token: str) -> bool:
        raise RuntimeError(name or token)


async def test_read_and_write_context_managers(lock: ReadWriteLock) -> None:
    """The two views bind their own guard and release on exit."""
    async with lock.read as reading:
        assert reading.name == "catalog"
        assert reading.generation == 0
        assert reading.valid
        assert reading.expires_in > 0

    async with lock.write as writing:
        assert writing.fencing_token == 1
        assert not writing.poisoned

    assert await lock.state() == ReadWriteLockState(
        generation=1, writing=False, readers=0, waiting_writers=0
    )


async def test_readers_run_together(lock: ReadWriteLock) -> None:
    """Several tasks hold the read lock at the same time."""
    inside = asyncio.Event()
    peak = 0
    current = 0

    async def reader() -> None:
        nonlocal peak, current
        async with lock.read:
            current += 1
            peak = max(peak, current)
            if current == _READERS:
                inside.set()
            await inside.wait()
            current -= 1

    await asyncio.gather(*[reader() for _ in range(_READERS)])

    assert peak == _READERS


async def test_writer_waits_for_the_reader(lock: ReadWriteLock) -> None:
    """A writer retries until the reader releases."""
    leaving = asyncio.Event()

    async def reader() -> None:
        async with lock.read:
            await asyncio.sleep(0.05)
            leaving.set()

    async def writer() -> int:
        async with lock.write as writing:
            assert leaving.is_set()
            return writing.fencing_token

    reader_task = asyncio.create_task(reader())
    await asyncio.sleep(0)
    fencing_token = await writer()
    await reader_task

    assert fencing_token == 1


async def test_waiting_writer_holds_new_readers_out(
    lock: ReadWriteLock, backend: MemoryReadWriteLockAdapter
) -> None:
    """The writer's intent stops a reader that arrives after it."""

    async def hold_read() -> None:
        async with lock.read:
            await asyncio.sleep(0.15)

    async def write_and_release() -> int:
        async with lock.write as writing:
            return writing.fencing_token

    reader_task = asyncio.create_task(hold_read())
    await asyncio.sleep(0.01)
    writer_task = asyncio.create_task(write_and_release())
    await asyncio.sleep(0.02)

    assert (await lock.state()).waiting_writers == 1
    assert (
        await backend.acquire_read(
            name="rwlock:catalog",
            token="latecomer",
            duration=timedelta(seconds=5),
        )
        is None
    )

    await reader_task
    assert await writer_task == 1


async def test_write_timeout_withdraws_the_intent(
    lock: ReadWriteLock,
) -> None:
    """A writer that gives up stops holding readers out."""

    async def hold_read() -> None:
        async with lock.read:
            await asyncio.sleep(0.2)

    reader_task = asyncio.create_task(hold_read())
    await asyncio.sleep(0.01)

    with pytest.raises(TimeoutError):
        await lock.write.acquire(timeout=0.02)

    assert (await lock.state()).waiting_writers == 0
    await reader_task


async def test_acquire_nowait_refuses(lock: ReadWriteLock) -> None:
    """A non-blocking try raises rather than waiting."""

    async def read_nowait() -> None:
        await lock.read.acquire_nowait()

    async def write_nowait() -> None:
        await lock.write.acquire_nowait()

    async with lock.write:
        with pytest.raises(WouldBlockError):
            await asyncio.create_task(read_nowait())

    async with lock.read:
        with pytest.raises(WouldBlockError):
            await asyncio.create_task(write_nowait())


async def test_nested_acquire_is_refused(lock: ReadWriteLock) -> None:
    """The same task cannot take either view twice."""
    async with lock.read:
        with pytest.raises(LockReentrantError):
            await lock.read.acquire()

    async with lock.write:
        with pytest.raises(LockReentrantError):
            await lock.write.acquire()
        with pytest.raises(LockReentrantError):
            await lock.read.acquire()


async def test_nested_acquire_nowait_is_refused(lock: ReadWriteLock) -> None:
    """A non-blocking try is refused the same way as a waiting acquire."""
    async with lock.read:
        with pytest.raises(LockReentrantError):
            await lock.read.acquire_nowait()
        with pytest.raises(LockUpgradeError):
            await lock.write.acquire_nowait()

    async with lock.write:
        with pytest.raises(LockReentrantError):
            await lock.write.acquire_nowait()


@pytest.mark.parametrize(
    ("mode", "method"),
    [
        ("read", "acquire"),
        ("read", "acquire_nowait"),
        ("write", "acquire"),
        ("write", "acquire_nowait"),
    ],
)
async def test_acquire_token_carries_the_worker(
    backend: MemoryReadWriteLockAdapter, mode: str, method: str
) -> None:
    """Every acquire path presents a token led by the configured worker."""
    lock = ReadWriteLock("catalog", backend=backend, worker="web-1")
    side = getattr(lock, mode)

    guard = await getattr(side, method)()

    assert guard.token.startswith("web-1:")
    await side.release()


@pytest.mark.parametrize("mode", ["read", "write"])
async def test_from_thread_token_carries_the_worker(
    backend: MemoryReadWriteLockAdapter, mode: str
) -> None:
    """A worker thread presents a token led by the configured worker."""
    lock = ReadWriteLock("catalog", backend=backend, worker="web-1")
    side = getattr(lock, mode)

    def body() -> str:
        with side.from_thread as guard:
            return guard.token

    token = await asyncio.to_thread(body)

    assert token.startswith("web-1:")


async def test_from_thread_readers_hold_their_own_leases(
    lock: ReadWriteLock,
) -> None:
    """Two worker threads reading at once hold two separate read leases."""
    leave = threading.Event()
    held = [threading.Event(), threading.Event()]

    def read(entered: threading.Event) -> None:
        with lock.read.from_thread:
            entered.set()
            leave.wait()

    threads = [asyncio.create_task(asyncio.to_thread(read, e)) for e in held]
    for entered in held:
        await asyncio.to_thread(entered.wait)

    readers = (await lock.state()).readers
    leave.set()
    await asyncio.gather(*threads)

    assert readers == _THREAD_READERS


async def test_from_thread_writer_excludes_another_thread(
    lock: ReadWriteLock,
) -> None:
    """A second worker thread cannot take the write lock a first one holds."""
    entered = threading.Event()
    leave = threading.Event()

    def write() -> None:
        with lock.write.from_thread:
            entered.set()
            leave.wait()

    def try_write() -> None:
        with lock.write.from_thread.hold(timeout=0):
            pass

    holder = asyncio.create_task(asyncio.to_thread(write))
    await asyncio.to_thread(entered.wait)
    try:
        with pytest.raises(LockTimeoutError):
            await asyncio.to_thread(try_write)
    finally:
        leave.set()
        await holder


async def test_upgrade_is_refused(lock: ReadWriteLock) -> None:
    """A reader asking for the write lock gets an error, not a deadlock."""
    async with lock.read:
        with pytest.raises(LockUpgradeError):
            await lock.write.acquire()


async def test_downgrade_keeps_the_lock(lock: ReadWriteLock) -> None:
    """A downgrade turns the write lease into a read lease with no gap."""
    async with lock.write as writing:
        reading = await writing.downgrade()

        assert reading.generation == 1
        assert not writing.valid
        assert await lock.read.owned()
        assert not await lock.write.owned()

    assert await lock.state() == ReadWriteLockState(
        generation=1, writing=False, readers=0, waiting_writers=0
    )


async def test_downgrade_from_another_task_is_refused(
    lock: ReadWriteLock,
) -> None:
    """Only the task holding the write lease can downgrade it."""
    async with lock.write as writing:
        with pytest.raises(LockNotOwnedError):
            await asyncio.create_task(writing.downgrade())

        assert writing.valid
        assert await lock.write.owned()
        reading = await writing.downgrade()
        assert await lock.read.owned()

    assert not reading.valid
    assert await lock.state() == ReadWriteLockState(
        generation=1, writing=False, readers=0, waiting_writers=0
    )


async def test_downgrade_from_another_task_leaves_the_writer_releasing(
    lock: ReadWriteLock,
) -> None:
    """A refused downgrade leaves the holder free to release the write lease."""
    async with lock.write as writing:
        with pytest.raises(LockNotOwnedError):
            await asyncio.create_task(writing.downgrade())

    assert not writing.valid
    assert await lock.state() == ReadWriteLockState(
        generation=1, writing=False, readers=0, waiting_writers=0
    )


async def test_downgrade_of_a_thread_write_lease_is_refused(
    lock: ReadWriteLock,
) -> None:
    """A write lease taken from a thread cannot be downgraded from a task."""

    def body() -> None:
        writing = lock.write.from_thread.acquire()
        with pytest.raises(LockNotOwnedError):
            asyncio.run(writing.downgrade())
        assert writing.valid
        lock.write.from_thread.release()

    await asyncio.to_thread(body)

    assert await lock.state() == ReadWriteLockState(
        generation=1, writing=False, readers=0, waiting_writers=0
    )


async def test_downgrade_after_losing_the_lease(
    lock: ReadWriteLock, backend: MemoryReadWriteLockAdapter
) -> None:
    """A downgrade without the write lease raises."""
    writing = await lock.write.acquire()
    await backend.release_write(name="rwlock:catalog", token=writing.token)

    with pytest.raises(LockNotOwnedError):
        await writing.downgrade()

    assert not writing.valid


async def test_guard_after_release(lock: ReadWriteLock) -> None:
    """A spent guard hands back no token."""
    async with lock.write as writing:
        assert writing.fencing_token == 1

    assert not writing.valid
    assert writing.poisoned is False
    with pytest.raises(LockNotOwnedError):
        _ = writing.fencing_token

    async with lock.read as reading:
        assert reading.generation == 1
    with pytest.raises(LockNotOwnedError):
        _ = reading.generation


async def test_guard_repr(lock: ReadWriteLock) -> None:
    """Guards print what they hold."""
    async with lock.write as writing:
        assert "fencing_token=1" in repr(writing)
    async with lock.read as reading:
        assert "generation=1" in repr(reading)


async def test_extend_renews_the_lease(lock: ReadWriteLock) -> None:
    """Extending keeps the same token and pushes the deadline out."""
    async with lock.write as writing:
        before = writing.expires_in
        await asyncio.sleep(0.01)
        await writing.extend()

        assert writing.fencing_token == 1
        assert writing.expires_in >= before - 0.01

    async with lock.read as reading:
        await reading.extend()
        assert reading.generation == 1
        assert (await lock.state()).readers == 1
        assert await lock.read.owned()


async def test_extend_of_a_lost_write_lease_records_no_intent(
    lock: ReadWriteLock, backend: MemoryReadWriteLockAdapter
) -> None:
    """An extend that finds readers in place of its lease holds no one out."""
    writing = await lock.write.acquire()
    await backend.release_write(name="rwlock:catalog", token=writing.token)
    await backend.acquire_read(
        name="rwlock:catalog", token="reader", duration=timedelta(seconds=5)
    )

    with pytest.raises(LockNotOwnedError):
        await writing.extend()

    assert (await lock.state()).waiting_writers == 0


async def test_refused_write_acquire_nowait_records_no_intent(
    lock: ReadWriteLock, backend: MemoryReadWriteLockAdapter
) -> None:
    """A refused non-blocking write try lets later readers in."""
    await backend.acquire_read(
        name="rwlock:catalog", token="reader", duration=timedelta(seconds=5)
    )

    with pytest.raises(WouldBlockError):
        await lock.write.acquire_nowait()

    assert (await lock.state()).waiting_writers == 0


async def test_extend_reports_a_lost_read_lease(
    lock: ReadWriteLock, backend: MemoryReadWriteLockAdapter
) -> None:
    """A read lease taken over while paused surfaces on the next extend."""
    reading = await lock.read.acquire()
    await backend.release_read(name="rwlock:catalog", token=reading.token)
    await backend.acquire_write(
        name="rwlock:catalog",
        token="someone-else",
        duration=timedelta(seconds=5),
    )

    with pytest.raises(LockNotOwnedError):
        await reading.extend()
    assert not reading.valid
    await backend.release_write(name="rwlock:catalog", token="someone-else")


async def test_extend_reports_a_retaken_write_lease(
    lock: ReadWriteLock, backend: MemoryReadWriteLockAdapter
) -> None:
    """A write lease lost and retaken carries the new token, marked poisoned."""
    writing = await lock.write.acquire()
    await backend.release_write(name="rwlock:catalog", token=writing.token)

    await writing.extend()

    assert writing.fencing_token == _RETAKEN_FENCE
    assert writing.poisoned
    await lock.write.release()


async def test_extend_after_the_write_lease_moved(
    lock: ReadWriteLock, backend: MemoryReadWriteLockAdapter
) -> None:
    """Extending after another writer took over raises."""
    writing = await lock.write.acquire()
    await backend.release_write(name="rwlock:catalog", token=writing.token)
    await backend.acquire_write(
        name="rwlock:catalog",
        token="someone-else",
        duration=timedelta(seconds=5),
    )

    with pytest.raises(LockNotOwnedError):
        await writing.extend()
    assert not writing.valid


@pytest.mark.parametrize("mode", ["read", "write"])
async def test_readwritelock_extend_backend_failure_raises_lock_extend_error(
    lock: ReadWriteLock,
    backend: MemoryReadWriteLockAdapter,
    mocker: MockerFixture,
    mode: str,
) -> None:
    """Extending a lease raises LockExtendError when the backend call fails."""
    # Arrange
    side = getattr(lock, mode)
    await side.acquire()
    mocker.patch.object(
        backend, f"acquire_{mode}", side_effect=RuntimeError("down")
    )

    # Act & Assert
    with pytest.raises(LockExtendError):
        await side.extend()


@pytest.mark.parametrize("mode", ["read", "write"])
async def test_readwritelock_guard_extend_backend_failure_raises_lock_extend_error(
    lock: ReadWriteLock,
    backend: MemoryReadWriteLockAdapter,
    mocker: MockerFixture,
    mode: str,
) -> None:
    """Extending a guard raises LockExtendError when the backend call fails."""
    # Arrange
    guard = await getattr(lock, mode).acquire()
    mocker.patch.object(
        backend, f"acquire_{mode}", side_effect=RuntimeError("down")
    )

    # Act & Assert
    with pytest.raises(LockExtendError):
        await guard.extend()


@pytest.mark.parametrize("mode", ["read", "write"])
async def test_readwritelock_from_thread_extend_backend_failure_raises_lock_extend_error(
    lock: ReadWriteLock,
    backend: MemoryReadWriteLockAdapter,
    mocker: MockerFixture,
    mode: str,
) -> None:
    """A thread extend raises LockExtendError when the backend call fails."""
    # Arrange
    side = getattr(lock, mode)

    # Act & Assert: acquire, extend and release run on one thread.
    def body() -> None:
        side.from_thread.acquire()
        mocker.patch.object(
            backend,
            f"acquire_{mode}",
            mocker.AsyncMock(side_effect=RuntimeError("down")),
        )
        with pytest.raises(LockExtendError):
            side.from_thread.extend()
        side.from_thread.release()

    await asyncio.to_thread(body)


async def test_release_without_holding(lock: ReadWriteLock) -> None:
    """Releasing what this task never took raises."""
    with pytest.raises(LockNotOwnedError):
        await lock.read.release()
    with pytest.raises(LockNotOwnedError):
        await lock.write.release()
    with pytest.raises(LockNotOwnedError):
        await lock.read.extend()
    with pytest.raises(LockNotOwnedError):
        await lock.write.extend()


async def test_release_of_a_lost_lease(
    lock: ReadWriteLock, backend: MemoryReadWriteLockAdapter
) -> None:
    """A lease already gone on the backend reports as not owned."""
    reading = await lock.read.acquire()
    await backend.release_read(name="rwlock:catalog", token=reading.token)
    with pytest.raises(LockNotOwnedError):
        await lock.read.release()

    writing = await lock.write.acquire()
    await backend.release_write(name="rwlock:catalog", token=writing.token)
    with pytest.raises(LockNotOwnedError):
        await lock.write.release()


async def test_owned_without_holding(lock: ReadWriteLock) -> None:
    """A task that holds nothing owns nothing."""
    assert not await lock.read.owned()
    assert not await lock.write.owned()


async def test_owned_while_holding(lock: ReadWriteLock) -> None:
    """A task holding the write lease owns it, and only the write view says so."""
    async with lock.write:
        assert await lock.write.owned()
        assert not await lock.read.owned()


async def test_backend_errors_are_wrapped() -> None:
    """A backend that raises surfaces as a coordination error."""
    lock = ReadWriteLock("catalog", backend=_FailingBackend())

    with pytest.raises(LockAcquireError):
        await lock.read.acquire_nowait()
    with pytest.raises(LockAcquireError):
        await lock.write.acquire_nowait()
    with pytest.raises(LockOwnedCheckError):
        await lock.state()


async def test_release_errors_are_wrapped(
    backend: MemoryReadWriteLockAdapter,
) -> None:
    """A backend that raises on release surfaces as a release error."""
    lock = ReadWriteLock("catalog", backend=backend, lease_duration=5)
    guard = await lock.read.acquire()
    lock._backend = _FailingBackend()

    with pytest.raises(LockReleaseError):
        await lock.read.release()
    with pytest.raises(LockOwnedCheckError):
        await lock.read.owned()

    lock._backend = backend
    await backend.release_read(name="rwlock:catalog", token=guard.token)


async def test_write_release_and_owned_errors_are_wrapped(
    backend: MemoryReadWriteLockAdapter,
) -> None:
    """The write view wraps backend failures the same way."""
    lock = ReadWriteLock("catalog", backend=backend, lease_duration=5)
    guard = await lock.write.acquire()
    lock._backend = _FailingBackend()

    with pytest.raises(LockReleaseError):
        await lock.write.release()
    with pytest.raises(LockOwnedCheckError):
        await lock.write.owned()
    with pytest.raises(LockAcquireError):
        await guard.downgrade()

    lock._backend = backend
    await backend.release_write(name="rwlock:catalog", token=guard.token)


async def test_backend_resolves_from_the_app(
    backend: MemoryReadWriteLockAdapter,
) -> None:
    """A lock with no backend resolves through the active app."""
    micro = Grelmicro(
        uses=[Coordination(readwritelock=backend, name="default")]
    )
    lock = ReadWriteLock("catalog", lease_duration=5)

    async with micro, lock.write as writing:
        assert writing.fencing_token == 1


async def test_backend_without_an_app() -> None:
    """A lock with nothing wired says what to do about it."""
    lock = ReadWriteLock("catalog")

    with pytest.raises(OutOfContextError, match="Register a Coordination"):
        await lock.read.acquire_nowait()


async def test_component_without_a_backend() -> None:
    """A component with no read-write lock backend says so."""
    component = Coordination()

    with pytest.raises(CoordinationBackendError, match="readwritelock"):
        _ = component.readwritelock_backend


async def test_component_builds_the_lock(
    backend: MemoryReadWriteLockAdapter,
) -> None:
    """The component hands back a lock bound to its backend."""
    component = Coordination(readwritelock=backend)

    lock = component.readwritelock("catalog", lease_duration=5)

    async with lock.read as reading:
        assert reading.generation == 0


async def test_from_config(backend: MemoryReadWriteLockAdapter) -> None:
    """The declarative path skips the environment."""
    lock = ReadWriteLock.from_config(
        "catalog",
        ReadWriteLockConfig(lease_duration=5, worker="web-1"),
        backend=backend,
    )

    async with lock.write as writing:
        assert writing.token.startswith("web-1:")


async def test_environment_configuration(
    backend: MemoryReadWriteLockAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fields resolve from `GREL_READWRITELOCK_{NAME}_*`."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_READWRITELOCK_CATALOG_LEASE_DURATION", "12")

    lock = ReadWriteLock("catalog", backend=backend)

    assert lock.config.lease_duration == timedelta(seconds=_ENV_LEASE_DURATION)


@pytest.mark.parametrize(
    ("variable", "env_prefix"),
    [
        ("GREL_READWRITELOCK_LEASE_DURATION", None),
        ("MYAPP_RW_LEASE_DURATION", "MYAPP_RW_"),
    ],
)
async def test_environment_configuration_prefixes(
    backend: MemoryReadWriteLockAdapter,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    env_prefix: str | None,
) -> None:
    """A named lock reads the kind-wide prefix, or the `env_prefix=` override."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv(variable, "12")

    lock = ReadWriteLock("catalog", backend=backend, env_prefix=env_prefix)

    assert lock.config.lease_duration == timedelta(seconds=_ENV_LEASE_DURATION)


async def test_environment_configuration_env_load_false(
    backend: MemoryReadWriteLockAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`env_load=False` skips the environment even when the flag enables it."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv("GREL_READWRITELOCK_CATALOG_LEASE_DURATION", "12")

    lock = ReadWriteLock("catalog", backend=backend, env_load=False)

    assert lock.config.lease_duration == ReadWriteLockConfig().lease_duration


async def test_external_config_reloads_the_lock(
    backend: MemoryReadWriteLockAdapter,
) -> None:
    """A reload keyed on the lock's own prefix reconfigures it."""
    lock = ReadWriteLock("catalog", backend=backend, lease_duration=5)

    await reconfigure_all({"GREL_READWRITELOCK_CATALOG_LEASE_DURATION": "30"})

    assert lock.config.lease_duration == _RECONFIGURED_LEASE_DURATION


async def test_reconfigure_keeps_the_worker(
    backend: MemoryReadWriteLockAdapter,
) -> None:
    """The worker identity cannot change under a live lease."""
    lock = ReadWriteLock("catalog", backend=backend, worker="web-1")

    await lock.reconfigure(
        lock.config.model_copy(
            update={"lease_duration": _RECONFIGURED_LEASE_DURATION}
        )
    )
    assert lock.config.lease_duration == _RECONFIGURED_LEASE_DURATION

    with pytest.raises(SettingsValidationError, match="worker is immutable"):
        await lock.reconfigure(
            lock.config.model_copy(update={"worker": "web-2"})
        )


async def test_from_thread(lock: ReadWriteLock) -> None:
    """Worker threads take both views through the adapters."""

    def body() -> tuple[int, int]:
        with lock.read.from_thread as reading:
            lock.read.from_thread.extend()
            generation = reading.generation
        with lock.write.from_thread as writing:
            lock.write.from_thread.extend()
            fencing_token = writing.fencing_token
        return generation, fencing_token

    generation, fencing_token = await asyncio.to_thread(body)

    assert generation == 0
    assert fencing_token == 1


async def test_from_thread_rejects_nesting(lock: ReadWriteLock) -> None:
    """A thread cannot take a view twice, nor upgrade."""

    def body() -> None:
        with lock.read.from_thread:
            with pytest.raises(LockReentrantError):
                lock.read.from_thread.acquire()
            with pytest.raises(LockUpgradeError):
                lock.write.from_thread.acquire()
        with lock.write.from_thread, pytest.raises(LockReentrantError):
            lock.write.from_thread.acquire()

    await asyncio.to_thread(body)


async def test_from_thread_without_holding(lock: ReadWriteLock) -> None:
    """A thread that holds nothing cannot release or extend."""

    def body() -> None:
        with pytest.raises(LockNotOwnedError):
            lock.read.from_thread.release()
        with pytest.raises(LockNotOwnedError):
            lock.write.from_thread.release()
        with pytest.raises(LockNotOwnedError):
            lock.read.from_thread.extend()
        with pytest.raises(LockNotOwnedError):
            lock.write.from_thread.extend()

    await asyncio.to_thread(body)


async def test_from_thread_needs_an_open_backend() -> None:
    """A closed backend says so rather than hanging the thread."""
    lock = ReadWriteLock("catalog", backend=MemoryReadWriteLockAdapter())

    def body() -> None:
        with pytest.raises(RuntimeError, match="before its backend is opened"):
            lock.read.from_thread.acquire()

    await asyncio.to_thread(body)


async def test_from_thread_on_the_event_loop_thread_is_refused(
    lock: ReadWriteLock,
) -> None:
    """A call made from the backend's own loop would wait on itself.

    Both views refuse, and each names the one it belongs to.
    """
    with pytest.raises(EventLoopDeadlockError, match=r"`read\.from_thread`"):
        lock.read.from_thread.acquire()
    with pytest.raises(EventLoopDeadlockError, match=r"`write\.from_thread`"):
        lock.write.from_thread.acquire()

    assert (await lock.state()).readers == 0


async def test_from_thread_on_another_loop_is_served(
    lock: ReadWriteLock,
) -> None:
    """Only the backend's own loop would wait on itself."""

    def read_on_a_second_loop() -> int:
        async def inner() -> int:
            with lock.read.from_thread as guard:
                return guard.generation

        return asyncio.run(inner())

    assert await asyncio.to_thread(read_on_a_second_loop) == 0


async def test_acquire_nowait_grants(lock: ReadWriteLock) -> None:
    """A non-blocking try that finds the lock free hands back a guard."""
    reading = await lock.read.acquire_nowait()
    assert reading.generation == 0
    await lock.read.release()

    writing = await lock.write.acquire_nowait()
    assert writing.fencing_token == 1
    await lock.write.release()


async def test_from_thread_release_of_a_lost_lease(
    lock: ReadWriteLock, backend: MemoryReadWriteLockAdapter
) -> None:
    """A thread releasing a lease that is already gone is told so."""

    def read_body() -> None:
        guard = lock.read.from_thread.acquire()
        loop = backend._loop
        assert loop is not None
        asyncio.run_coroutine_threadsafe(
            backend.release_read(name="rwlock:catalog", token=guard.token),
            loop,
        ).result()
        with pytest.raises(LockNotOwnedError):
            lock.read.from_thread.release()

    def write_body() -> None:
        guard = lock.write.from_thread.acquire()
        loop = backend._loop
        assert loop is not None
        asyncio.run_coroutine_threadsafe(
            backend.release_write(name="rwlock:catalog", token=guard.token),
            loop,
        ).result()
        with pytest.raises(LockNotOwnedError):
            lock.write.from_thread.release()

    await asyncio.to_thread(read_body)
    await asyncio.to_thread(write_body)


async def test_expired_intent_is_reaped(
    backend: MemoryReadWriteLockAdapter,
) -> None:
    """An intent left by a writer that died stops holding readers out."""
    await backend.acquire_read(
        name="catalog", token="reader", duration=timedelta(seconds=10)
    )
    await backend.acquire_write(
        name="catalog", token="dead", duration=timedelta(milliseconds=50)
    )
    await asyncio.sleep(0.1)

    state = await backend.state(name="catalog")

    assert state.waiting_writers == 0
    assert (
        await backend.acquire_read(
            name="catalog", token="latecomer", duration=timedelta(seconds=10)
        )
        is not None
    )
