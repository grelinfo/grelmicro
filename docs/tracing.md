# Tracing

The `tracing` module provides unified instrumentation inspired by Rust's [tracing](https://docs.rs/tracing/latest/tracing/) crate. A single `@instrument` decorator creates OTel spans and enriches log records with structured context automatically.

## Quick Start

```python
from grelmicro.logging import configure_logging
from grelmicro.tracing import instrument, span, add_context
import logging

configure_logging()
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

```python
# Logging only (no OTel dependency needed)
configure_logging()

# Logging + OTel: install opentelemetry and configure your exporter separately.
# @instrument and span() will automatically create OTel spans when opentelemetry
# is installed and a TracerProvider is configured.
```

## Works With All Backends

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
