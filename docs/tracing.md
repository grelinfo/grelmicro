# Tracing

Unified instrumentation. Use it to enrich logs with structured context and create distributed traces from one decorator.

- **One decorator**: `@instrument` captures function arguments as context.
- **Logs always enriched**: context flows into every log record, no OpenTelemetry needed.
- **Traces when installed**: with OpenTelemetry present, the same fields become OTel span attributes.

## Quick Start

```python
from grelmicro.log import configure
from grelmicro.trace import instrument, span, add_context
import logging

configure()
logger = logging.getLogger(__name__)

@instrument
async def process_order(order_id: str, user_id: str):
    logger.info("started")
    # {"time":...,"level":"INFO","msg":"started","logger":...,"order_id":"ORD-1","user_id":"USR-1"}

    add_context(payment_status="pending")
    logger.info("payment initiated")
    # {"time":...,"level":"INFO","msg":"payment initiated","logger":...,"order_id":"ORD-1","user_id":"USR-1","payment_status":"pending"}

    with span("db_query", table="orders"):
        logger.info("querying")
        # {"time":...,"level":"INFO","msg":"querying","logger":...,"order_id":"ORD-1","user_id":"USR-1","payment_status":"pending","table":"orders"}

    logger.info("done")
    # table removed (span exited), payment_status still present
```

## API

### `@instrument`

Decorator that captures function arguments as structured context. Works with sync and async functions.

```python
# Bare decorator: captures all arguments
@instrument
async def process(order_id: str, user_id: str): ...

# Skip sensitive arguments
@instrument(skip={"password", "token"})
async def login(username: str, password: str): ...

# Skip all arguments
@instrument(skip_all=True)
async def bulk_process(payload: bytes): ...

# Custom span name
@instrument(name="db.query")
async def fetch_user(user_id: str): ...
```

### `span()`

Context manager for mid-function instrumentation. Creates a nested context that is automatically removed on exit.

```python
@instrument
async def handle_request(request_id: str):
    logger.info("received")  # has request_id

    with span("auth", method="jwt"):
        logger.info("authenticating")  # has request_id + method

    with span("db", table="users"):
        logger.info("querying")  # has request_id + table

    logger.info("done")  # back to request_id only
```

### `add_context()`

Add fields to the current context. Updates both log records and the active OTel span (if tracing is configured).

```python
@instrument
async def process(order_id: str):
    result = charge()
    add_context(payment_id=result.id, status=result.status)
    logger.info("charged")  # includes payment_id and status
```

## Configuration

The tracing context enriches log records regardless of how logging is configured. No additional configuration is needed.

When OpenTelemetry is installed, `@instrument` and `span()` also create OTel spans and add the same fields as span attributes. A single decorator produces both structured logs and distributed traces.

!!! tip "Install"
    OpenTelemetry integration needs the `opentelemetry` extra: `pip install "grelmicro[opentelemetry]"`. See the [installation guide](installation.md) for `uv` and `poetry`.

### Standalone

```python
# Logging only (no OTel dependency needed)
configure()

# Logging + OTel: install opentelemetry and configure your exporter separately.
# @instrument and span() will automatically create OTel spans when opentelemetry
# is installed and a TracerProvider is configured.
```

### Via Grelmicro app

`Trace()` owns the `TracerProvider` lifecycle when registered on a `Grelmicro` app:

```python
--8<-- "trace/component.py"
```

!!! tip "Off until an endpoint is configured"
    `Trace()` defaults to `TraceExporterType.AUTO`. It exports over OTLP HTTP when an endpoint is configured (the `endpoint` argument, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, or `OTEL_EXPORTER_OTLP_ENDPOINT`) and otherwise no-ops. So you register `Trace()` unconditionally and it stays silent in dev, test, and CI instead of falling back to `localhost:4318`. A bounded `shutdown_timeout` (default `5.0` seconds) caps the flush on exit, so a slow or unreachable collector cannot hang shutdown.

    For local development, set `exporter=TraceExporterType.CONSOLE` to print spans to the console. Use `TraceExporterType.NONE` to force export off even when an endpoint is set.

