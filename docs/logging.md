# Logging

Zero-config logging that follows the **12-factor app** methodology. Use it to get structured, environment-aware logs without wiring handlers by hand.

- **Zero-config**: logs go to stdout, the format is picked automatically.
- **Structured**: extra fields become flat top-level keys, exceptions become structured error data.
- **Environment-driven**: every knob is a `GREL_LOG_*` environment variable, read when `GREL_ENV_LOAD` is enabled, or passed straight to `configure()`.

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

## OpenTelemetry Integration

When [OpenTelemetry](https://opentelemetry.io/) is installed, `trace_id` and `span_id` are automatically added to logs:

```python
--8<-- "log/opentelemetry_example.py"
```

Output:
```json
{
  "time": "2026-01-27T16:00:00.000Z",
  "level": "INFO",
  "msg": "Processing request",
  "logger": "myapp.service",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "span_id": "00f067aa0ba902b7",
  "user_id": 123
}
```

Trace fields follow the OpenTelemetry standard and are placed at the JSON root level for compatibility with observability platforms (Jaeger, Zipkin, DataDog, Grafana Tempo).

To disable: `GREL_LOG_OTEL_ENABLED=false`

## FastAPI Integration

```python
--8<-- "log/fastapi.py"
```

!!! warning
    Call `configure` during the lifespan of the FastAPI application. The FastAPI CLI may reset the logging configuration otherwise.

## Uvicorn Integration

Uvicorn has its own logging system separate from your application: it installs its own handlers and turns propagation off, so its lines never reach the handler `configure()` sets up. Left alone, one process emits two formats.

`configure()` fixes that for you. No log config file, no change to how uvicorn is started:

```python
from grelmicro.log import configure

configure()
```

```
time=2026-08-05T13:10:49.805834+00:00 level=INFO msg="Application startup complete." logger=uvicorn.error
time=2026-08-05T13:10:49.807003+00:00 level=INFO msg="POST /orders 200" logger=uvicorn.access client_addr=127.0.0.1:54321 method=POST full_path=/orders http_version=1.1 status_code=200
time=2026-08-05T13:10:49.807224+00:00 level=INFO msg="Order created" logger=myapp order_id=a1b2c3
```

Uvicorn's handlers are kept and only their formatter is replaced, so the stderr/stdout split survives and access lines keep their structured fields.

This works because uvicorn configures logging while building its `Config`, before it imports your application module, so a `configure()` call at import time runs afterwards. A process that configures logging *before* uvicorn starts is not covered.

Pass `uvicorn_enabled=False` when uvicorn's logging is configured elsewhere, such as with `--log-config`:

```python
configure(uvicorn_enabled=False)
```

### Configuring uvicorn with a log config file

The file-based route still works, and is the option when you are not calling `configure()` at all:

```json
--8<-- "log/uvicorn_log_config.json"
```

Then start uvicorn with:

```bash
uvicorn app:app --log-config uvicorn_log_config.json
```

`UvicornFormatter` and `UvicornAccessFormatter` read `GREL_LOG_FORMAT` at startup and produce the matching output (AUTO, JSON, LOGFMT, TEXT, PRETTY). This ensures uvicorn logs and application logs use the same format.

`UvicornAccessFormatter` additionally parses uvicorn's access log arguments into structured fields: `client_addr`, `method`, `full_path`, `http_version`, `status_code`.

### Quieting health probes

Kubernetes polls `/livez`, `/readyz` and `/healthz` every few seconds for the life of the pod, and the access log reports every one. In a healthy pod they are close to the only thing in the log.

`ProbeFilter` drops them. Attach it to the access logger, the same way as the other filters on this page:

```python
--8<-- "log/probes.py"
```

**A failing probe is still logged.** Only responses below `400` are dropped, so a readiness check that starts refusing traffic still shows up. That line is often the only evidence the kubelet asked and was refused, so hiding it with the noise would remove the one thing worth reading.

**Paths are matched by suffix**, so `health_router(prefix="/api/v1")` is covered with no configuration. A query string is ignored, so `/healthz?exclude=redis` is still recognised as a probe.

Pass `paths=` to cover other polled endpoints. It replaces the defaults rather than adding to them:

```python
logging.getLogger("uvicorn.access").addFilter(
    ProbeFilter(paths=("/livez", "/readyz", "/healthz", "/metrics"))
)
```

It is a plain `logging.Filter`, so it also works in a `dictConfig`, on another access logger, or removed again with `removeFilter`.

## Deduplicating Noisy Logs

`DuplicateFilter` is a `logging.Filter` that silences repeated log records.

```python
--8<-- "log/duplicate_filter.py"
```

After **5** identical records, the filter silently drops any further occurrences. It tracks up to **100** distinct keys in an LRU cache.

`key_mode="template"` (default) uses the raw format string as the key, so `%`-style calls with different arguments share one counter. It is also about **3 times faster** than rendered keying. `DuplicateFilter` and `RateLimitFilter` share the same five `key_mode` values:

| `key_mode` | Counter scope | Good for |
|---|---|---|
| `"logger"` | One counter per logger name | Collapse every record from a noisy logger |
| `"level"` | One counter per log level | Collapse all records at a level |
| `"global"` | One shared counter | Collapse everything into one budget |
| `"template"` (default) | One counter per (logger, level, `str(record.msg)`) | Shares across arg values of the same template |
| `"rendered"` | One counter per (logger, level, `record.getMessage()`) | Distinguishes fully-rendered messages |

Use `key_mode="rendered"` to track each rendered message separately, or pass `key=` for a custom fingerprint:

```python
logger.addFilter(DuplicateFilter(key_mode="rendered"))
logger.addFilter(DuplicateFilter(key=lambda r: (r.name, r.exc_info)))
```

Set `ttl` to re-emit a burst of `allowed_repetitions` records every window during sustained floods, so operators continue to receive periodic reminders:

```python
logger.addFilter(DuplicateFilter(allowed_repetitions=5, ttl=300))
```

State is in-process only. There is no cross-process sharing and no explicit reset API: construct a new filter if you need to wipe counters.

!!! tip
    `DuplicateFilter` attaches to any stdlib logger, so it works with every `GREL_LOG_BACKEND`. For code using `from loguru import logger` or `structlog.get_logger()` directly, use those libraries' native filtering.

## Rate-Limiting Noisy Logs

`RateLimitFilter` is a `logging.Filter` that drops records when a token bucket is empty. It allows bursts: up to `capacity` records can pass through at once, and the bucket then refills at `refill_rate` records per second.

```python
--8<-- "log/rate_limit_filter.py"
```

By default the filter buckets **per logger**: each logger has its own burst budget. Swap `key_mode` for different grouping:

| `key_mode` | Bucket scope | Good for |
|---|---|---|
| `"logger"` (default) | One bucket per logger name | Noisy third-party libraries that flood a single logger |
| `"level"` | One bucket per log level | Throttle all WARNING/ERROR across the app |
| `"global"` | One shared bucket | App-wide safety net on the root handler |
| `"template"` | One bucket per (logger, level, `str(record.msg)`) | Shares across arg values of the same template |
| `"rendered"` | One bucket per (logger, level, `record.getMessage()`) | Distinguishes fully-rendered messages |

```python
--8<-- "log/rate_limit_filter_global.py"
```

Pass a custom `key=` callable for any other grouping:

```python
logger.addFilter(
    RateLimitFilter(
        capacity=20,
        refill_rate=2,
        key=lambda r: f"{r.name}|{r.exc_info is not None}",
    )
)
```

Use `cost=` when a record should spend multiple tokens (e.g. on a verbose-level handler):

```python
logger.addFilter(RateLimitFilter(capacity=100, refill_rate=10, cost=2))
```

State is in-process only, backed by [`MemoryTokenBucket`][grelmicro.resilience.MemoryTokenBucket]. Call `filter.reset(key)` to clear one key, or construct a new filter to wipe all state.

!!! tip
    `RateLimitFilter` and `DuplicateFilter` compose well: attach the dedup filter first to collapse true duplicates, then the rate-limit filter to cap the sustained flow.

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
| `GREL_LOG_JSON_SERIALIZER` | `stdlib`, `orjson` | `stdlib` |
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
"time":"2026-04-01T15:56:36.066922+01:00"
```

**TEXT / PRETTY**: Localized time
```
2026-04-01 15:56:36.066
```

## Custom Format (Loguru only)

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
    For high-throughput applications, use `GREL_LOG_JSON_SERIALIZER=orjson` with `structlog` or `stdlib` backend.

### Why orjson is not selected automatically

Installing orjson does not change anything until you also select it. That is deliberate, and it is the one place in the logging module that is not auto-detected.

The two serializers do not agree on every payload:

| Value in `extra={...}` | `stdlib` | `orjson` |
|---|---|---|
| `float("nan")`, `float("inf")` | `NaN`, `Infinity` | `null` |
| a non-string dict key | coerced to a string | raises `TypeError` |

Picking a serializer because a package happens to be importable would mean an unrelated dependency pulling in orjson could change what your logs say, or turn a working log call into an exception on a payload that used to serialize. A log line that crashes the request is worse than a log line that is slower.

So the choice stays yours. Set `GREL_LOG_JSON_SERIALIZER=orjson`, or pass `json_serializer="orjson"` to `configure()`, once you know your payloads are compatible.

Run the benchmark:
```bash
python benchmarks/logging_benchmark.py
```
