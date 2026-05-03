# Installation

grelmicro supports Python 3.12+ and depends on Pydantic v2+, AnyIO v4+, and FastDepends.

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

| Extra | Pulls in |
|---|---|
| `standard` | `orjson` for fast JSON serialization. |
| `redis` | `redis-py` for the Redis backends. |
| `postgres` | `asyncpg` for the PostgreSQL sync backend. |
| `sqlite` | `aiosqlite` for the SQLite sync backend. |
| `kubernetes` | `lightkube` for the Kubernetes Lease sync backend. |
| `opentelemetry` | OpenTelemetry API and SDK for tracing integration. |
| `structlog` | `structlog` as an alternative logging backend. |

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