!!! note "Basic auth in one line"
    Backends like OpenObserve want an `Authorization: Basic` header. Pass `basic_auth=(username, password)` and `Trace` builds and attaches it to the exporter:

    ```python
    Trace(
        service_name="orders",
        endpoint="https://obs.example.com/api/default/v1/traces",
        basic_auth=("me@example.com", password),
    )
    ```

    From the environment, set `GREL_TRACE_BASIC_AUTH_USERNAME` and `GREL_TRACE_BASIC_AUTH_PASSWORD` instead. The header is attached on the exporter directly, so it bypasses the `OTEL_EXPORTER_OTLP_HEADERS` encoding where base64 padding (`=`) can be mangled or dropped.

??? note "Provider lifecycle and exporters"
    The provider is installed on enter and restored to the prior global on exit, so sequential apps in tests do not stack providers.

    `Trace()` reads `GREL_TRACE_*` environment variables (see `TraceConfig` for the full field set) or accepts the same fields as keyword arguments. The OTLP HTTP and gRPC exporters require their own packages (`opentelemetry-exporter-otlp-proto-http` or `opentelemetry-exporter-otlp-proto-grpc`) and are imported only when selected.

??? note "Works with all logging backends"
    The tracing context is injected into all three logging backends (stdlib, loguru, structlog). Use whichever logger you prefer:

    ```python
    # stdlib
    import logging
    logger = logging.getLogger(__name__)
    logger.info("message", extra={"key": "value"})

    # loguru
    from loguru import logger
    logger.info("message", key="value")

    # structlog
    import structlog
    log = structlog.get_logger()
    log.info("message", key="value")
    ```

    All produce the same JSON output with tracing context included.

## Automatic instrumentation

`@instrument` traces your own functions. To trace incoming HTTP requests and database or cache calls without touching every handler, `Trace` auto-instruments the FastAPI app, the providers it manages, and **every other library you use** that ships an OpenTelemetry instrumentor.

Install the instrumentor packages alongside the OpenTelemetry SDK:

```bash
pip install "grelmicro[opentelemetry,instrumentation]"
```

The `instrumentation` extra bundles the FastAPI, Redis, and asyncpg instrumentors. **The set of installed `opentelemetry-instrumentation-*` packages defines what gets traced** : add `opentelemetry-instrumentation-sqlalchemy` or `opentelemetry-instrumentation-httpx` and `Trace` wires them too, with no code change. This matters when your app uses its own database client (a SQLAlchemy or asyncpg engine) instead of a grelmicro `PostgresProvider` : the spans appear all the same.

Then `Trace` does the rest. Request spans wrap each handler, and database, cache, and outbound HTTP spans nest under them, all bound to the app's tracer provider:

```python
--8<-- "trace/autoinstrument.py"
```

`instrument` is on by default and degrades to a no-op when an instrumentor package is absent, so it does nothing until you install the extras. A grelmicro-managed Redis client is traced precisely (per-client). Every other installed instrumentor is attached process-wide, so a library the app uses through its own client is traced without a grelmicro provider.

Select what to instrument by OpenTelemetry instrumentor name (the same names as `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS`):

```python
Trace(instrument=False)                      # nothing (the @instrument decorator still works)
Trace(instrument=["fastapi", "asyncpg"])     # only the named targets, an unknown name raises
Trace(instrument={"sqlalchemy": False})      # every installed target except the named ones
```

What is covered:

| Target | Spans | Notes |
|---|---|---|
| FastAPI | Incoming HTTP requests | wired by `micro.install(app)`, FastAPI apps only |
| Redis | Cache and lock commands | per-client when grelmicro owns the client, cluster included |
| asyncpg | Queries | covers a grelmicro `PostgresProvider` and any app-owned asyncpg or SQLAlchemy-on-asyncpg engine |
| Any other installed instrumentor | Per that library | e.g. `sqlalchemy`, `httpx`, `psycopg` : install the package and it is wired |
| Valkey, SQLite | None | No OpenTelemetry package exists. Use `@instrument` for these. |

!!! note "asyncpg and SQLAlchemy together"
    If both the asyncpg and SQLAlchemy instrumentors are installed they would double-span the same queries (SQLAlchemy runs through asyncpg). `Trace` keeps asyncpg and drops SQLAlchemy with a warning. Pass `instrument={"asyncpg": False}` to trace at the SQLAlchemy layer instead.

Under a FastStream app, the provider and library spans are produced, but FastStream message spans are not auto-instrumented yet. Use `@instrument` for handler spans.
