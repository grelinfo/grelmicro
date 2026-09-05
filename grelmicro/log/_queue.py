"""Queued writer that keeps log writes off the calling thread."""

from __future__ import annotations

import atexit
import logging
import os
import queue
import sys
import threading
import time
import traceback
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

if TYPE_CHECKING:
    from typing import TextIO


class TextStream(Protocol):
    """The part of a text stream a log backend writes through."""

    def write(self, text: str, /) -> int:
        """Write `text` and return how much of it was taken."""

    def flush(self) -> None:
        """Push what was written to the underlying sink."""


_MAX_BATCH: Final = 512
_JOIN_TIMEOUT: Final = 5.0
_STOP_POLL: Final = 0.05
_THREAD_NAME: Final = "grelmicro-log"
# Above the pool and queue finalizers, so log lines are written before
# multiprocessing tears down what the child was working with.
_EXIT_PRIORITY: Final = 100

_logger = logging.getLogger("grelmicro.log")

_install_lock = threading.Lock()
_current: QueueWriter | None = None


class QueueWriter:
    """Text stream that hands every write to a background thread.

    Wraps the stream a backend would have written to. `write` appends the
    rendered line to a bounded queue and returns, and a worker thread
    writes it out. Formatting stays on the calling thread, which is what
    holds the context variables a record carries, so a queued line keeps
    its `trace_id` and every bound field.

    A full queue drops the arriving line rather than blocking the caller,
    because blocking is what this class exists to remove. The count is
    available on `dropped` and is reported once the queue has room again.
    """

    def __init__(self, stream: TextStream, *, size: int) -> None:
        """Wrap `stream` behind a queue of at most `size` lines."""
        self._stream = stream
        self._size = size
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=size)
        self._dropped = 0
        self._reported = 0
        self._dropped_lock = threading.Lock()
        self._stopping = False
        self._stop: object = object()
        self._partial = threading.local()
        self._shutdown_lock = threading.Lock()
        self._drain_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._start()

    @property
    def stream(self) -> TextStream:
        """Return the wrapped stream."""
        return self._stream

    @property
    def dropped(self) -> int:
        """Return how many lines a full queue has dropped, for this writer's life.

        Monotonic. Reporting works on the difference since the last
        report, so that a reader of this counter, a metric or a
        benchmark, is not handed a residue that the worker has already
        zeroed dozens of times during a run.
        """
        with self._dropped_lock:
            return self._dropped

    def write(self, text: str) -> int:
        """Queue one whole line for the worker and return immediately.

        A record does not always arrive in one call. `structlog` prints
        through `print(message, file=...)`, which writes the message and
        its newline separately, so queueing each call on its own would let
        a full queue drop the newline and glue two records into one line
        no log reader can parse. Text without a trailing newline is held
        until the newline arrives, and the whole line is queued at once.
        A drop is then always a whole record.

        The buffer is per thread, so two threads writing at once cannot
        splice their partial lines together.
        """
        if not text.endswith("\n"):
            self._partial.text = getattr(self._partial, "text", "") + text
            return len(text)

        written = len(text)
        pending = getattr(self._partial, "text", "")
        if pending:
            self._partial.text = ""
            text = pending + text

        if self._thread is None:
            # No worker to hand it to, so write it here rather than into a
            # queue nothing reads. Losing the record would be worse than
            # the blocking write this class normally avoids. Under the
            # drain lock, so a line written while the worker is on its way
            # out lands after the backlog rather than ahead of it.
            with self._drain_lock:
                # Anything a racing `put_nowait` left behind goes first,
                # so this line cannot overtake a record queued before it.
                self._emit([*self._collect_locked(), text])
            return written

        try:
            self._queue.put_nowait(text)
        except queue.Full:
            with self._dropped_lock:
                self._dropped += 1
            return 0

        if self._thread is None:
            # `shutdown` finished between the check above and this put, so
            # the record is sitting on a queue no worker will read. Write
            # it here instead of stranding it.
            self._drain_remaining()
        return written

    def flush(self) -> None:
        """Do nothing.

        Every handler flushes after each record. Waiting for the queue
        here would hand the caller back the blocking write it just
        avoided, so the worker owns flushing the wrapped stream.
        """

    def isatty(self) -> bool:
        """Return whether the wrapped stream is a terminal."""
        isatty = getattr(self._stream, "isatty", None)
        return bool(isatty()) if isatty is not None else False

    def shutdown(self) -> None:
        """Write what is queued, then stop the worker.

        Deliberately not named `stop`. Loguru's `StreamSink` treats any
        sink with a callable `stop` as its own to stop, so `logger.remove()`
        would have killed the worker every backend shares.

        Bounded by `_JOIN_TIMEOUT`, including the wait for room to put the
        sentinel. A blocking put would hang here whenever the queue is full
        and the sink has stalled, which is the one case this runs in, and
        it would hang at interpreter exit where nothing can interrupt it.

        Returning does not mean the worker has gone. When the deadline
        passes first the sentinel is already queued, so the worker stops
        as soon as the sink clears, and it is the worker that marks itself
        gone. Nothing here may do that on its behalf: writes would go to a
        queue the worker was still about to read, and would be written
        twice or not at all.
        """
        with self._shutdown_lock:
            # Checked by the worker after every batch, so it stops even
            # when the queue stays full for the whole deadline and the
            # sentinel never fits. Without it that worker drains, blocks
            # on an empty queue nothing will ever feed, and leaks.
            self._stopping = True
            thread = self._thread
            if thread is None:
                return
            deadline = time.monotonic() + _JOIN_TIMEOUT
            while time.monotonic() < deadline:
                try:
                    self._queue.put(self._stop, timeout=_STOP_POLL)
                except queue.Full:
                    continue
                thread.join(max(0.0, deadline - time.monotonic()))
                break

    def _restart_if_stopped(self) -> None:
        """Start a worker again unless one is still running.

        Only the fork handler calls this. A worker is never started from
        `write`: that would let a record's own log call raise when the
        process is at its thread limit, and would let a second worker
        take items from a queue an exiting one is still draining.

        A worker still draining a stalled sink is therefore left alone,
        and the writer goes on writing inline once it exits. That is a
        slower process, not a wrong one.
        """
        with self._shutdown_lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._start()

    def _drain_remaining(self) -> None:
        """Write whatever is left on the queue, on the calling thread."""
        with self._drain_lock:
            self._drain_locked()

    def _drain_locked(self) -> None:
        """Write what is left, with `_drain_lock` already held."""
        lines = self._collect_locked()
        if lines:
            self._emit(lines)

    def _collect_locked(self) -> list[str]:
        """Take every queued line, leaving any stop token behind."""
        lines: list[str] = []
        try:
            while True:
                item = self._queue.get_nowait()
                if isinstance(item, str):
                    lines.append(item)
        except queue.Empty:
            pass
        return lines

    def _start(self) -> None:
        """Start the worker thread.

        Each worker gets a stop token of its own. A sentinel a previous
        shutdown left on the queue therefore means nothing to this one,
        which is not something draining the queue and putting it back
        could promise: a concurrent write refills it in between, and the
        records put back land behind it.

        The thread is recorded before it starts and taken back if it will
        not start. Recording it afterwards loses the race the other way:
        a worker that runs to completion first clears the field in its own
        `finally`, and the assignment then puts a dead thread back, which
        reads as healthy and strands every later record.
        """
        self._stopping = False
        self._stop = object()
        thread = threading.Thread(
            target=self._run, name=_THREAD_NAME, daemon=True
        )
        self._thread = thread
        try:
            thread.start()
        except BaseException:
            self._thread = None
            raise

    def _reopen(self) -> None:
        """Start a fresh worker after a fork.

        A thread does not survive `fork`, so the child needs one of its
        own. The queue is replaced rather than drained: the parent wrote
        what it held before forking, and keeping a copy would print every
        one of those lines twice. Every lock is replaced rather than
        taken, since no thread is left in the child to release one the
        parent held.
        """
        self._queue = queue.Queue(maxsize=self._size)
        self._dropped_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._drain_lock = threading.Lock()
        self._partial = threading.local()
        self._dropped = 0
        self._reported = 0
        # Cleared first: the inherited object is a thread of the parent's
        # that does not exist here, so a `_start` that raises must not
        # leave it behind reading as a healthy worker.
        self._thread = None
        self._start()

    def _run(self) -> None:
        """Write queued lines until stopped.

        On the way out it marks itself gone and writes what is left. The
        flag is cleared under `_drain_lock` and the backlog is written in
        the same critical section, so a record handed to `write` while
        this is happening cannot reach the stream ahead of one queued
        before it.
        """
        try:
            self._drain_until_stopped()
        finally:
            with self._drain_lock:
                self._thread = None
                self._drain_locked()
            self._report_dropped()

    def _drain_until_stopped(self) -> None:
        """Write queued lines until this worker's own stop arrives."""
        stop = self._stop
        while True:
            batch = [self._queue.get()]
            try:
                while len(batch) < _MAX_BATCH:
                    batch.append(self._queue.get_nowait())
            except queue.Empty:
                pass

            stopping = stop in batch or self._stopping
            lines = [item for item in batch if isinstance(item, str)]
            if lines:
                self._emit(lines)
            if stopping:
                return
            self._report_dropped()

    def _emit(self, lines: list[str]) -> None:
        """Write `lines` to the wrapped stream in one call."""
        try:
            self._stream.write("".join(lines))
            self._stream.flush()
        except Exception:  # noqa: BLE001
            # Mirrors `logging.Handler.handleError`: a broken stream must
            # not take the worker thread down with it, and reporting it
            # through the logger would come straight back to this queue.
            if logging.raiseExceptions and sys.stderr is not None:
                sys.stderr.write("--- grelmicro log queue write failed ---\n")
                traceback.print_exc(file=sys.stderr)

    def _report_dropped(self) -> None:
        """Report what a full queue dropped, once it has room again.

        Reported at `ERROR`, so that a service running at `WARNING` or
        below still hears about it. A service configured above `ERROR`
        filters the report out along with everything else.
        """
        with self._dropped_lock:
            dropped = self._dropped - self._reported
            self._reported = self._dropped
        if dropped:
            _logger.error(
                "Log queue full, dropped %d records",
                dropped,
                extra={"dropped": dropped},
            )


