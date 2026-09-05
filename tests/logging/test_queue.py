"""Tests for the queued writer that keeps log writes off the calling thread."""

import contextlib
import io
import json
import logging
import multiprocessing
import multiprocessing.util
import os
import sys
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path

import anyio
import pytest
import structlog
from loguru import logger as loguru_logger

from grelmicro._context import pop_context, push_context
from grelmicro.errors import DependencyNotFoundError
from grelmicro.log import Log, _queue, _stdlib, configure
from grelmicro.log._queue import (
    _MAX_BATCH,
    QueueWriter,
    _after_fork_in_child,
    _after_fork_in_parent,
    _before_fork,
    get_stream,
    get_writer,
    swap,
    uninstall,
)
from grelmicro.log.config import (
    LogBackendType,
    LogConfig,
    LogFormatType,
)
from tests.logging.conftest import BACKENDS, log_message, parse_json_logs

_TIMEOUT = 5.0
_DEFAULT_QUEUE_SIZE = 10_000
_BACKLOG = 10
_BURST = 201
_QUEUED_BEFORE_DRAIN = 2


class _Stream:
    """Stream that records what the worker wrote."""

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.flushes = 0
        self.written = threading.Event()

    def write(self, text: str) -> int:
        """Record the chunk."""
        self.chunks.append(text)
        self.written.set()
        return len(text)

    def flush(self) -> None:
        """Count the flush."""
        self.flushes += 1

    @property
    def text(self) -> str:
        """Return everything written so far."""
        return "".join(self.chunks)


class _FailingStream(_Stream):
    """Stream whose first `failures` writes raise."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def write(self, text: str) -> int:
        """Raise while failures remain, then record the chunk."""
        if self.failures:
            self.failures -= 1
            msg = "stream is gone"
            raise OSError(msg)
        return super().write(text)


class _GatedStream(_Stream):
    """Stream the test steps through one write at a time."""

    def __init__(self) -> None:
        super().__init__()
        self.permits = threading.Semaphore(0)
        self.arrived = threading.Semaphore(0)

    def write(self, text: str) -> int:
        """Announce arrival, wait for a permit, then record the chunk."""
        self.arrived.release()
        self.permits.acquire()
        return super().write(text)

    def wait_for_write(self) -> None:
        """Block until the worker is inside a write."""
        assert self.arrived.acquire(timeout=_TIMEOUT)

    def let_one_through(self) -> None:
        """Release the write the worker is parked on."""
        self.permits.release()


class _BlockedStream(_Stream):
    """Stream that holds the worker until released."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.entered = threading.Event()

    def write(self, text: str) -> int:
        """Block until released, then record the chunk."""
        self.entered.set()
        self.release.wait(_TIMEOUT)
        return super().write(text)


@pytest.fixture
def clean_queue() -> Generator[None, None, None]:
    """Remove any writer a test installed."""
    yield
    uninstall()


def _wait_for(predicate: Callable[[], bool]) -> None:
    """Wait until `predicate` holds, or fail the test."""
    deadline = time.monotonic() + _TIMEOUT
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail("condition not reached within the timeout")


def test_writer_hands_the_write_to_the_worker() -> None:
    """The line reaches the stream from the worker, not the caller."""
    stream = _Stream()
    writer = QueueWriter(stream, size=10)

    caller = threading.current_thread().name
    assert writer.write("hello\n") == len("hello\n")
    writer.shutdown()

    assert stream.text == "hello\n"
    assert stream.flushes >= 1
    assert caller == threading.current_thread().name


def test_flush_does_not_wait_for_the_queue() -> None:
    """`flush` returns while the worker is still blocked on a write."""
    stream = _BlockedStream()
    writer = QueueWriter(stream, size=10)
    writer.write("first\n")
    stream.entered.wait(_TIMEOUT)

    writer.write("second\n")
    writer.flush()

    assert stream.chunks == []
    stream.release.set()
    writer.shutdown()
    assert stream.text == "first\nsecond\n"


def test_shutdown_writes_what_is_queued() -> None:
    """Shutting down drains rather than discards."""
    stream = _BlockedStream()
    writer = QueueWriter(stream, size=100)
    writer.write("a\n")
    stream.entered.wait(_TIMEOUT)
    for index in range(_BACKLOG):
        writer.write(f"line{index}\n")

    stream.release.set()
    writer.shutdown()

    assert stream.text.count("\n") == _BACKLOG + 1


