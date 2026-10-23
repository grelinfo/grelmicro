# Installation

grelmicro supports Python 3.14+ and depends on Pydantic v2+ and FastDepends. The async runtime is `asyncio`. Trio is not supported.

!!! warning "Python 3.13 or older installs an old release"
    On Python 3.13 or older, `pip install grelmicro` installs 0.41.x, which these docs do not cover.
    Install `"grelmicro>=0.42"` to get an error instead of the old release.

## Supported Python versions

grelmicro supports up to three Python releases, starting with 3.14. Today that is 3.14 and 3.15. Python 3.16 joins once CI tests it. When a fourth release joins, the oldest one drops.

grelmicro builds on 3.14 features, such as the standard library
[`uuid.uuid7`](https://docs.python.org/3/library/uuid.html#uuid.uuid7)
for outbox ids. One floor keeps the codebase free of version checks.

CI runs the tests on Python 3.14 and 3.15. Older Python versions are not supported.

## Quick install

=== "pip"
    ```bash
    pip install grelmicro
    ```

=== "uv"
    ```bash
    uv add grelmicro
    ```

=== "poetry"
    ```bash
    poetry add grelmicro
    ```

## Optional extras

grelmicro is modular. Install only the extras you need.

| Extra | Pulls in | Platforms |
|---|---|---|
| `standard` | `orjson` for fast JSON serialization. `uvloop` for a faster event loop. Activate with `uvloop.run(main())`. | `orjson` everywhere. `uvloop` only on Linux/macOS CPython (skipped on Windows and PyPy). |
| `redis` | `redis-py` for the Redis backends. | All platforms. |
| `valkey` | `valkey-py` for the Valkey backends. | All platforms. |
| `postgres` | `asyncpg` for the PostgreSQL backends. | All platforms. |
| `sqlite` | `aiosqlite` for the SQLite backends. | All platforms. |
| `kubernetes` | `lightkube` for the Kubernetes Lease backend. | All platforms. |
| `fastapi` | `fastapi>=0.142.1` and `starlette>=1.7.0` for the FastAPI integration. | All platforms. |
| `starlette` | `starlette>=1.7.0` for the Starlette integration. | All platforms. |
| `litestar` | `litestar` for the Litestar integration. | All platforms. |
| `faststream` | `faststream` for the FastStream integration. | All platforms. |
| `opentelemetry` | OpenTelemetry API and SDK for the `Trace` and `Metrics` components. | All platforms. |
| `instrumentation` | OpenTelemetry instrumentation packages (Redis, asyncpg) for `Trace(instrument=...)`. | All platforms. |
| `structlog` | `structlog` as an alternative logging backend. | All platforms. |
| `yaml` | `pyyaml` for YAML sources in `ExternalConfig`. | All platforms. |
| `jwt` | `grelmicro-core`, the compiled Rust core behind JWT verification. | Prebuilt wheels for Linux (glibc and musl), macOS, and Windows on CPython 3.14 and 3.15, free-threaded 3.14t and 3.15t included. Elsewhere it builds from source and needs a Rust toolchain. |

=== "pip"
    ```bash
    pip install "grelmicro[standard]"
    pip install "grelmicro[redis]"
    pip install "grelmicro[valkey]"
    pip install "grelmicro[postgres]"
    pip install "grelmicro[sqlite]"
    pip install "grelmicro[kubernetes]"
    pip install "grelmicro[fastapi]"
    pip install "grelmicro[starlette]"
    pip install "grelmicro[litestar]"
    pip install "grelmicro[faststream]"
    pip install "grelmicro[opentelemetry]"
    pip install "grelmicro[instrumentation]"
    pip install "grelmicro[structlog]"
    pip install "grelmicro[yaml]"
    pip install "grelmicro[jwt]"
    ```

=== "uv"
    ```bash
    uv add "grelmicro[standard]"
    uv add "grelmicro[redis]"
    uv add "grelmicro[valkey]"
    uv add "grelmicro[postgres]"
    uv add "grelmicro[sqlite]"
    uv add "grelmicro[kubernetes]"
    uv add "grelmicro[fastapi]"
    uv add "grelmicro[starlette]"
    uv add "grelmicro[litestar]"
    uv add "grelmicro[faststream]"
    uv add "grelmicro[opentelemetry]"
    uv add "grelmicro[instrumentation]"
    uv add "grelmicro[structlog]"
    uv add "grelmicro[yaml]"
    uv add "grelmicro[jwt]"
    ```

=== "poetry"
    ```bash
    poetry add "grelmicro[standard]"
    poetry add "grelmicro[redis]"
    poetry add "grelmicro[valkey]"
    poetry add "grelmicro[postgres]"
    poetry add "grelmicro[sqlite]"
    poetry add "grelmicro[kubernetes]"
    poetry add "grelmicro[fastapi]"
    poetry add "grelmicro[starlette]"
    poetry add "grelmicro[litestar]"
    poetry add "grelmicro[faststream]"
    poetry add "grelmicro[opentelemetry]"
    poetry add "grelmicro[instrumentation]"
    poetry add "grelmicro[structlog]"
    poetry add "grelmicro[yaml]"
    poetry add "grelmicro[jwt]"
    ```

Combine multiple extras in one call, for example `pip install "grelmicro[redis,valkey,opentelemetry,structlog]"`.

A framework extra installs the framework at the oldest version grelmicro runs on, as the table lists. Install the extra even when your app already depends on the framework, or pin the framework at that floor in your own dependencies. An older framework raises `DependencyVersionError` at import, naming the extra to install. The [Frameworks](frameworks.md) page lists what `micro.install(app)` wires for each.
