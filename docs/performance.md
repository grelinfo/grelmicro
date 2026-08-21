# Performance

Grelmicro's own overhead is under a microsecond per call. Whether that matters depends entirely on where your state lives.

On Redis or Postgres, one operation costs a network round trip of 100 microseconds or more. Everything on this page is rounding error against that. On the in-process backends, and inside a loop that runs millions of times, the same knobs are worth real throughput.

So work down this page in order. The tips at the top pay off on any deployment. The ones at the bottom pay off only when you have already removed the network from the path.

## Measure first

[Benchmarks](benchmarks.md) ships runnable scripts for every request-path primitive. Run them on the hardware you deploy on, before and after each change:

```bash
uv run python benchmarks/cache_benchmark.py
uv run python benchmarks/lock_benchmark.py
```

Every number on this page came from those conditions: CPython 3.12, Apple Silicon, a developer machine shared with other work. Treat them as an order of magnitude.

## Install the standard extra

```bash
pip install "grelmicro[standard]"
```

That brings two things:

- **orjson**, picked up automatically wherever grelmicro serializes JSON.
- **uvloop**, a faster event loop. Nothing activates it for you. Start your app with `uvloop.run(main())`.

[Installation](installation.md) lists the platforms uvloop covers.

## Choose the cache serializer

The serializer runs on every cache read and write, so it is the single biggest lever on a cache-heavy service.

| Serializer | Use it for | Relative speed |
|---|---|---|
| `PydanticSerializer` | `BaseModel`, dataclasses, `TypedDict` | Fastest, roughly 2x faster than pickle |
| `JsonSerializer` | dicts, lists, JSON-native types | Fast, roughly 7x faster than stdlib json when orjson is installed |
| `PickleSerializer` | arbitrary Python objects | Slowest, and the least portable |

Annotate the type and grelmicro picks the matching serializer for you:

```python
users = TTLCache[User]()  # PydanticSerializer
```

See [Cache](reference/cache.md) for the full list.

## Pick a fast logging backend

Logging runs on every request, and the serializer already follows the standard extra: `GREL_LOG_JSON_SERIALIZER` defaults to `auto`, which uses orjson when it is installed. So the lever here is the backend:

```bash
export GREL_LOG_BACKEND=structlog
```

structlog with orjson reaches roughly 283,000 records per second against 137,000 for loguru with stdlib json, a 2.1x spread. [Benchmarks](benchmarks.md#logging) has the full table, and [Logging](logging/index.md#which-serializer-you-get) covers the values the two serializers write differently.

## Fold cache misses at the right level

`@cached` defaults to `lock="local"`, which folds concurrent misses inside one worker and never touches a backend. That is free. Raise it to `lock=True` only when you need misses folded across replicas, because it costs one backend acquire per cold miss.

[Stampede protection](cache/cached.md#stampede-protection) compares every mode.

## Bind the backend on the hottest paths

A pattern written without `backend=` resolves the active app on every operation. A pattern given `backend=` binds once, at construction:

```python
--8<-- "performance/explicit_backend.py"
```

| Pattern | Ambient | With `backend=` |
|---|---|---|
| `Lock` | ~100 ns | ~30 ns |
| `TTLCache` | ~90 ns | ~30 ns |
| `RateLimiter` | ~100 ns | ~30 ns |
| `CircuitBreaker` | ~100 ns | ~30 ns |

Rounded to the nearest 10 nanoseconds, because run-to-run spread on a shared machine is wider than the digits underneath.

A lock acquire and release cycle on the in-memory backend costs about 1.9 microseconds ambient against 1.7 explicit. On Redis both round to the same number.

!!! note "Ambient is the right default"
    Ambient resolution is what makes `micro.override(...)` and `micro.fake()` work, because the lookup happens per call rather than once. Reach for `backend=` on the few call sites a profile actually points at, not everywhere.

## Skip the ambient binding

`micro.install(app)` adds one middleware that binds the app around each request. It costs about 165 nanoseconds per request. Drop it when nothing needs it:

```python
micro.install(app, ambient=False)
```

!!! warning "Only when every call site passes `backend=`"
    Without the binding, a pattern that omits `backend=` raises `OutOfContextError` on the first request that reaches it. The app still starts up healthy, so the failure shows up in production rather than at boot.

    A registered `IdempotentRequests()` needs it too: its middleware resolves the `Cache` through the request scope, so without the binding the first request that carries a key fails.

    `install(ambient=False)` warns at startup when ambient components are registered, and raises under `Grelmicro(strict=True)`. Assert the wiring in a test either way:

    ```python
    def test_ambient_binding_is_wired() -> None:
        assert micro.check_ambient_binding(app)
    ```

[Wiring](wiring.md#fastapi) covers the failure modes in full.

## What not to tune

Do not choose a backend for its per-call compute cost. The in-process algorithms all run in well under a microsecond, so on a distributed backend the round trip decides your latency. Choose a backend for the coordination and durability you need, then tune the path around it.