class _ForkAnchor:
    """Anchor the multiprocessing after-fork registration.

    `register_after_fork` keys a weak dictionary on the object it is
    given, so a bare `object()` cannot be used.
    """

    __slots__ = ("__weakref__",)


_MULTIPROCESSING_KEY: Final = _ForkAnchor()
_multiprocessing_ready = False


def _arm_multiprocessing_drain() -> None:
    """Ask multiprocessing to drain the queue before a child exits.

    A `multiprocessing` child ends in `os._exit`, which runs none of the
    `atexit` handlers. Without this the child writes its whole run into a
    queue that is then discarded, not even counted as dropped.

    Registered as an after-forker rather than straight into the finalizer
    registry, because the child clears that registry on the way up and
    only then runs the after-forkers.
    """
    global _multiprocessing_ready  # noqa: PLW0603
    if _multiprocessing_ready:
        return
    from multiprocessing.util import (  # noqa: PLC0415
        Finalize,
        register_after_fork,
    )

    register_after_fork(
        _MULTIPROCESSING_KEY,
        lambda _: Finalize(None, uninstall, exitpriority=_EXIT_PRIORITY),
    )
    _multiprocessing_ready = True


def swap(*, size: int | None) -> QueueWriter | None:
    """Install a writer of `size`, or none at all when `size` is `None`.

    Returns the writer this replaced, still running. The caller stops it
    once the handlers point at the new one, so the records it is holding
    are written instead of stranded on a queue nothing reads.
    """
    global _current  # noqa: PLW0603
    with _install_lock:
        previous = _current
        if size:
            _arm_multiprocessing_drain()
            _current = QueueWriter(sys.stdout, size=size)
        else:
            _current = None
        return previous


