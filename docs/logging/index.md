# Logging

Zero-config logging that follows the **12-factor app** methodology. Use it to get structured, environment-aware logs without wiring handlers by hand.

- **Zero-config**: logs go to stdout, the format is picked automatically.
- **Structured**: extra fields become flat top-level keys, exceptions become structured error data.
- **Environment-driven**: every knob is a `GREL_LOG_*` environment variable, read when `GREL_ENV_LOAD` is enabled, or passed straight to `configure()`.

Two more pages cover the rest: [Integrations](integrations.md) for OpenTelemetry,
FastAPI, and uvicorn, and [Filters](filters.md) for taming a noisy logger.

## Quick Start

```python
--8<-- "log/configure.py"
```

Or attach it to a `Grelmicro` app via `uses=`:

```python
--8<-- "log/component.py"
```

`Log()` accepts the same knobs as `configure()` and resolves `GREL_LOG_*` environment variables. On exit, the previous stdlib root handlers are restored.

With no environment variables set, `configure()` detects your terminal:

- **Terminal (TTY)**: human-readable colored text
- **Piped / CI / container**: structured JSON

This is the `AUTO` format (the default). Most users never need to set `GREL_LOG_FORMAT`.

## Backends

grelmicro supports three logging backends. All backends produce **identical output** for each format, so switching is easy. Select one with the `GREL_LOG_BACKEND` environment variable, then use the matching logger.