def test_shutdown_is_idempotent() -> None:
    """A second stop is a no-op rather than a second sentinel."""
    writer = QueueWriter(_Stream(), size=10)
    writer.shutdown()
    writer.shutdown()


def test_full_queue_drops_the_arriving_record() -> None:
    """The caller is never blocked, and the drop is counted."""
    stream = _BlockedStream()
    writer = QueueWriter(stream, size=2)
    writer.write("held\n")
    stream.entered.wait(_TIMEOUT)

    for index in range(20):
        writer.write(f"line{index}\n")

    assert writer.dropped > 0
    stream.release.set()
    writer.shutdown()
    # The queue took two, so the two oldest arrivals survive.
    assert "line0\n" in stream.text
    assert "line19\n" not in stream.text


def test_drops_are_reported_once_the_queue_drains(
    reset_backend: None, clean_queue: None
) -> None:
    """One summary record names the count, rather than one per drop."""
    _ = reset_backend, clean_queue
    stream = io.StringIO()
    original, sys.stdout = sys.stdout, stream
    try:
        configure(
            backend=LogBackendType.STDLIB,
            format=LogFormatType.JSON,
            queue_enabled=True,
            queue_size=1,
            env_load=False,
        )
        logger = logging.getLogger("app")
        for index in range(300):
            logger.info("burst %d", index)
        writer = get_writer()
        assert writer is not None
        _wait_for(lambda: "Log queue full" in stream.getvalue())
        uninstall()
    finally:
        sys.stdout = original

    reports = [
        record
        for record in parse_json_logs(stream.getvalue())
        if record["logger"] == "grelmicro.log"
    ]
    assert len(reports) >= 1
    assert reports[0]["level"] == "ERROR"
    assert reports[0]["dropped"] > 0
    assert "dropped" in reports[0]["msg"]


def test_write_failure_does_not_kill_the_worker() -> None:
    """A broken stream is survivable, the next line still lands."""
    stream = _FailingStream(failures=1)
    writer = QueueWriter(stream, size=10)
    writer.write("lost\n")
    _wait_for(lambda: stream.failures == 0)
    writer.write("kept\n")
    writer.shutdown()

    assert stream.text == "kept\n"


def test_a_long_backlog_is_written_in_capped_batches() -> None:
    """The worker caps a batch so one drain cannot hold the queue open."""
    stream = _BlockedStream()
    writer = QueueWriter(stream, size=2000)
    writer.write("held\n")
    stream.entered.wait(_TIMEOUT)
    for index in range(_MAX_BATCH * 2):
        writer.write(f"line{index}\n")

    stream.release.set()
    writer.shutdown()

    assert len(stream.chunks) > 1
    assert stream.text.count("\n") == _MAX_BATCH * 2 + 1


