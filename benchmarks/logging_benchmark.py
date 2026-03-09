"""Benchmark logging backends and JSON serializers.

Run with: python benchmarks/logging_benchmark.py
"""

from __future__ import annotations

import importlib
import io
import logging
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from loguru import logger as loguru_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    import grelmicro.logging

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _NullWriter(io.StringIO):
    """Null writer that discards all output."""

    def __init__(self) -> None:
        super().__init__()
        self._buffer = io.BytesIO()

    @property
    def buffer(self) -> io.BytesIO:
        """Return bytes buffer for structlog BytesLoggerFactory."""
        return self._buffer

    def write(self, _: str) -> int:
        """Discard all writes."""
        return 0


def _reset_logging() -> None:
    """Reset all logging configurations."""
    logging.getLogger().handlers.clear()
    logging.getLogger().setLevel(logging.WARNING)
    loguru_logger.remove()
    loguru_logger.configure(handlers=[])
    structlog.reset_defaults()


def _reload_modules() -> grelmicro.logging:  # type: ignore[name-defined]
    """Reload grelmicro.logging modules to pick up env changes."""
    import grelmicro.logging as logging_module  # noqa: PLC0415
    import grelmicro.logging._shared as shared_module  # noqa: PLC0415
    import grelmicro.logging.config as config_module  # noqa: PLC0415

    importlib.reload(config_module)
    importlib.reload(shared_module)
    importlib.reload(logging_module)
    return logging_module


def _get_logger(backend: str) -> Callable[[], None]:
    """Get the log function for the specified backend."""
    if backend == "loguru":
        return lambda: loguru_logger.info("Msg", user_id=123, action="test")
    if backend == "structlog":
        slog = structlog.get_logger()
        return lambda: slog.info("Msg", user_id=123, action="test")
    # stdlib
    stdlib_log = logging.getLogger("bench")
    return lambda: stdlib_log.info(
        "Msg", extra={"user_id": 123, "action": "test"}
    )


def _run_benchmark(backend: str, serializer: str, iterations: int) -> float:
    """Run benchmark and return ops/sec."""
    _reset_logging()

    os.environ["LOG_BACKEND"] = backend
    os.environ["LOG_FORMAT"] = "JSON"
    os.environ["LOG_JSON_SERIALIZER"] = serializer
    os.environ["LOG_OTEL_ENABLED"] = "false"

    grelmicro_logging = _reload_modules()

    null_sink = _NullWriter()
    old_stdout = sys.stdout
    sys.stdout = null_sink  # type: ignore[assignment,misc]

    try:
        grelmicro_logging.configure_logging()
        log = _get_logger(backend)

        # Warmup
        for _ in range(100):
            log()

        # Benchmark
        start = time.perf_counter()
        for _ in range(iterations):
            log()
        elapsed = time.perf_counter() - start

        return iterations / elapsed
    finally:
        sys.stdout = old_stdout


def _print_results(results: list[tuple[str, str, float]]) -> None:
    """Print sorted benchmark results."""
    results.sort(key=lambda x: x[2], reverse=True)
    fastest = results[0][2]

    print("\n" + "=" * 60)  # noqa: T201
    print("Results (sorted by speed)")  # noqa: T201
    print("=" * 60)  # noqa: T201
    print(
        f"\n{'Backend':<12} {'Serializer':<10} {'Ops/sec':>12} {'vs Best':>10}"
    )  # noqa: T201
    print("-" * 50)  # noqa: T201

    for backend, serializer, ops in results:
        pct = ops / fastest * 100
        print(f"{backend:<12} {serializer:<10} {ops:>12,.0f} {pct:>9.1f}%")  # noqa: T201

    print(
        f"\nFastest: {results[0][0]} + {results[0][1]} ({results[0][2]:,.0f} ops/sec)"
    )  # noqa: T201
    print(
        f"Slowest: {results[-1][0]} + {results[-1][1]} ({results[-1][2]:,.0f} ops/sec)"
    )  # noqa: T201
    print(f"Speedup: {results[0][2] / results[-1][2]:.2f}x")  # noqa: T201


def main() -> None:
    """Run all benchmarks."""
    print("=" * 60)  # noqa: T201
    print("Logging Backend Benchmark")  # noqa: T201
    print("=" * 60)  # noqa: T201

    iterations = 50000
    results: list[tuple[str, str, float]] = []

    configs = [
        ("stdlib", "stdlib"),
        ("stdlib", "orjson"),
        ("loguru", "stdlib"),
        ("loguru", "orjson"),
        ("structlog", "stdlib"),
        ("structlog", "orjson"),
    ]

    print(f"\nRunning {iterations:,} iterations per configuration...\n")  # noqa: T201

    for backend, serializer in configs:
        print(f"  {backend} + {serializer}...", end=" ", flush=True)  # noqa: T201
        ops = _run_benchmark(backend, serializer, iterations)
        results.append((backend, serializer, ops))
        print(f"{ops:,.0f} ops/sec")  # noqa: T201

    _print_results(results)


if __name__ == "__main__":
    main()