| Backend | Dependencies | Best for |
|---------|-------------|----------|
| **stdlib** (default) | None | Zero-dependency setups |
| **[Loguru](https://loguru.readthedocs.io/)** | `loguru` | Developer ergonomics |
| **[structlog](https://www.structlog.org/)** | `structlog` | High-throughput services |

The backend is how your own code writes a record. Whichever one you pick,
anything logging through the standard library renders in the same format on
the same stream: grelmicro's own components, and every dependency you run,
httpx, SQLAlchemy and redis among them. One process, one format.

`configure()` owns the root logger, so a handler attached to it beforehand is
replaced. Attach yours afterwards, or to a logger of its own.

The exception is a loguru format template of your own. `JSON`, `LOGFMT`,
`TEXT` and `PRETTY` are formats grelmicro renders on both sides, and a
template asking for the serialized record is read as one of them. Any other
template is loguru's alone, so records from the standard library are written
in grelmicro's text format instead.

=== "stdlib"
    ```python
    import logging

    configure()
    logger = logging.getLogger(__name__)
    logger.info("Hello, World!", extra={"user_id": 123})
    ```

=== "Loguru"
    ```python
    from loguru import logger

    configure()
    logger.info("Hello, World!", user_id=123)
    ```

=== "structlog"
    ```python
    import structlog

    configure()
    log = structlog.get_logger()
    log.info("Hello, World!", user_id=123)
    ```

??? note "Installation"
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

Every level and an exception, on the loguru backend:

```python title="basic.py"
--8<-- "log/basic.py"
```

## Structured Logging

Extra context fields are passed as keyword arguments and appear as flat top-level fields:

```python
--8<-- "log/structured_logging.py"
```

Output:
```json
{"time":"...","level":"INFO","msg":"User logged in","logger":"...","user_id":123,"ip_address":"192.168.1.1"}
```

## Exception Handling

Exceptions are automatically captured as structured `ErrorDict`:

```python
--8<-- "log/exception_logging.py"
```

JSON output:
```json
{"time":"...","level":"ERROR","msg":"Operation failed","logger":"...","operation":"divide","error":{"type":"ZeroDivisionError","message":"division by zero","stack":"..."}}
```

??? note "LOGFMT and PRETTY output"
    LOGFMT output:
    ```
    time=... level=ERROR msg="Operation failed" logger=... error.type=ZeroDivisionError error.message="division by zero" error.stack="Traceback..."
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

## Log Formats

grelmicro provides **five format options**, following common structured-logging conventions:

| Format | Use Case | Machine-Parseable |
|--------|----------|-------------------|
| `AUTO` | **Default**. Adapts to environment | Depends |
| `JSON` | Production, log aggregation | Yes |
| `LOGFMT` | Structured + human-readable | Yes |
| `TEXT` | Local development | No |
| `PRETTY` | Verbose debugging | No |

### AUTO (Default)

Detects the output target and selects the best format automatically:

| Condition | Selected Format |
|-----------|-----------------|
| `stdout` is a TTY (terminal) | `TEXT` (colored) |
| `stdout` is piped or redirected | `JSON` |
| `FORCE_COLOR` env var set | `TEXT` (colored) |
| `NO_COLOR` env var set | `JSON` |

```python
--8<-- "log/auto_format.py"
```

In your terminal:
```
2026-04-01 10:30:00.123 INFO     __main__ - Application started version=1.0.0
```

In a container or CI:
```json
{"time":"2026-04-01T08:30:00.123456+00:00","level":"INFO","msg":"Application started","logger":"__main__","version":"1.0.0"}
```

??? note "JSON, LOGFMT, TEXT, and PRETTY formats"
    #### JSON

    Structured newline-delimited JSON. Ideal for production, log aggregation (Datadog, Loki, ELK).

    ```
    GREL_LOG_FORMAT=JSON
    ```

    ```python
    --8<-- "log/json_format.py"
    ```

    Output:
    ```json
    {"time":"2026-04-01T10:30:00.123456+02:00","level":"INFO","msg":"Application started","logger":"__main__","version":"1.0.0","environment":"production"}
    ```

    #### LOGFMT

    Key-value pairs following the [logfmt](https://brandur.org/logfmt) convention. 30-40% smaller than JSON, grep-friendly, parseable by Grafana Loki and most log tools.

    ```
    GREL_LOG_FORMAT=LOGFMT
    ```

    ```python
    --8<-- "log/logfmt_format.py"
    ```

    Output:
    ```
    time=2026-04-01T10:30:00.123456+00:00 level=INFO msg="Request handled" logger=__main__ method=GET path=/health status=200
    ```

    Nested dicts use dot notation:
    ```
    error.type=ValueError error.message="invalid input"
    ```

    #### TEXT

    Single-line, human-readable output. Includes extra fields as `key=value` pairs. Colors are enabled when output is a TTY.

    ```
    GREL_LOG_FORMAT=TEXT
    ```

    ```python
    --8<-- "log/text_format.py"
    ```

    Output:
    ```
    2026-04-01 10:30:00.123 INFO     __main__:<module>:12 - Application started version=1.0.0
    ```

    #### PRETTY

    Multi-line format with indented fields. Best for debugging with low log volume.

    ```
    GREL_LOG_FORMAT=PRETTY
    ```

    ```python
    --8<-- "log/pretty_format.py"
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

### Custom Format (Loguru only)

You can provide a custom [loguru format template](https://loguru.readthedocs.io/en/stable/api/logger.html#message):

```
GREL_LOG_FORMAT="{level} | {message}"
```

```python
--8<-- "log/custom_format.py"
```

Output:
```
INFO | Custom format example
```

!!! note
    Custom format strings only work with the loguru backend.

## Settings

Every setting can be passed to `configure()` directly, or read from the
environment:

--8<-- "env_gate.md"

| Variable | Values | Default |
|----------|--------|---------|
| `GREL_LOG_BACKEND` | `stdlib`, `loguru`, `structlog` | `stdlib` |
| `GREL_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | `INFO` |
| `GREL_LOG_FORMAT` | `AUTO`, `JSON`, `LOGFMT`, `TEXT`, `PRETTY` | `AUTO` |
| `GREL_LOG_TIMEZONE` | IANA timezone (e.g., `UTC`, `Europe/Zurich`) | `UTC` |
| `GREL_LOG_JSON_SERIALIZER` | `auto`, `stdlib`, `orjson` | `auto` |
| `GREL_LOG_CALLER_ENABLED` | `true`, `false` | `false` |
| `GREL_LOG_OTEL_ENABLED` | `true`, `false` | auto-detected |
| `GREL_LOG_UVICORN_ENABLED` | `true`, `false` | `true` |
| `NO_COLOR` | any value | (unset) |
| `FORCE_COLOR` | any value | (unset) |

!!! note "Color Support"
    Colors follow the [NO_COLOR](https://no-color.org) and [FORCE_COLOR](https://force-color.org) standards.
    When `NO_COLOR` is set, `AUTO` resolves to `JSON` and colors are disabled.
    `FORCE_COLOR` takes precedence over `NO_COLOR`.

### Timezone

The `GREL_LOG_TIMEZONE` setting controls timestamps in all formats:

```
GREL_LOG_TIMEZONE=Europe/Zurich
```

**JSON / LOGFMT**: ISO 8601 with timezone offset
```
"time":"2026-04-01T15:56:36.066922+02:00"
```

**TEXT / PRETTY**: local time with its offset, `Z` when that is UTC
```
2026-04-01 15:56:36.066+02:00
2026-04-01 13:56:36.066Z
```

Leave it unset to follow [`GREL_TIMEZONE`](../config.md#one-timezone-for-the-whole-service),
the wall clock the whole service runs on. Set it to `UTC` to keep log
timestamps on UTC under a service that schedules on local time.

### Which serializer you get

The default is `auto`: orjson when it is installed, the standard library when it is not. Install [`grelmicro[standard]`](../installation.md) and your logs get faster with no further setting. See the [benchmarks](../benchmarks.md#logging) for what that buys.

`auto` never writes less than the standard library would. An object JSON has no encoding for is rendered as text rather than raising, a dict keyed by numbers is written with string keys, and anything orjson declines falls back to the standard library, so switching orjson on cannot cost you a record.

Going the other way is not symmetric. `stdlib` refuses a dict key that is not a string, number, boolean, or `None`, where orjson writes a `datetime`, `UUID`, or `Enum` key. Pinning `stdlib` is the narrower choice.

They do not write every value the same way:

| Value in `extra={...}` | `stdlib` | `orjson` |
|---|---|---|
| `float("nan")`, `float("inf")` | `NaN`, `Infinity` | `null` |
| `"Zürich"` | `"Z\u00fcrich"` | `"Zürich"` |
| `UUID(...)` | `"UUID('0d8e...')"` | `"0d8e..."` |
| `Enum`, dataclass | `repr` of the object | the value, the fields |

orjson renders more types natively and writes UTF-8. The standard library escapes non-ASCII and falls back to `repr`. The text of a field can change when a deployment gains orjson. Note the first row goes the other way: bare `NaN` is not valid JSON, so a strict reader rejects the standard library's line where it accepts orjson's `null`.

So pin the serializer when the exact bytes matter, for example when a downstream parser matches on a field:

```bash
export GREL_LOG_JSON_SERIALIZER=stdlib   # or orjson
```

`orjson` raises `DependencyNotFoundError` at startup when it is not installed, so a deployment that depends on it fails loudly rather than quietly getting the slower serializer. `auto` never raises for a missing dependency, it just takes what is there.

## JSON Record Structure

All JSON log records follow this schema. Required fields are always present, optional fields may be absent. Extra context fields are merged flat at the top level:

```python
class JSONRecordDict:
    # Required
    time: str              # ISO 8601 timestamp with timezone
    level: str             # DEBUG, INFO, WARNING, ERROR, CRITICAL
    msg: str               # Log message
    logger: str            # Logger name (e.g., "myapp.api")
    # Optional (opt-in via GREL_LOG_CALLER_ENABLED=true)
    caller: str            # function:line (e.g., "handle:45")
    # Optional
    trace_id: str          # OpenTelemetry trace ID (32 hex chars)
    span_id: str           # OpenTelemetry span ID (16 hex chars)
    error: ErrorDict       # Structured error info
```

The `ErrorDict` structure:

```python
class ErrorDict:
    type: str              # Exception class name (e.g., "ValueError")
    message: str           # Exception message
    stack: str             # Optional: full traceback string
```

??? note "Design decisions"
    **Level casing**: UPPERCASE (`DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`), following common structured-logging conventions.

    **Field naming**: Core field names (`time`, `level`, `msg`, `logger`, `caller`, `error`) follow common structured-logging conventions. `logger` is the logger name, `caller` is the call site (`function:line`).

    **Caller opt-in**: `caller` is disabled by default, as in many structured-logging libraries. Enable with `GREL_LOG_CALLER_ENABLED=true`. Uvicorn formatters never include `caller` (points to uvicorn internals, not application code).

    **Collision protection**: Core fields cannot be overwritten by user-supplied extra context.

## Production Deployment

For strict unbuffered output (12-factor compliance):

```bash
PYTHONUNBUFFERED=1
```