def test_a_write_failure_stays_quiet_when_logging_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`logging.raiseExceptions` governs the fallback the way it does elsewhere."""
    monkeypatch.setattr(logging, "raiseExceptions", False)
    errors = io.StringIO()
    monkeypatch.setattr(sys, "stderr", errors)

    stream = _FailingStream(failures=1)
    writer = QueueWriter(stream, size=10)
    writer.write("lost\n")
    writer.shutdown()

    assert errors.getvalue() == ""


def test_fork_in_a_parent_without_a_writer_is_a_no_op(
    clean_queue: None,
) -> None:
    """Nothing to quiesce when the process was not writing through a queue."""
    _ = clean_queue
    uninstall()

    _before_fork()
    _after_fork_in_parent()

    assert get_writer() is None


def test_fork_in_a_child_without_a_writer_is_a_no_op(
    clean_queue: None,
) -> None:
    """Nothing to reopen when the process was not writing through a queue."""
    _ = clean_queue
    uninstall()

    _before_fork()
    _after_fork_in_child()

    assert get_writer() is None


def test_isatty_follows_the_wrapped_stream() -> None:
    """Color detection reads the terminal behind the queue, not the queue."""
    with Path(os.devnull).open("w", encoding="utf-8") as handle:
        writer = QueueWriter(handle, size=10)
        assert writer.isatty() is handle.isatty()
        assert writer.stream is handle
        writer.shutdown()

    plain = QueueWriter(_Stream(), size=1)
    assert plain.isatty() is False
    plain.shutdown()


def test_the_raw_file_descriptor_is_not_offered() -> None:
    """`fileno` would hand out a way to write past the queue and reorder output."""
    writer = QueueWriter(_Stream(), size=1)
    assert not hasattr(writer, "fileno")
    writer.shutdown()


def test_swap_returns_the_previous_writer(clean_queue: None) -> None:
    """The caller stops the old writer once handlers point at the new one."""
    _ = clean_queue
    assert swap(size=10) is None
    first = get_writer()
    assert first is not None

    previous = swap(size=10)

    assert previous is first
    assert get_writer() is not first
    previous.shutdown()


def test_swap_without_a_size_writes_direct(clean_queue: None) -> None:
    """`size=None` takes the queue back out of the path."""
    _ = clean_queue
    swap(size=10)
    assert get_stream() is get_writer()

    previous = swap(size=None)

    assert previous is not None
    previous.shutdown()
    assert get_writer() is None
    assert get_stream() is sys.stdout


@pytest.mark.parametrize("backend", BACKENDS)
def test_every_backend_writes_through_the_queue(
    backend: str, reset_backend: None, clean_queue: None
) -> None:
    """One writer serves loguru, structlog and stdlib alike."""
    _ = reset_backend, clean_queue
    stream = io.StringIO()
    original, sys.stdout = sys.stdout, stream
    try:
        configure(
            backend=LogBackendType(backend),
            format=LogFormatType.JSON,
            queue_enabled=True,
            env_load=False,
        )
        assert isinstance(get_stream(), QueueWriter)
        log_message(backend, "queued line")
        uninstall()
    finally:
        sys.stdout = original

    records = parse_json_logs(stream.getvalue())
    assert [record["msg"] for record in records] == ["queued line"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_context_survives_the_queue(
    backend: str, reset_backend: None, clean_queue: None
) -> None:
    """Rendering stays on the calling thread, so bound fields are kept.

    This is why the queue wraps the stream rather than the record. A
    `QueueHandler` would format on the worker thread, where the context
    variables the record was written under no longer exist.
    """
    _ = reset_backend, clean_queue
    stream = io.StringIO()
    original, sys.stdout = sys.stdout, stream
    token = push_context({"request_id": "abc-123"})
    try:
        configure(
            backend=LogBackendType(backend),
            format=LogFormatType.JSON,
            queue_enabled=True,
            env_load=False,
        )
        log_message(backend, "with context")
        uninstall()
    finally:
        pop_context(token)
        sys.stdout = original

    records = parse_json_logs(stream.getvalue())
    assert records[0]["request_id"] == "abc-123"


def test_reconfigure_writes_out_the_replaced_queue(
    reset_backend: None, clean_queue: None
) -> None:
    """Nothing is stranded on a queue the new handlers no longer read."""
    _ = reset_backend, clean_queue
    stream = io.StringIO()
    original, sys.stdout = sys.stdout, stream
    try:
        configure(
            backend=LogBackendType.STDLIB,
            format=LogFormatType.JSON,
            queue_enabled=True,
            env_load=False,
        )
        logging.getLogger("app").info("before")
        configure(
            backend=LogBackendType.STDLIB,
            format=LogFormatType.JSON,
            queue_enabled=True,
            env_load=False,
        )
        logging.getLogger("app").info("after")
        uninstall()
    finally:
        sys.stdout = original

    messages = [record["msg"] for record in parse_json_logs(stream.getvalue())]
    assert messages == ["before", "after"]


def test_turning_the_queue_off_keeps_the_records(
    reset_backend: None, clean_queue: None
) -> None:
    """Going back to direct writes drains what the queue still held."""
    _ = reset_backend, clean_queue
    stream = io.StringIO()
    original, sys.stdout = sys.stdout, stream
    try:
        configure(
            backend=LogBackendType.STDLIB,
            format=LogFormatType.JSON,
            queue_enabled=True,
            env_load=False,
        )
        logging.getLogger("app").info("queued")
        configure(
            backend=LogBackendType.STDLIB,
            format=LogFormatType.JSON,
            env_load=False,
        )
        assert get_writer() is None
        logging.getLogger("app").info("direct")
    finally:
        sys.stdout = original

    messages = [record["msg"] for record in parse_json_logs(stream.getvalue())]
    assert messages == ["queued", "direct"]


async def test_component_stops_the_worker_on_exit(
    reset_backend: None, clean_queue: None
) -> None:
    """`Log` gives the thread back along with the root handlers."""
    _ = reset_backend, clean_queue
    stream = io.StringIO()
    original, sys.stdout = sys.stdout, stream
    try:
        async with Log(
            backend=LogBackendType.STDLIB,
            format=LogFormatType.JSON,
            queue_enabled=True,
            env_load=False,
        ) as component:
            assert component.config.queue_enabled is True
            assert isinstance(get_stream(), QueueWriter)
            logging.getLogger("app").info("inside")
        assert get_writer() is None
    finally:
        sys.stdout = original

    messages = [record["msg"] for record in parse_json_logs(stream.getvalue())]
    assert messages == ["inside"]
    assert not any(
        thread.name == "grelmicro-log" for thread in threading.enumerate()
    )


def test_fork_drains_and_restarts_the_parent_worker(
    clean_queue: None,
) -> None:
    """The worker is quiesced across the fork, then started again.

    It is stopped rather than merely locked out: it can be inside
    `stream.write` holding the stream's own buffer lock at the instant of
    the fork, and the child would inherit that lock held with no thread
    left to release it.
    """
    _ = clean_queue
    stream = _Stream()
    swap(size=10)
    writer = get_writer()
    assert writer is not None
    writer._stream = stream
    writer.write("before the fork\n")

    _before_fork()

    assert writer._thread is None
    assert stream.text == "before the fork\n"

    _after_fork_in_parent()

    assert writer._thread is not None
    writer.write("after the fork\n")
    writer.shutdown()
    assert stream.text == "before the fork\nafter the fork\n"


def test_fork_gives_the_child_a_fresh_worker(clean_queue: None) -> None:
    """A thread does not survive `fork`, so the child starts its own."""
    _ = clean_queue
    stream = _Stream()
    swap(size=10)
    writer = get_writer()
    assert writer is not None
    writer._stream = stream
    writer.write("before the fork\n")

    _before_fork()
    _after_fork_in_child()

    assert writer._thread is not None
    writer.write("from the child\n")
    writer.shutdown()

    # The parent wrote its backlog before forking, so the child starts on
    # an empty queue and prints none of the parent's lines a second time.
    assert stream.text == "before the fork\nfrom the child\n"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_a_forked_child_still_logs(
    reset_backend: None, clean_queue: None
) -> None:
    """End to end: the child writes its own records after the fork."""
    _ = reset_backend, clean_queue
    read_fd, write_fd = os.pipe()
    configure(
        backend=LogBackendType.STDLIB,
        format=LogFormatType.JSON,
        queue_enabled=True,
        env_load=False,
    )
    writer = get_writer()
    assert writer is not None

    pid = os.fork()
    if pid == 0:  # pragma: no cover
        os.close(read_fd)
        child = io.StringIO()
        writer._stream = child
        logging.getLogger("child").error("from the child")
        uninstall()
        os.write(write_fd, child.getvalue().encode())
        os._exit(0)

    os.close(write_fd)
    payload = os.read(read_fd, 65536).decode()
    os.close(read_fd)
    os.waitpid(pid, 0)

    records = parse_json_logs(payload)
    assert [record["msg"] for record in records] == ["from the child"]


def test_queue_is_off_by_default() -> None:
    """The queue is opt-in until a benchmark earns the default."""
    config = LogConfig()
    assert config.queue_enabled is False
    assert config.queue_size == _DEFAULT_QUEUE_SIZE


def test_queue_size_must_be_positive() -> None:
    """A queue that holds nothing would drop every record."""
    with pytest.raises(ValueError, match="greater than 0"):
        LogConfig(queue_size=0)


def test_a_record_split_across_two_writes_is_queued_as_one_line() -> None:
    """`structlog` prints the message and its newline separately.

    Queueing each call on its own let a full queue drop the newline and
    glue two records onto one line no reader can parse, so the writer
    holds a partial line until its newline arrives.
    """
    stream = _BlockedStream()
    writer = QueueWriter(stream, size=10)
    writer.write("held\n")
    stream.entered.wait(_TIMEOUT)
    depth = writer._queue.qsize()

    writer.write('{"i":0}')

    assert writer._queue.qsize() == depth

    writer.write("\n")

    assert writer._queue.qsize() == depth + 1
    stream.release.set()
    writer.shutdown()
    assert stream.text == 'held\n{"i":0}\n'


def test_a_partial_line_is_held_per_thread() -> None:
    """Two threads writing at once cannot splice their partial lines."""
    stream = _Stream()
    writer = QueueWriter(stream, size=10)
    done = threading.Event()

    def other() -> None:
        writer.write("from-the-other-thread")
        writer.write("\n")
        done.set()

    writer.write("from-the-main-thread")
    thread = threading.Thread(target=other)
    thread.start()
    done.wait(_TIMEOUT)
    thread.join(_TIMEOUT)
    writer.write("\n")
    writer.shutdown()

    assert sorted(stream.text.splitlines()) == [
        "from-the-main-thread",
        "from-the-other-thread",
    ]


def test_structlog_records_stay_parseable_under_backpressure(
    reset_backend: None, clean_queue: None
) -> None:
    """Every line a full queue lets through is still one whole record."""
    _ = reset_backend, clean_queue
    stream = io.StringIO()
    original, sys.stdout = sys.stdout, stream
    try:
        configure(
            backend=LogBackendType.STRUCTLOG,
            format=LogFormatType.JSON,
            queue_enabled=True,
            queue_size=1,
            env_load=False,
        )
        log = structlog.get_logger()
        for index in range(300):
            log.info("burst", i=index)
        uninstall()
    finally:
        sys.stdout = original

    for line in stream.getvalue().splitlines():
        assert json.loads(line)


def test_loguru_remove_does_not_kill_the_shared_writer(
    reset_backend: None, clean_queue: None
) -> None:
    """Loguru stops any sink exposing `stop`, and the writer is shared.

    `logger.remove()` is the ordinary way to swap in a sink of your own.
    It must not take down the worker every backend writes through, or
    every later record, including a dependency's, is lost.
    """
    _ = reset_backend, clean_queue
    stream = io.StringIO()
    original, sys.stdout = sys.stdout, stream
    try:
        configure(
            backend=LogBackendType.LOGURU,
            format=LogFormatType.JSON,
            queue_enabled=True,
            env_load=False,
        )
        writer = get_writer()
        assert writer is not None

        loguru_logger.remove()

        # The property the name protects: the worker every backend shares
        # is still running. Without it the queue silently degrades to a
        # synchronous write and the whole feature is gone.
        assert writer._thread is not None

        logging.getLogger("dependency").error("still reaches the stream")
        uninstall()
    finally:
        sys.stdout = original

    messages = [record["msg"] for record in parse_json_logs(stream.getvalue())]
    assert "still reaches the stream" in messages


def test_records_written_after_shutdown_reach_the_stream() -> None:
    """A stopped writer writes through rather than swallowing the record."""
    stream = _Stream()
    writer = QueueWriter(stream, size=10)
    writer.shutdown()

    writer.write("after shutdown\n")

    assert stream.text == "after shutdown\n"


async def test_a_backend_keeps_writing_after_the_component_exits(
    reset_backend: None, clean_queue: None
) -> None:
    """`Log` restores stdlib handlers, but structlog still holds the writer."""
    _ = reset_backend, clean_queue
    stream = io.StringIO()
    original, sys.stdout = sys.stdout, stream
    try:
        async with Log(
            backend=LogBackendType.STRUCTLOG,
            format=LogFormatType.JSON,
            queue_enabled=True,
            env_load=False,
        ):
            structlog.get_logger().info("inside")
        structlog.get_logger().info("after exit")
    finally:
        sys.stdout = original

    messages = [record["msg"] for record in parse_json_logs(stream.getvalue())]
    assert messages == ["inside", "after exit"]


def test_a_refused_config_keeps_the_working_queue(
    reset_backend: None, clean_queue: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call that raised must leave logging as it found it.

    The backends were never bound to the writer the failed call made, so
    the one that was working goes back and the unused one is stopped.
    Tearing down a queue that was serving records is not something a
    caller expects from a `configure()` that failed.
    """
    _ = reset_backend, clean_queue
    stream = io.StringIO()
    original, sys.stdout = sys.stdout, stream
    try:
        configure(
            backend=LogBackendType.STDLIB,
            format=LogFormatType.JSON,
            queue_enabled=True,
            env_load=False,
        )
        working = get_writer()
        assert working is not None

        def refuse(_: LogConfig | None = None) -> None:
            raise DependencyNotFoundError(module="orjson")

        monkeypatch.setattr(_stdlib, "configure", refuse)
        with pytest.raises(DependencyNotFoundError):
            configure(
                backend=LogBackendType.STDLIB,
                format=LogFormatType.JSON,
                queue_enabled=True,
                env_load=False,
            )
        monkeypatch.undo()

        assert get_writer() is working
        assert working._thread is not None

        logging.getLogger("app").info("still queued")
        uninstall()
    finally:
        sys.stdout = original

    messages = [record["msg"] for record in parse_json_logs(stream.getvalue())]
    assert "still queued" in messages
    assert (
        sum(thread.name == "grelmicro-log" for thread in threading.enumerate())
        == 0
    )


