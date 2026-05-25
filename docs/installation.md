# Installation

grelmicro supports Python 3.12+ and depends on Pydantic v2+ and FastDepends. The async runtime is `asyncio`. Trio is not supported.

## Why Python 3.12

grelmicro uses [PEP 695](https://peps.python.org/pep-0695/) type
parameter syntax, structural [`Self`](https://peps.python.org/pep-0673/)
returns, and [`asyncio.timeout`](https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout)
on every primitive. These are 3.11/3.12 features. Pinning the floor
to 3.12 keeps the codebase free of conditional imports and lets `ty`
check a single typing dialect.

CI runs the test matrix on every advertised classifier (3.12, 3.13,
3.14). Older Python versions are not in scope before 1.0.

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
| `postgres` | `asyncpg` for the PostgreSQL sync backend. | All platforms. |
| `sqlite` | `aiosqlite` for the SQLite sync backend. | All platforms. |
| `kubernetes` | `lightkube` for the Kubernetes Lease sync backend. | All platforms. |
| `opentelemetry` | OpenTelemetry API and SDK for tracing integration. | All platforms. |
| `structlog` | `structlog` as an alternative logging backend. | All platforms. |

=== "pip"
    ```bash
    pip install "grelmicro[standard]"
    pip install "grelmicro[redis]"
    pip install "grelmicro[postgres]"
    pip install "grelmicro[sqlite]"
    pip install "grelmicro[kubernetes]"
    pip install "grelmicro[opentelemetry]"
    pip install "grelmicro[structlog]"
    ```

=== "uv"
    ```bash
    uv add "grelmicro[standard]"
    uv add "grelmicro[redis]"
    uv add "grelmicro[postgres]"
    uv add "grelmicro[sqlite]"
    uv add "grelmicro[kubernetes]"
    uv add "grelmicro[opentelemetry]"
    uv add "grelmicro[structlog]"
    ```

=== "poetry"
    ```bash
    poetry add "grelmicro[standard]"
    poetry add "grelmicro[redis]"
    poetry add "grelmicro[postgres]"
    poetry add "grelmicro[sqlite]"
    poetry add "grelmicro[kubernetes]"
    poetry add "grelmicro[opentelemetry]"
    poetry add "grelmicro[structlog]"
    ```

Combine multiple extras in one call, for example `pip install "grelmicro[redis,opentelemetry,structlog]"`.
