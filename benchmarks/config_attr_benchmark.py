"""Benchmark Pydantic model attribute access vs plain instance attrs.

Issue #113: hot paths read `self._config.<field>` on a frozen
BaseModel. The question is whether copying fields to plain
instance attrs (`self._field`) at init time is worth it.

Run with: python benchmarks/config_attr_benchmark.py
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel

from grelmicro.logging import RateLimitFilter

if TYPE_CHECKING:
    from collections.abc import Callable


class PydanticConfig(BaseModel, frozen=True, extra="forbid"):
    """Pydantic config like the ones used across grelmicro."""

    name: str
    worker: str
    cost: float
    retry_interval: float


@dataclass(frozen=True, slots=True)
class DataclassConfig:
    """Plain dataclass equivalent."""

    name: str
    worker: str
    cost: float
    retry_interval: float


class PydanticHolder:
    """Holder that reads fields off a Pydantic model."""

    def __init__(self) -> None:
        """Build the holder with a validated Pydantic config."""
        self._config = PydanticConfig(
            name="lock-a",
            worker="w-1",
            cost=1.0,
            retry_interval=0.1,
        )

    def read_four_fields(self) -> tuple[str, str, float, float]:
        """Read four fields through `self._config.<x>`."""
        return (
            self._config.name,
            self._config.worker,
            self._config.cost,
            self._config.retry_interval,
        )


class PlainAttrHolder:
    """Holder that copies fields to plain attrs after validation."""

    def __init__(self) -> None:
        """Build the holder and cache each field as a plain attr."""
        self._config = PydanticConfig(
            name="lock-a",
            worker="w-1",
            cost=1.0,
            retry_interval=0.1,
        )
        self._name = self._config.name
        self._worker = self._config.worker
        self._cost = self._config.cost
        self._retry_interval = self._config.retry_interval

    def read_four_fields(self) -> tuple[str, str, float, float]:
        """Read four fields through plain `self._<x>` attrs."""
        return (self._name, self._worker, self._cost, self._retry_interval)


class DataclassHolder:
    """Holder that uses a frozen slotted dataclass directly."""

    def __init__(self) -> None:
        """Build the holder with a frozen slotted dataclass config."""
        self._config = DataclassConfig(
            name="lock-a",
            worker="w-1",
            cost=1.0,
            retry_interval=0.1,
        )

    def read_four_fields(self) -> tuple[str, str, float, float]:
        """Read four fields through `self._config.<x>`."""
        return (
            self._config.name,
            self._config.worker,
            self._config.cost,
            self._config.retry_interval,
        )


def _measure(label: str, fn: Callable[[], object], iterations: int) -> float:
    """Return ns/op for `fn` over `iterations`."""
    for _ in range(1000):
        fn()
    start = time.perf_counter()
    for _ in range(iterations):
        fn()
    elapsed = time.perf_counter() - start
    ns_per_op = elapsed / iterations * 1e9
    print(f"  {label:<30} {ns_per_op:>8.1f} ns/op")  # noqa: T201
    return ns_per_op


def _bench_realistic_hot_path(iterations: int) -> None:
    """Measure the tightest real hot path: RateLimitFilter.filter.

    It does one Pydantic attr read (`self._config.cost`) plus a
    token-bucket `try_acquire` with dict/lock/math work. This
    tells us what fraction of a real call is attr access.
    """
    print("\nRealistic hot path: RateLimitFilter.filter per record")  # noqa: T201
    print("-" * 60)  # noqa: T201

    filt = RateLimitFilter(capacity=1_000_000_000, refill_rate=1e9)
    record = logging.LogRecord(
        name="bench",
        level=logging.INFO,
        pathname=__file__,
        lineno=0,
        msg="hello",
        args=(),
        exc_info=None,
    )

    for _ in range(1000):
        filt.filter(record)

    start = time.perf_counter()
    for _ in range(iterations):
        filt.filter(record)
    elapsed = time.perf_counter() - start
    total_ns = elapsed / iterations * 1e9
    print(f"  Total call:       {total_ns:>8.1f} ns/op")  # noqa: T201

    bucket = filt._bucket  # noqa: SLF001
    key_fn = filt._key_fn  # noqa: SLF001

    def call_without_config_read() -> bool:
        return bucket.try_acquire(key_fn(record), cost=1.0)

    for _ in range(1000):
        call_without_config_read()

    start = time.perf_counter()
    for _ in range(iterations):
        call_without_config_read()
    elapsed = time.perf_counter() - start
    no_cfg_ns = elapsed / iterations * 1e9
    print(f"  Minus cfg read:   {no_cfg_ns:>8.1f} ns/op")  # noqa: T201
    print(f"  Delta (cfg cost): {total_ns - no_cfg_ns:>8.1f} ns")  # noqa: T201
    share = (total_ns - no_cfg_ns) / total_ns * 100
    print(f"  Share of call:    {share:>7.1f}%")  # noqa: T201


def main() -> None:
    """Run all benchmarks."""
    print("=" * 60)  # noqa: T201
    print("Config attr access benchmark (issue #113)")  # noqa: T201
    print("=" * 60)  # noqa: T201

    iterations = 2_000_000

    pyd = PydanticHolder()
    plain = PlainAttrHolder()
    dc = DataclassHolder()

    print(f"\nReading 4 fields, {iterations:,} iterations:\n")  # noqa: T201

    pyd_ns = _measure(
        "Pydantic attr (self._config.x)",
        pyd.read_four_fields,
        iterations,
    )
    plain_ns = _measure(
        "Plain attr (self._x)",
        plain.read_four_fields,
        iterations,
    )
    dc_ns = _measure(
        "Dataclass (frozen, slots)",
        dc.read_four_fields,
        iterations,
    )

    print("\nPer-field cost (ns):")  # noqa: T201
    print(f"  Pydantic:   {pyd_ns / 4:>6.1f}")  # noqa: T201
    print(f"  Plain attr: {plain_ns / 4:>6.1f}")  # noqa: T201
    print(f"  Dataclass:  {dc_ns / 4:>6.1f}")  # noqa: T201

    print("\nOverhead of Pydantic vs plain attr:")  # noqa: T201
    print(f"  Per 4-field read: {pyd_ns - plain_ns:>7.1f} ns")  # noqa: T201
    print(f"  Ratio:            {pyd_ns / plain_ns:>7.2f}x")  # noqa: T201

    _bench_realistic_hot_path(iterations)


if __name__ == "__main__":
    main()