def test_a_write_through_waits_for_the_drain() -> None:
    """The write-through path must not overtake what the worker is flushing.

    The worker clears the flag and writes its backlog in one critical
    section. A caller that finds the flag cleared has to wait for that,
    or its record reaches the stream ahead of older ones.
    """
    stream = _Stream()
    writer = QueueWriter(stream, size=10)
    writer.shutdown()
    assert writer._thread is None

    started = threading.Event()
    finished = threading.Event()

    def late_writer() -> None:
        started.set()
        writer.write("late\n")
        finished.set()

    with writer._drain_lock:
        threading.Thread(target=late_writer, daemon=True).start()
        assert started.wait(_TIMEOUT)
        time.sleep(0.05)
        assert stream.chunks == []

    assert finished.wait(_TIMEOUT)
    assert stream.text == "late\n"


async def test_the_component_tears_down_under_cancellation(
    reset_backend: None, clean_queue: None
) -> None:
    """A cancelled scope is the ordinary way an app stops.

    `to_thread.run_sync` checks for cancellation before running anything,
    so an unshielded await here would raise on the way in and leave the
    writer installed and the root handlers never restored.
    """
    _ = reset_backend, clean_queue
    root = logging.getLogger()
    before = list(root.handlers)
    stream = io.StringIO()
    original, sys.stdout = sys.stdout, stream
    try:
        with contextlib.suppress(BaseException):
            async with anyio.create_task_group() as task_group:

                async def fail_soon() -> None:
                    await anyio.sleep(0.01)
                    msg = "a sibling task failed"
                    raise RuntimeError(msg)

                task_group.start_soon(fail_soon)
                async with Log(
                    backend=LogBackendType.STDLIB,
                    format=LogFormatType.JSON,
                    queue_enabled=True,
                    env_load=False,
                ):
                    await anyio.sleep(1)
    finally:
        sys.stdout = original

    assert get_writer() is None
    assert root.handlers == before


