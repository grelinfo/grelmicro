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

!!! warning "The default exporter expects a collector"
    `Trace()` defaults to `TraceExporterType.OTLP_HTTP`, which sends spans to an OpenTelemetry collector or endpoint over OTLP HTTP. Without a reachable collector, spans are dropped. A bounded `shutdown_timeout` (default `5.0` seconds) caps the flush on exit, so a slow or unreachable collector cannot hang shutdown.

    For local development with no collector, set `exporter=TraceExporterType.CONSOLE` to print spans to the console instead. Use `TraceExporterType.NONE` to disable export entirely.

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
