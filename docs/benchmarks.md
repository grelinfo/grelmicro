# Benchmarks

Grelmicro ships runnable benchmark scripts for its request-path primitives. Use them to verify overhead claims on your own hardware. The scripts live in the [`benchmarks/`](https://github.com/grelinfo/grelmicro/tree/main/benchmarks) directory and depend only on the standard library plus grelmicro.

## Running

Run any script with `uv`:

```bash
uv run python benchmarks/ratelimiter_benchmark.py
uv run python benchmarks/circuitbreaker_benchmark.py
uv run python benchmarks/cache_benchmark.py
uv run python benchmarks/lock_benchmark.py
```

Each script measures the in-memory backend so the numbers reflect grelmicro's own overhead, not a network round-trip. Distributed backends (Redis, Postgres, SQLite) add their transport and storage cost on top.

## Results

The numbers below come from one machine and are indicative only. Run the scripts yourself for figures that match your hardware and Python build.

| Primitive | Operation | Time per op |
|---|---|---|
| Rate limiter | `RateLimiter.token_bucket` acquire (allowed) | ~530 ns |
| Rate limiter | `RateLimiter.sliding_window` acquire (allowed) | ~485 ns |
| Rate limiter | `MemoryTokenBucket.try_acquire` (sync, hit) | ~280 ns |
| Circuit breaker | `try_acquire` (CLOSED) | ~105 ns |
| Circuit breaker | `record_outcome` (success) | ~505 ns |
| Cache | `get` (hit) | ~275 ns |
| Cache | `get` (miss) | ~190 ns |
| Cache | `set` | ~270 ns |
| Lock | `acquire` + `release` cycle | ~1150 ns |

## Reading the numbers

The in-memory primitives run in well under a microsecond per call, so on a distributed backend the algorithm itself is never the bottleneck. End-to-end latency is dominated by the backend round-trip. Choose a backend for its coordination and durability properties, not for the per-call compute cost.