def test_shutdown_stops_a_worker_the_sentinel_never_reaches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queue full for the whole deadline takes no sentinel.

    Without a flag the worker checks itself, it drains and then blocks on
    a queue nothing will ever feed, leaking a thread per `configure()`.
    """
    monkeypatch.setattr(_queue, "_JOIN_TIMEOUT", 0.2)
    stream = _BlockedStream()
    writer = QueueWriter(stream, size=2)
    writer.write("held\n")
    stream.entered.wait(_TIMEOUT)
    for index in range(20):
        writer.write(f"line{index}\n")
    assert writer._queue.full()

    writer.shutdown()
    stream.release.set()

    _wait_for(lambda: writer._thread is None)


def test_a_failed_restart_still_releases_the_install_lock(
    monkeypatch: pytest.MonkeyPatch, clean_queue: None
) -> None:
    """`Thread.start` raises at the process thread limit.

    Holding the install lock through that would deadlock every later
    swap in the process.
    """
    _ = clean_queue
    swap(size=10)
    writer = get_writer()
    assert writer is not None
    writer.shutdown()

    def refuse_to_start(_: threading.Thread) -> None:
        msg = "can't start new thread"
        raise RuntimeError(msg)

    monkeypatch.setattr(threading.Thread, "start", refuse_to_start)
    _before_fork()
    with pytest.raises(RuntimeError, match="can't start new thread"):
        _after_fork_in_parent()
    monkeypatch.undo()

    # Asserted rather than exercised: a held lock would deadlock the whole
    # suite here instead of failing this one test.
    assert not _queue._install_lock.locked()
    swap(size=None)


def test_dropped_counts_the_whole_life_of_the_writer() -> None:
    """Reporting must not zero the counter a metric reads.

    The worker reports after every batch. Resetting there left `dropped`
    holding only the residue since the last one, so a reader saw zero
    while thousands of records had gone.
    """
    stream = _BlockedStream()
    writer = QueueWriter(stream, size=2)
    writer.write("held\n")
    stream.entered.wait(_TIMEOUT)
    for index in range(_BURST - 1):
        writer.write(f"line{index}\n")

    stream.release.set()
    _wait_for(lambda: writer.dropped > 0)
    writer.shutdown()

    written = stream.text.count("\n")
    reports = stream.text.count("Log queue full")
    assert writer.dropped + written - reports == _BURST


def test_a_worker_lost_after_a_fork_writes_inline(
    monkeypatch: pytest.MonkeyPatch, clean_queue: None
) -> None:
    """A sink that outran the drain leaves the parent writing inline.

    Nothing brings a worker back from `write`. Doing so would let a log
    call raise at the process thread limit, and would let a second worker
    take items from a queue an exiting one is still draining. The records
    still reach the stream, on the calling thread.
    """
    _ = clean_queue
    monkeypatch.setattr(_queue, "_JOIN_TIMEOUT", 0.2)
    stream = _BlockedStream()
    swap(size=10)
    writer = get_writer()
    assert writer is not None
    writer._stream = stream
    writer.write("held\n")
    stream.entered.wait(_TIMEOUT)

    _before_fork()
    try:
        assert writer._thread is not None
    finally:
        _after_fork_in_parent()

    stream.release.set()
    _wait_for(lambda: writer._thread is None)

    writer.write("after the fork\n")

    assert writer._thread is None
    assert "after the fork\n" in stream.text


def test_a_write_never_raises_when_the_worker_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A log call must not raise into application code.

    `structlog.PrintLogger.msg` does not catch, so anything raised from
    `write` reaches the caller and the record is lost outright.
    """
    stream = _Stream()
    writer = QueueWriter(stream, size=10)
    writer.shutdown()
    # The state a worker leaves behind when it goes while the writer was
    # still meant to be queueing, which is what tempts `write` to start
    # one. Set directly, because nothing reaches it any more.
    writer._stopping = False

    def refuse_to_start(_: threading.Thread) -> None:
        msg = "can't start new thread"
        raise RuntimeError(msg)

    monkeypatch.setattr(threading.Thread, "start", refuse_to_start)
    try:
        assert writer.write("still written\n") == len("still written\n")
    finally:
        monkeypatch.undo()

    assert stream.text == "still written\n"


