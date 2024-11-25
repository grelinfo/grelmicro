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
{!> ../examples/logging/configure_logging.py!}
```

### Settings

You can change the default settings using the following environment variables:

- `LOG_LEVEL`: Set the desired log level (default: `INFO`).
- `LOG_FORMAT`: Choose the log format. Options are `TEXT` and `JSON`, or you can provide a custom [loguru](https://loguru.readthedocs.io/en/stable/overview.html) template (default: `TEXT`).


## Examples

### Basic Usage

Here is a quick example of how to use the logging system:

```python
{!> ../examples/logging/basic.py!}
```

The console output, `stdout` will be:

```json
{!> ../examples/logging/basic.log!}
```

### FastAPI Integration

You can use the logging system with FastAPI as well:

```python
{!> ../examples/logging/fastapi.py!}
```

!!! warning
    It is crucial to call `configure_logging` during the lifespan of the FastAPI application. Failing to do so may result in the FastAPI CLI resetting the logging configuration.