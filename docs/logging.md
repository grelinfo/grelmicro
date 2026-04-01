# Logging

The `logging` package provides a simple, zero-config logging system following the **12-factor app** methodology.

Logs go to stdout. Format is selected automatically. Configuration is done via environment variables.

## Quick Start

```python
--8<-- "logging/configure_logging.py"
```

With no environment variables set, `configure_logging()` detects your terminal:

- **Terminal (TTY)**: human-readable colored text
- **Piped / CI / container**: structured JSON

This is the `AUTO` format (the default).

## Backend Selection

grelmicro supports three logging backends. All backends produce **identical output** for each format, making it easy to switch.

| Backend | Dependencies | Best for |
|---------|-------------|----------|
| **stdlib** (default) | None | Zero-dependency setups |
| **[Loguru](https://loguru.readthedocs.io/)** | `loguru` | Developer ergonomics |
| **[structlog](https://www.structlog.org/)** | `structlog` | High-throughput services |

### Installation

=== "stdlib (no dependencies)"
    No additional dependencies required. Uses Python's built-in `logging` module.

=== "Loguru"
    ```bash
    pip install grelmicro[standard]
    ```

=== "structlog"
    ```bash
    pip install grelmicro[structlog]
    ```

=== "With OpenTelemetry"
    ```bash
    pip install grelmicro[standard,opentelemetry]
    # or
    pip install grelmicro[structlog,opentelemetry]
    ```

### Usage

Select the backend with the `LOG_BACKEND` environment variable, then use the corresponding logger:

=== "Loguru"
    ```python
    from loguru import logger

    configure_logging()
    logger.info("Hello, World!", user_id=123)
    ```

=== "structlog"
    ```python
    import structlog

    configure_logging()
    log = structlog.get_logger()
    log.info("Hello, World!", user_id=123)
    ```

=== "stdlib"
    ```python
    import logging

    configure_logging()
    logger = logging.getLogger(__name__)
    logger.info("Hello, World!", extra={"user_id": 123})
    ```

## Log Formats

grelmicro provides **five format options**, inspired by the Rust [tracing](https://docs.rs/tracing-subscriber/latest/tracing_subscriber/fmt/index.html) ecosystem and Go [slog](https://pkg.go.dev/log/slog):

| Format | Use Case | Machine-Parseable | Inspired By |
|--------|----------|-------------------|-------------|
| `AUTO` | **Default**. Adapts to environment | Depends | zerolog, pino |
| `JSON` | Production, log aggregation | Yes | pino, tracing JSON, slog JSON |
| `LOGFMT` | Structured + human-readable | Yes | Go slog TextHandler, tracing-logfmt |
| `TEXT` | Local development | No | tracing Full, loguru |
| `PRETTY` | Verbose debugging | No | tracing Pretty, structlog ConsoleRenderer |

### AUTO (Default)

Detects the output target and selects the best format automatically:

| Condition | Selected Format |
|-----------|-----------------|
| `stdout` is a TTY (terminal) | `TEXT` (colored) |
| `stdout` is piped or redirected | `JSON` |
| `FORCE_COLOR` env var set | `TEXT` (colored) |
| `NO_COLOR` env var set | `JSON` |

```python
--8<-- "logging/auto_format.py"
```

In your terminal:
```
2026-04-01 10:30:00.123 INFO     __main__:<module>:12 - Application started version=1.0.0
```

In a container or CI:
```json
{"time":"2026-04-01T08:30:00.123456+00:00","level":"INFO","msg":"Application started","caller":"__main__:<module>:12","version":"1.0.0"}
```

!!! tip "Zero Config"
    `AUTO` is the default. Most users never need to set `LOG_FORMAT`.

### JSON

Structured newline-delimited JSON. Ideal for production, log aggregation (Datadog, Loki, ELK).

```
LOG_FORMAT=JSON
```

```python
--8<-- "logging/json_format.py"
```

Output:
```json
{"time":"2026-04-01T10:30:00.123456+02:00","level":"INFO","msg":"Application started","caller":"__main__:<module>:12","version":"1.0.0","environment":"production"}
```

### LOGFMT

Key-value pairs following the [logfmt](https://brandur.org/logfmt) convention. 30-40% smaller than JSON, grep-friendly, parseable by Grafana Loki and most log tools.

```
LOG_FORMAT=LOGFMT
```

```python
--8<-- "logging/logfmt_format.py"
```

Output:
```
time=2026-04-01T10:30:00.123456+00:00 level=INFO msg="Request handled" caller=__main__:<module>:10 method=GET path=/health status=200
```

Nested dicts use dot notation:
```
error.type=ValueError error.message="invalid input"
```

### TEXT

Single-line, human-readable output. Includes extra fields as `key=value` pairs. Colors are enabled when output is a TTY.

```
LOG_FORMAT=TEXT
```

```python
--8<-- "logging/text_format.py"
```

Output:
```
2026-04-01 10:30:00.123 INFO     __main__:<module>:12 - Application started version=1.0.0
```

### PRETTY

Multi-line format with indented fields. Best for debugging with low log volume.

```
LOG_FORMAT=PRETTY
```

```python
--8<-- "logging/pretty_format.py"
```

Output:
```
  2026-04-01 10:30:00.123 INFO Request handled
    at __main__:<module>:10
    method: GET
    path: /health
    status: 200
```

With exceptions:
```
  2026-04-01 10:30:01.456 ERROR Operation failed
    at myapp.service:process:78
    error.type: ZeroDivisionError
    error.message: division by zero
    error.stack:
      Traceback (most recent call last):
        File "service.py", line 78, in process
          result = 1 / 0
      ZeroDivisionError: division by zero
```

## Settings

All configuration is done via environment variables:

| Variable | Values | Default |
|----------|--------|---------|
| `LOG_BACKEND` | `stdlib`, `loguru`, `structlog` | `stdlib` |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | `INFO` |
| `LOG_FORMAT` | `AUTO`, `JSON`, `LOGFMT`, `TEXT`, `PRETTY` | `AUTO` |
| `LOG_TIMEZONE` | IANA timezone (e.g., `UTC`, `Europe/Zurich`) | `UTC` |
| `LOG_JSON_SERIALIZER` | `stdlib`, `orjson` | `stdlib` |
| `LOG_OTEL_ENABLED` | `true`, `false` | auto-detected |
| `NO_COLOR` | any value | (unset) |
| `FORCE_COLOR` | any value | (unset) |

!!! note "Color Support"
    Colors follow the [NO_COLOR](https://no-color.org) and [FORCE_COLOR](https://force-color.org) standards.
    When `NO_COLOR` is set, `AUTO` resolves to `JSON` and colors are disabled.
    `FORCE_COLOR` takes precedence over `NO_COLOR`.

### Timezone

The `LOG_TIMEZONE` setting controls timestamps in all formats:

```
LOG_TIMEZONE=Europe/Zurich
```

**JSON / LOGFMT**: ISO 8601 with timezone offset
```
"time":"2026-04-01T15:56:36.066922+01:00"
```

**TEXT / PRETTY**: Localized time
```
2026-04-01 15:56:36.066
```

## Structured Logging

Extra context fields are passed as keyword arguments and appear as flat top-level fields:

```python
--8<-- "logging/structured_logging.py"
```

Output:
```json
{"time":"...","level":"INFO","msg":"User logged in","caller":"...","user_id":123,"ip_address":"192.168.1.1"}
```

## Exception Handling

Exceptions are automatically captured as structured `ErrorDict`:

```python
--8<-- "logging/exception_logging.py"
```

JSON output:
```json
{"time":"...","level":"ERROR","msg":"Operation failed","caller":"...","operation":"divide","error":{"type":"ZeroDivisionError","message":"division by zero","stack":"..."}}
```

LOGFMT output:
```
time=... level=ERROR msg="Operation failed" caller=... error.type=ZeroDivisionError error.message="division by zero" error.stack="Traceback..."
```

PRETTY output:
```
  ... ERROR Operation failed
    at ...
    operation: divide
    error.type: ZeroDivisionError
    error.message: division by zero
    error.stack:
      Traceback (most recent call last):
        ...
      ZeroDivisionError: division by zero
```

## OpenTelemetry Integration

When [OpenTelemetry](https://opentelemetry.io/) is installed, `trace_id` and `span_id` are automatically added to logs:

```python
--8<-- "logging/opentelemetry_example.py"
```

Output:
```json
{
  "time": "2026-01-27T16:00:00.000Z",
  "level": "INFO",
  "msg": "Processing request",
  "caller": "myapp.service:process_request:42",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "span_id": "00f067aa0ba902b7",
  "user_id": 123
}
```

Trace fields follow the OpenTelemetry standard and are placed at the JSON root level for compatibility with observability platforms (Jaeger, Zipkin, DataDog, Grafana Tempo).

To disable: `LOG_OTEL_ENABLED=false`

## FastAPI Integration

```python
--8<-- "logging/fastapi.py"
```

!!! warning
    Call `configure_logging` during the lifespan of the FastAPI application. The FastAPI CLI may reset the logging configuration otherwise.

## Uvicorn Integration

Uvicorn has its own logging system separate from your application. To get consistent output between uvicorn and your app, use the format-aware uvicorn formatters via a log config file:

```json
--8<-- "logging/uvicorn_log_config.json"
```

Then start uvicorn with:

```bash
uvicorn app:app --log-config uvicorn_log_config.json
```

`UvicornFormatter` and `UvicornAccessFormatter` read `LOG_FORMAT` at startup and produce the matching output (AUTO, JSON, LOGFMT, TEXT, PRETTY). This ensures uvicorn logs and application logs use the same format.

`UvicornAccessFormatter` additionally parses uvicorn's access log arguments into structured fields: `client_addr`, `method`, `full_path`, `http_version`, `status_code`.

!!! note "Backward Compatibility"
    The old names `UvicornJSONFormatter` and `UvicornAccessJSONFormatter` are kept as deprecated aliases that emit `DeprecationWarning`.

## Custom Format (Loguru only)

You can provide a custom [loguru format template](https://loguru.readthedocs.io/en/stable/api/logger.html#message):

```
LOG_FORMAT="{level} | {message}"
```

```python
--8<-- "logging/custom_format.py"
```

Output:
```
INFO | Custom format example
```

!!! note
    Custom format strings only work with the loguru backend.

## JSON Record Structure

All JSON log records follow this schema. Core fields are always present, extra context fields are merged flat at the top level:

```python
class JSONRecordDict:
    time: str              # ISO 8601 timestamp with timezone
    level: str             # DEBUG, INFO, WARNING, ERROR, CRITICAL
    msg: str               # Log message
    caller: str            # module:function:line
    trace_id: str          # Optional: OpenTelemetry trace ID (32 hex chars)
    span_id: str           # Optional: OpenTelemetry span ID (16 hex chars)
    error: ErrorDict       # Optional: structured error info
```

The `ErrorDict` structure:

```python
class ErrorDict:
    type: str              # Exception class name (e.g., "ValueError")
    message: str           # Exception message
    stack: str             # Optional: full traceback string
```

### Design Decisions

**Level casing**: UPPERCASE (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`), following Go slog, Java Log4j2, Rust tracing conventions.

**Field naming**: Core field names (`time`, `level`, `msg`, `caller`, `error`) follow slog/zap conventions.

**Collision protection**: Core fields cannot be overwritten by user-supplied extra context.

## Production Deployment

For strict unbuffered output (12-factor compliance):

```bash
PYTHONUNBUFFERED=1
```

## Performance

Benchmark results (50,000 iterations):

| Backend | Serializer | Ops/sec | vs Best |
|---------|------------|---------|---------|
| structlog | orjson | 302,273 | 100.0% |
| stdlib | orjson | 269,353 | 89.1% |
| structlog | stdlib | 198,000 | 65.5% |
| loguru | orjson | 192,953 | 63.8% |
| stdlib | stdlib | 181,745 | 60.1% |
| loguru | stdlib | 147,185 | 48.7% |

!!! tip "Performance Recommendation"
    For high-throughput applications, use `LOG_JSON_SERIALIZER=orjson` with `structlog` or `stdlib` backend.

Run the benchmark:
```bash
python benchmarks/logging_benchmark.py
```