def test_a_stopped_writer_never_spawns_another_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`atexit` drains, then handlers flush. That must start no thread."""
    monkeypatch.setattr(_queue, "_JOIN_TIMEOUT", 0.2)
    stream = _Stream()
    writer = QueueWriter(stream, size=10)
    writer.shutdown()
    writer._stopping = False

    writer.shutdown()  # the early-return path, on a writer already gone

    assert writer._stopping is True
    before = threading.active_count()

    writer.write("after everything\n")

    assert threading.active_count() == before
    assert stream.text == "after everything\n"


def test_write_reports_the_length_it_was_handed() -> None:
    """A merged partial line must not report more than the caller wrote."""
    stream = _Stream()
    writer = QueueWriter(stream, size=10)

    assert writer.write("abc") == len("abc")
    assert writer.write("\n") == len("\n")

    writer.shutdown()
    assert stream.text == "abc\n"


def test_a_stale_stop_does_not_kill_the_next_worker() -> None:
    """A sentinel can outlive the worker it was meant for.

    The stop flag can end the worker before the sentinel finds room, and
    the retry then lands on a queue nobody reads. The next worker would
    read it as its first item and exit before doing any work, leaving the
    process writing inline with a writer that looks installed.
    """
    stream = _Stream()
    writer = QueueWriter(stream, size=10)
    writer.shutdown()
    stale = writer._stop
    writer._queue.put_nowait(stale)

    writer._start()
    assert writer._stop is not stale
    writer.write("after the restart\n")
    _wait_for(lambda: stream.text == "after the restart\n")

    # Written by the worker, not inline: a worker killed by the stale
    # sentinel would have cleared this on its way out.
    assert writer._thread is not None
    writer.shutdown()


def test_a_worker_that_finishes_at_once_leaves_no_stale_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recording the thread after starting it loses a race.

    A worker that runs to completion first clears the field in its own
    `finally`, and the assignment then puts the dead thread back. That
    reads as a healthy worker, so every later record is queued behind it
    and never written, and never counted as dropped either.
    """
    stream = _Stream()
    writer = QueueWriter(stream, size=10)
    writer.shutdown()

    def run_inline(self: threading.Thread) -> None:
        self._target()  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]

    # The worker runs to completion inside `Thread.start`, which makes
    # the race deterministic: with the old ordering the assignment lands
    # after the worker has already cleared the field.
    monkeypatch.setattr(QueueWriter, "_drain_until_stopped", lambda _: None)
    monkeypatch.setattr(threading.Thread, "start", run_inline)
    try:
        writer._start()
    finally:
        monkeypatch.undo()

    assert writer._thread is None
    writer.write("written inline\n")
    assert stream.text == "written inline\n"


