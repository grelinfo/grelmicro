# Logging

The `logging` package provides a simple and easy-to-configure logging system.

The logging feature adheres to the 12-factor app methodology, directing logs to stdout. It supports JSON formatting and allows log level configuration via environment variables.

## Dependencies

For the moment the `logging` package is only working with the `loguru` Python logging library.
When `orjson` is installed, it will be used as the default JSON serializer for faster performance, otherwise, the standard `json` library will be used.

[**Loguru**](https://loguru.readthedocs.io/en/stable/overview.html) is used as the logging library.

For using `logging` package, please install the required dependencies:

=== "Standard"
    ```bash
    pip install grelmicro[standard]
    ```

=== "With OpenTelemetry"
    ```bash
    pip install grelmicro[standard,opentelemetry]
    ```

=== "only loguru (minimum)"
    ```bash
    pip install loguru
    ```

=== "loguru and orjson (manual)"
    ```bash
    pip install loguru orjson
    ```


## Configure Logging

Just call the `configure_logging` function to set up the logging system.

```python
--8<-- "logging/configure_logging.py"
```

### Settings

You can change the default settings using the following environment variables:

- `LOG_LEVEL`: Set the desired log level (default: `INFO`). Available options: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.
- `LOG_FORMAT`: Choose the log format. Options are `TEXT` and `JSON`, or you can provide a custom [loguru](https://loguru.readthedocs.io/en/stable/overview.html) template (default: `JSON`).
- `LOG_TIMEZONE`: IANA timezone for timestamps (e.g., `UTC`, `Europe/Zurich`, `America/New_York`) (default: `UTC`).
- `LOG_OTEL_ENABLED`: Enable OpenTelemetry trace context extraction (default: auto-enabled if OpenTelemetry is installed).

#### Timezone Support

The `LOG_TIMEZONE` setting controls the timezone used for all log timestamps in both JSON and TEXT formats. This is particularly useful when running applications across multiple regions or when you need logs in a specific timezone for compliance or debugging purposes.

**JSON Format**: Timestamps are ISO 8601 formatted with timezone offset
```json
{"time":"2024-11-25T15:56:36.066922+01:00",...}  // Europe/Zurich
{"time":"2024-11-25T14:56:36.066922+00:00",...}  // UTC
```

**TEXT Format**: Timestamps are displayed in the format `YYYY-MM-DD HH:MM:SS.mmm`
```
2024-11-25 15:56:36.066 | INFO     | ...  // Europe/Zurich
2024-11-25 14:56:36.066 | INFO     | ...  // UTC
```

#### Structured Logging

When using JSON format, additional context can be passed to logger methods as keyword arguments. These will be captured in the `ctx` field:

```python
--8<-- "logging/structured_logging.py"
```

Output:
```json
{"time":"...","level":"INFO",...,"msg":"User logged in","ctx":{"user_id":123,"ip_address":"192.168.1.1"}}
```

Exceptions are automatically captured in the `ctx` field when using `logger.exception()`:

```python
--8<-- "logging/exception_logging.py"
```

Output:
```json
{"time":"...","level":"ERROR",...,"msg":"Operation failed","ctx":{"operation":"divide","exception":"ZeroDivisionError: division by zero"}}
```

#### OpenTelemetry Integration

The logging system automatically integrates with [OpenTelemetry](https://opentelemetry.io/) for distributed tracing. When you install the `opentelemetry` extras and have an active span, `trace_id` and `span_id` are automatically added to your logs at the top level:

```python
--8<-- "logging/opentelemetry_example.py"
```

Output:
```json
{
  "time": "2026-01-27T16:00:00.000Z",
  "level": "INFO",
  "msg": "Processing request",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "span_id": "00f067aa0ba902b7",
  "ctx": {"user_id": 123}
}
```

Trace fields follow the OpenTelemetry standard and are placed at the JSON root level (not in `ctx`) for compatibility with observability platforms like Jaeger, Zipkin, DataDog, and Grafana Tempo.

To disable: `LOG_OTEL_ENABLED=false`

## Production Deployment

For strict unbuffered output (12-factor compliance), set the `PYTHONUNBUFFERED=1` environment variable in your container runtime.

## Examples

### Basic Usage

Here is a quick example of how to use the logging system:

```python
--8<-- "logging/basic.py"
```

The console output, `stdout` will be:

```json
--8<-- "logging/basic.log"
```

### FastAPI Integration

You can use the logging system with FastAPI as well:

```python
--8<-- "logging/fastapi.py"
```

!!! warning
    It is crucial to call `configure_logging` during the lifespan of the FastAPI application. Failing to do so may result in the FastAPI CLI resetting the logging configuration.

### Different Log Formats

#### JSON Format (Default)

JSON format is ideal for production environments, log aggregation systems, and structured logging:

```
LOG_FORMAT=JSON
LOG_TIMEZONE=Europe/Zurich
```

```python
--8<-- "logging/json_format.py"
```

Output:
```json
{"time":"2024-11-25T15:56:36.066922+01:00","level":"INFO","thread":"MainThread","logger":"__main__:<module>:12","msg":"Application started","ctx":{"version":"1.0.0","environment":"production"}}
```

#### TEXT Format

TEXT format is more human-readable, ideal for local development and debugging:

```
LOG_FORMAT=TEXT
LOG_TIMEZONE=America/New_York
```

```python
--8<-- "logging/text_format.py"
```

Output:
```
2024-11-25 09:56:36.066 | INFO     | __main__:<module>:12 - Application started
```

#### Custom Format

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

## Advanced Usage

!!! warning "API Stability"
    The advanced APIs (`LoguruPatcher`, `json_patcher`, `localtime_patcher`, `otel_patcher`) are subject to change in future versions. For most use cases, prefer using `configure_logging()` with environment variables.

### Using LoguruPatcher Directly

For advanced use cases where you need more control over logging configuration, you can use the `LoguruPatcher` class directly:

```python
--8<-- "logging/advanced_patcher.py"
```

### JSON Record Structure

When using JSON format, log records follow this structure:

```python
class JSONRecordDict:
    time: str              # ISO 8601 timestamp with timezone
    level: str             # Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    msg: str               # Log message
    logger: str | None     # Logger name in format "module:function:line"
    thread: str            # Thread name
    trace_id: str          # Optional: OpenTelemetry trace ID (32 hex chars)
    span_id: str           # Optional: OpenTelemetry span ID (16 hex chars)
    ctx: dict[Any, Any]    # Optional context data (kwargs passed to logger)
```

Example:
```json
{
  "time": "2024-11-25T15:56:36.066922+01:00",
  "level": "INFO",
  "thread": "MainThread",
  "logger": "myapp.service:process_data:42",
  "msg": "Processing complete",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "span_id": "00f067aa0ba902b7",
  "ctx": {
    "records_processed": 1000,
    "duration_ms": 234
  }
}
```

!!! note
    The `trace_id` and `span_id` fields only appear when OpenTelemetry integration is enabled and an active span exists.
