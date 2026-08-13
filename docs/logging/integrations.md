# Integrations

## OpenTelemetry

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

## FastAPI

```python
--8<-- "log/fastapi.py"
```

!!! warning
    Call `configure` during the lifespan of the FastAPI application. The FastAPI CLI may reset the logging configuration otherwise.

## Uvicorn

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

`ProbeFilter` drops them. Attach it to the access logger, the same way as the [other filters](filters.md):

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