def test_a_thread_that_will_not_start_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed start must leave no reference reading as a healthy worker."""
    stream = _Stream()
    writer = QueueWriter(stream, size=10)
    writer.shutdown()

    def refuse_to_start(_: threading.Thread) -> None:
        msg = "can't start new thread"
        raise RuntimeError(msg)

    monkeypatch.setattr(threading.Thread, "start", refuse_to_start)
    try:
        with pytest.raises(RuntimeError, match="can't start new thread"):
            writer._start()
    finally:
        monkeypatch.undo()

    assert writer._thread is None
    writer.write("written inline\n")
    assert stream.text == "written inline\n"


def test_a_broken_stream_reports_what_went_wrong(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `StreamHandler` prints the traceback, so this must too."""
    errors = io.StringIO()
    monkeypatch.setattr(sys, "stderr", errors)
    stream = _FailingStream(failures=1)
    writer = QueueWriter(stream, size=10)

    writer.write("lost\n")
    _wait_for(lambda: stream.failures == 0)
    writer.shutdown()

    assert "grelmicro log queue write failed" in errors.getvalue()
    assert "OSError" in errors.getvalue()
    assert "stream is gone" in errors.getvalue()


def test_a_record_queued_as_shutdown_completes_is_not_stranded() -> None:
    """`apply` shuts the old writer down after the handlers move.

    A thread already inside `write` at that moment would otherwise leave
    its record on a queue no worker will ever read.
    """
    stream = _Stream()
    writer = QueueWriter(stream, size=10)
    real_put = writer._queue.put_nowait

    def racing_put(item: str) -> None:
        # Land the shutdown between `write`'s worker check and its put.
        writer.shutdown()
        real_put(item)

    writer._queue.put_nowait = racing_put  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]

    writer.write("raced\n")

    assert stream.text == "raced\n"


