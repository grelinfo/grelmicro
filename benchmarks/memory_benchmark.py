"""Benchmark the memory and modules three reference apps load.

Each app runs in a fresh interpreter. The interpreter imports `fastapi` first,
and that state is the baseline. The script reports the grelmicro modules the
app loads, every module it adds above the baseline, and the peak RSS it adds
above the baseline, as the median of five runs.

The Redis and Postgres apps need no server. They stop after construction and
`install`, before the first connection.

Run with: python benchmarks/memory_benchmark.py
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
from typing import TypedDict

RUNS = 5
"""The number of fresh interpreters per app."""

APPS = {
    "tasks": (
        "from fastapi import FastAPI\n"
        "from grelmicro import Grelmicro\n"
        "from grelmicro.health import HealthChecks\n"
        "from grelmicro.integrations.fastapi import health_router\n"
        "from grelmicro.log import Log\n"
        "from grelmicro.providers.memory import MemoryProvider\n"
        "from grelmicro.task import Tasks\n"
        "tasks = Tasks()\n"
        "micro = Grelmicro(\n"
        "    uses=[MemoryProvider(), Log(), HealthChecks(provider_checks='require'), tasks]\n"
        ")\n"
        "@tasks.every(interval=60, gate='claim')\n"
        "async def job() -> None: ...\n"
        "app = FastAPI()\n"
        "micro.install(app)\n"
        "app.include_router(health_router())\n"
        "async def _open() -> None:\n"
        "    async with micro:\n"
        "        await asyncio.sleep(0)\n"
        "asyncio.run(_open())\n"
    ),
    "redis": (
        "from fastapi import FastAPI\n"
        "from grelmicro import Grelmicro\n"
        "from grelmicro.providers.redis import RedisProvider\n"
        "from grelmicro.resilience import CircuitBreaker, RateLimiter\n"
        "micro = Grelmicro(uses=[RedisProvider('redis://localhost:6379')])\n"
        "limiter = RateLimiter.token_bucket('api', capacity=10, refill_rate=1)\n"
        "breaker = CircuitBreaker('dependency')\n"
        "app = FastAPI()\n"
        "micro.install(app)\n"
    ),
    "postgres": (
        "from grelmicro import Grelmicro\n"
        "from grelmicro.outbox import Outbox\n"
        "from grelmicro.providers.postgres import PostgresProvider\n"
        "postgres = PostgresProvider('postgresql://user:pass@localhost:5432/db')\n"
        "micro = Grelmicro(uses=[postgres, Outbox(postgres)])\n"
    ),
}
"""The reference apps, by name.

`tasks` opens and closes the app. `redis` and `postgres` stop before the
first connection, so they need no server.
"""

BASELINE = "import fastapi\n"
"""The code that sets the baseline: a process that imported FastAPI."""

_PROBE = """\
import asyncio, json, resource, sys
{baseline}
before = set(sys.modules)
base_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
{app}
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
scale = 1 if sys.platform == "darwin" else 1024
print(json.dumps({{
    "grelmicro": sorted(m for m in sys.modules if m.split(".")[0] == "grelmicro"),
    "new": len(set(sys.modules) - before),
    "rss": (rss - base_rss) * scale,
}}))
"""
"""Run an app above the baseline and print what it loaded as JSON.

The app may log to stdout, so the JSON is the last line. `ru_maxrss` is in
bytes on macOS and in kilobytes on Linux, so the probe converts it to bytes.
"""


class Probe(TypedDict):
    """What one app loaded above the baseline."""

    grelmicro: list[str]
    """The grelmicro modules loaded, sorted."""
    new: int
    """The number of modules loaded above the baseline."""
    rss: int
    """The peak RSS added above the baseline, in bytes."""


def probe(app: str) -> Probe:
    """Run one app in a fresh interpreter and return what it loaded."""
    code = _PROBE.format(baseline=BASELINE, app=APPS[app])
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        check=True,
        text=True,
    )
    return json.loads(result.stdout.splitlines()[-1])


def main() -> None:
    """Run all benchmarks."""
    print("=" * 60)  # noqa: T201
    print("Reference app memory benchmark")  # noqa: T201
    print("=" * 60)  # noqa: T201
    print(f"\nAbove an `import fastapi` baseline, median of {RUNS} runs.")  # noqa: T201
    print("redis and postgres: construction and install only, no server.\n")  # noqa: T201
    header = (
        f"  {'App':<10} {'grelmicro':>10} {'New modules':>12} {'Max RSS':>10}"
    )
    print(header)  # noqa: T201
    for app in APPS:
        runs = [probe(app) for _ in range(RUNS)]
        grelmicro = len(runs[0]["grelmicro"])
        new = runs[0]["new"]
        rss = statistics.median(run["rss"] for run in runs) / 1e6
        print(f"  {app:<10} {grelmicro:>10} {new:>12} {rss:>8.1f} MB")  # noqa: T201


if __name__ == "__main__":
    main()
