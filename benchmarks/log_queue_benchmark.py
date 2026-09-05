"""Measure what the log queue costs and what it saves.

The queue exists to keep a slow sink from blocking the event loop, so the
number that matters is the time the calling thread spends inside the log
call, not the time until the line reaches the sink.

Run with: python benchmarks/log_queue_benchmark.py
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from grelmicro.log import configure
from grelmicro.log._queue import get_writer, uninstall
from grelmicro.log.config import (
    LogBackendType,
    LogFormatType,
)

ITERATIONS = 20_000
BURST = 5_000
WARMUP = 200


class _NullSink:
    """Sink that takes a line and does nothing, as a fast stdout does."""

    def write(self, text: str) -> int:
        """Discard the line."""
        return len(text)

    def flush(self) -> None:
        """Do nothing."""

    def isatty(self) -> bool:
        """Report a pipe rather than a terminal."""
        return False


class _SlowSink(_NullSink):
    """Sink that costs `delay` per line, as a throttled stdout does.

    Charged per line rather than per call. The worker joins a batch into
    one `write`, so a per-call cost would have the queued run pay the
    stall a few dozen times where the direct run pays it once per record,
    and the comparison would measure batching instead of queueing.
    """

    def __init__(self, delay: float) -> None:
        self.delay = delay

    def write(self, text: str) -> int:
        """Sleep once per line, then discard them."""
        time.sleep(self.delay * text.count("\n"))
        return len(text)


def _measure(
    sink: _NullSink,
    backend: LogBackendType,
    records: int,
    *,
    queued: bool,
) -> tuple[float, int]:
    """Return the microseconds a log call costs its caller, and the drops."""
    original = sys.stdout
    sys.stdout = sink  # type: ignore[assignment]
    try:
        configure(
            backend=backend,
            format=LogFormatType.JSON,
            queue_enabled=queued,
            otel_enabled=False,
            env_load=False,
        )
        log = logging.getLogger("bench")
        for _ in range(WARMUP):
            log.info("warmup %d", 1, extra={"user_id": 7})

        start = time.perf_counter()
        for index in range(records):
            log.info("record %d", index, extra={"user_id": 7})
        elapsed = time.perf_counter() - start
        writer = get_writer()
        dropped = writer.dropped if writer is not None else 0
    finally:
        uninstall()
        sys.stdout = original
    return elapsed / records * 1e6, dropped


def main() -> None:
    """Run the comparison and print the table."""
    rows = [
        ("fast, a no-op write", 0.0, ITERATIONS),
        ("slow, 10us per line", 0.000_010, ITERATIONS),
        ("stalled, 200us per line", 0.000_200, BURST),
    ]

    print("=" * 76)  # noqa: T201
    print("Log queue: caller-side cost per record")  # noqa: T201
    print("=" * 76)  # noqa: T201
    print("\nstdlib backend, JSON format\n")  # noqa: T201
    header = (
        f"{'Sink':<26}{'records':>9}{'direct':>11}{'queued':>11}{'dropped':>9}"
    )
    print(header)  # noqa: T201
    print("-" * 76)  # noqa: T201

    for label, delay, records in rows:
        direct, _ = _measure(
            _SlowSink(delay) if delay else _NullSink(),
            LogBackendType.STDLIB,
            records,
            queued=False,
        )
        queued, dropped = _measure(
            _SlowSink(delay) if delay else _NullSink(),
            LogBackendType.STDLIB,
            records,
            queued=True,
        )
        print(  # noqa: T201
            f"{label:<26}{records:>9,}{direct:>9.2f}us"
            f"{queued:>9.2f}us{dropped:>9,}"
        )

    print(  # noqa: T201
        "\nA burst that fits the queue is absorbed whole. Sustained load "
        "\nbeyond what the sink can drain drops records, which is the "
        "\ntrade the bound buys."
    )


if __name__ == "__main__":
    main()
