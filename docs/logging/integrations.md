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

Uvicorn's handlers are kept, so a custom one is never dropped. A handler still on `sys.stdout` or `sys.stderr` is pointed at the stream the rest of the process writes to, so a request line goes through the [queue](index.md) and lands beside your own records instead of on a second file descriptor. Uvicorn's error lines move off `sys.stderr` with it, which is what one process writing one stream means. Pass `uvicorn_enabled=False` to keep uvicorn's own streams.

This works because uvicorn configures logging while building its `Config`, then imports your application module, and only then logs its first line. A `configure()` call at import time gets there first.

Pass `uvicorn_enabled=False` when uvicorn's logging is configured elsewhere:

```python
configure(uvicorn_enabled=False)
```

## Application servers

Two cases sit outside what `configure()` can reach, because your application module is never imported in time:

- `--reload` and `--workers` run a parent process that only supervises. Its lines, `Started parent process` and `Child process died` among them, keep uvicorn's format.
- A script that calls `configure()` and then `uvicorn.run(app)` has its root configuration replaced, because uvicorn applies its own while building `Config`.

Hand the server a configuration instead, and the process reads in one format from its first record:

```python
--8<-- "log/dict_config.py"
```

The same document goes to every server. Gunicorn and Hypercorn take it as `logconfig_dict`, Granian as `log_dictconfig`:

```python
# gunicorn.conf.py
from grelmicro.log import dict_config

logconfig_dict = dict_config()
```

Every logger those servers write to is handed to the root logger, so a server line and an application line render the same way. Uvicorn's access logger keeps a formatter of its own, because uvicorn carries the request in the record's arguments rather than in its message.

Settings resolve from `GREL_LOG_*` when the document is built, and the document carries them. It is a snapshot, not a template. Reading the environment is opt-in as it is everywhere else, so `GREL_ENV_LOAD=1` has to be set for `GREL_LOG_*` to be read at all. A process that cannot set it passes `dict_config(env_load=True)` instead. To build one from settings assembled in code, use `dict_config_with(config)`.

`queue_enabled` is honoured too. The handler starts the writer when none is running, so a document applied on its own puts the whole process behind the queue.

It is a plain `dictConfig` document, so it is also what goes in the file `uvicorn --log-config` reads. Write it where the container starts, not where the image is built, so it snapshots the environment the process actually runs with:

```bash
python -c 'import json; from grelmicro.log import dict_config; print(json.dumps(dict_config()))' > logging.json
uvicorn app:app --log-config logging.json
```

An application that writes through loguru or structlog calls `configure()` as well, which adds the backend. Each pass replaces the root handler rather than adding one, so the last one applied is what the process reads in, and uvicorn applies the document before it imports your application.

A `configure()` that passes keyword arguments resolves settings the document never sees. Build the document from what it returns, so both render the same:

```python
config = configure(format="pretty")
uvicorn.run(app, log_config=dict_config_with(config))
```

## Quieting health probes

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