def test_discarding_a_stale_stop_keeps_the_records_behind_it() -> None:
    """Only the sentinel is taken, and the lines keep their order."""
    stream = _Stream()
    writer = QueueWriter(stream, size=10)
    writer.shutdown()
    writer._queue.put_nowait("first\n")
    writer._queue.put_nowait(writer._stop)
    writer._queue.put_nowait("second\n")

    writer._start()
    _wait_for(lambda: stream.text == "first\nsecond\n")

    assert writer._thread is not None
    writer.shutdown()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_a_multiprocessing_child_writes_its_records(
    reset_backend: None, clean_queue: None
) -> None:
    """A `multiprocessing` child ends in `os._exit`, which skips `atexit`.

    Without an exit hook the child writes its whole run into a queue that
    is then discarded, and the records are not counted as dropped either.
    """
    _ = reset_backend, clean_queue
    read_fd, write_fd = os.pipe()

    def child() -> None:  # pragma: no cover
        os.close(read_fd)
        out = io.StringIO()
        writer = get_writer()
        assert writer is not None
        writer._stream = out
        logging.getLogger("child").warning("from the child")
        multiprocessing.util._exit_function()  # ty: ignore[unresolved-attribute]
        os.write(write_fd, out.getvalue().encode())

    configure(
        backend=LogBackendType.STDLIB,
        format=LogFormatType.JSON,
        queue_enabled=True,
        env_load=False,
    )
    process = multiprocessing.get_context("fork").Process(target=child)
    process.start()
    os.close(write_fd)
    payload = os.read(read_fd, 65536).decode()
    os.close(read_fd)
    process.join()

    records = parse_json_logs(payload)
    assert [record["msg"] for record in records] == ["from the child"]


def test_each_worker_gets_its_own_stop_token() -> None:
    """A token from a previous shutdown must mean nothing to a new worker.

    Draining the queue and putting it back could not promise this: a
    concurrent write refills it in between, and the records put back land
    behind the ones that arrived while it was empty.
    """
    stream = _Stream()
    writer = QueueWriter(stream, size=10)
    first = writer._stop
    writer.shutdown()

    writer._start()

    assert writer._stop is not first
    writer._queue.put_nowait(first)
    writer.write("after the stale token\n")
    _wait_for(lambda: stream.text == "after the stale token\n")
    assert writer._thread is not None
    writer.shutdown()


def test_the_inline_path_writes_the_backlog_first() -> None:
    """A record queued as the worker went must not be left behind."""
    stream = _Stream()
    writer = QueueWriter(stream, size=10)
    writer.shutdown()
    writer._queue.put_nowait("queued before\n")

    writer.write("written inline\n")

    assert stream.text == "queued before\nwritten inline\n"