def install_if_absent(*, size: int) -> bool:
    """Install a writer of `size` when none is running, and say whether it did.

    The test and the install are one step, so two callers racing to put a
    queue behind a process end up with one writer rather than one of them
    replacing, and having to stop, the other's.
    """
    global _current  # noqa: PLW0603
    with _install_lock:
        if _current is not None:
            return False
        _arm_multiprocessing_drain()
        _current = QueueWriter(sys.stdout, size=size)
        return True


def restore(writer: QueueWriter | None) -> QueueWriter | None:
    """Put `writer` back as the installed one, returning what it replaced."""
    global _current  # noqa: PLW0603
    with _install_lock:
        replaced = _current
        _current = writer
        return replaced


def uninstall() -> None:
    """Write out what the installed writer holds and remove it."""
    global _current  # noqa: PLW0603
    with _install_lock:
        if _current is not None:
            _current.shutdown()
            _current = None


def get_writer() -> QueueWriter | None:
    """Return the installed writer, or `None` when writes go direct."""
    return _current


def get_stream() -> TextIO:
    """Return the stream a backend writes to.

    The installed writer when logging is queued, `sys.stdout` otherwise.

    A `QueueWriter` is not a whole `TextIO`, it is the `write`, `flush`
    and `isatty` a log backend actually calls. It is cast to one here so
    the three backends keep the stream type their own libraries declare.
    """
    if _current is None:
        return sys.stdout
    return cast("TextIO", _current)


def _before_fork() -> None:
    """Stop the worker and hold the install lock across the fork.

    The worker is stopped rather than merely locked out, because it can
    be inside `stream.write` holding the stream's own buffer lock at the
    instant of the fork, and the child would inherit that lock held with
    no thread left to release it.

    Stopping is bounded by `_JOIN_TIMEOUT`, so a sink stalled for longer
    than that forks anyway and the child can still meet a held lock. That
    is the lesser of the two, since waiting without a bound would hang the
    fork itself.
    """
    _install_lock.acquire()
    if _current is not None:
        _current.shutdown()


def _after_fork_in_parent() -> None:
    """Start the worker again and release the install lock."""
    try:
        if _current is not None:
            _current._restart_if_stopped()  # noqa: SLF001
    finally:
        # `Thread.start` raises at the process thread limit. Leaving the
        # lock held would deadlock every later swap in this process.
        _install_lock.release()


def _after_fork_in_child() -> None:
    """Replace the inherited locks and give the child its own worker."""
    global _install_lock  # noqa: PLW0603
    _install_lock = threading.Lock()
    if _current is not None:
        _current._reopen()  # noqa: SLF001


if hasattr(os, "register_at_fork"):  # pragma: no branch
    os.register_at_fork(
        before=_before_fork,
        after_in_parent=_after_fork_in_parent,
        after_in_child=_after_fork_in_child,
    )

atexit.register(uninstall)
