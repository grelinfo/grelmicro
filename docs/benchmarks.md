# Benchmarks

Grelmicro ships runnable benchmark scripts for its request-path primitives. Use them to verify overhead claims on your own hardware. The scripts live in the [`benchmarks/`](https://github.com/grelinfo/grelmicro/tree/main/benchmarks) directory and depend only on the standard library plus grelmicro.

## Running

Run any script with `uv`:

```bash
uv run python benchmarks/ratelimiter_benchmark.py
uv run python benchmarks/circuitbreaker_benchmark.py
uv run python benchmarks/cache_benchmark.py
uv run python benchmarks/lock_benchmark.py
uv run python benchmarks/logging_benchmark.py
uv run python benchmarks/access_log_benchmark.py
uv run python benchmarks/jwt_benchmark.py
uv run python benchmarks/memory_benchmark.py
```

Each script measures the in-memory backend so the numbers reflect grelmicro's own overhead, not a network round-trip. Distributed backends (Redis, Postgres, SQLite) add their transport and storage cost on top.

## Results

The primitive rows are the best of three runs. The logging table is a single run. The conditions are stated so you can judge them.

| | |
|---|---|
| Hardware | Apple Silicon, macOS |
| Interpreter | CPython 3.14.7 |
| grelmicro | 0.42.4 |
| Measured | 2026-10-22 |
| Conditions | Developer machine, shared with other work |

The conditions line is the part most published benchmarks leave out. These were not measured on an idle machine, so treat them as the right order of magnitude rather than a precise figure. A quiet machine would move them somewhat, and not evenly across rows. A second run on another day, under a heavier load, agreed within 10% on every row.

That is also why the numbers below are not the point. The scripts are. Run them on the hardware you will deploy on, and compare against your own baseline rather than against this table.

| Primitive | Operation | Time per op |
|---|---|---|
| Rate limiter | `RateLimiter.token_bucket` acquire (allowed) | ~525 ns |
| Rate limiter | `RateLimiter.sliding_window` acquire (allowed) | ~695 ns |
| Rate limiter | `MemoryTokenBucket.try_acquire` (sync, hit) | ~270 ns |
| Circuit breaker | `try_acquire` (CLOSED) | ~90 ns |
| Circuit breaker | `record_outcome` (success) | ~500 ns |
| Cache | `get` (hit) | ~415 ns |
| Cache | `get` (miss) | ~290 ns |
| Cache | `set` | ~550 ns |

The lock has no row. A release runs to completion in a task of its own, so each `acquire` and `release` cycle costs one turn of the event loop. That turn depends on the machine more than on grelmicro. Run `benchmarks/lock_benchmark.py` to see it on yours.

## Logging

The logging backends are measured separately, over 50,000 iterations:

| Backend | Serializer | Ops/sec | vs Best |
|---------|------------|---------|---------|
| structlog | orjson | 279,428 | 100.0% |
| stdlib | orjson | 216,737 | 77.6% |
| structlog | stdlib | 193,333 | 69.2% |
| loguru | orjson | 164,178 | 58.8% |
| stdlib | stdlib | 159,015 | 56.9% |
| loguru | stdlib | 126,845 | 45.4% |

For a high-throughput service, run the `structlog` or `stdlib` backend with
orjson installed. The serializer defaults to `auto`, so
[`grelmicro[standard]`](installation.md) is all it takes.
[Logging](logging/index.md#which-serializer-you-get) covers the values the two
serializers write differently.

## JWT verification

A JWT signature check in the compiled core is about four times faster than in a pure-Python library. [JWT verification](architecture/jwt.md#why-a-compiled-core) lists the libraries measured and the conditions, on CPython 3.12. [`benchmarks/jwt_benchmark.py`](https://github.com/grelinfo/grelmicro/blob/main/benchmarks/jwt_benchmark.py) measures the verifier's own cost per algorithm and its cache hit.

## Memory

`benchmarks/memory_benchmark.py` runs three reference apps, each in a fresh interpreter:

- **tasks**: FastAPI with `Grelmicro(uses=[MemoryProvider(), Log(), HealthChecks(auto_health=True), tasks])`, one task on `@tasks.every(interval=60, gate="claim")` and `health_router()`. The app opens and closes.
- **redis**: FastAPI with `RedisProvider`, a `RateLimiter` and a `CircuitBreaker`.
- **postgres**: `PostgresProvider` and `Outbox`, without FastAPI.

The redis and postgres apps need no server. They stop after construction and `micro.install(app)`, before the first connection, so the numbers leave out the client a connection loads.

Each interpreter imports `fastapi` first, and the numbers are what the app adds on top. Max RSS is the median of five runs. Same machine and interpreter as above, on `main` after 0.42.4, measured on 2026-10-23.

| App | grelmicro modules | New modules | Max RSS |
|---|---|---|---|
| tasks | 167 | 258 | +16.1 MB |
| redis | 160 | 321 | +24.0 MB |
| postgres | 102 | 176 | +11.1 MB |

Most of this is the modules an app imports, not the data it holds. `tests/test_memory_budget.py` pins which grelmicro packages each app loads, so a change that loads more fails the test.

## Reading the numbers

The in-memory primitives run in well under a microsecond per call, so on a distributed backend the algorithm itself is never the bottleneck. End-to-end latency is dominated by the backend round-trip. Choose a backend for its coordination and durability properties, not for the per-call compute cost.
