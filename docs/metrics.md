# Metrics

The `metrics` module records OpenTelemetry metrics for your app. Add one `Metrics()` component and grelmicro emits metrics from every built-in component. Add `@measure` to time your own functions. Add `metrics_router()` to expose a Prometheus endpoint.

## Quick Start

```python
--8<-- "metrics/component.py"
```

`Metrics()` installs an OpenTelemetry `MeterProvider` for the app's lifetime. The provider is installed on enter and restored to the prior global on exit, so sequential apps in tests do not stack providers.

!!! tip "Install"
    Metrics need the `opentelemetry` extra: `pip install "grelmicro[opentelemetry]"`. See the [installation guide](installation.md) for `uv` and `poetry`. Without the extra, the metric calls built into every component are no-ops, so an app that does not register `Metrics()` runs normally. Registering a `Metrics()` component does require the extra: it raises `DependencyNotFoundError` at startup when OpenTelemetry is missing.

## Exporters

Pick an exporter with the `exporter` field or the `GREL_METRICS_EXPORTER` environment variable.

| Exporter     | Use it for                                        |
| ------------ | ------------------------------------------------- |
| `auto`       | Exporting over OTLP HTTP when an endpoint is set, otherwise a no-op (default). |
| `otlp-http`  | Sending metrics to an OpenTelemetry collector.    |
| `otlp-grpc`  | The same, over gRPC.                              |
| `prometheus` | Serving a `/metrics` endpoint that Prometheus scrapes. |
| `console`    | Printing metrics to the console in development.   |
| `none`       | Installing the provider without exporting.        |

!!! tip "Off until an endpoint is configured"
    `Metrics()` defaults to `MetricsExporterType.AUTO`. It exports over OTLP HTTP when an endpoint is configured (the `endpoint` argument, `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`, or `OTEL_EXPORTER_OTLP_ENDPOINT`) and otherwise auto-disables into a true no-op: it installs no meter provider and leaves the global provider untouched. So you register `Metrics()` unconditionally and it stays silent in dev, test, and CI instead of falling back to `localhost:4318`, and it never conflicts with a second app. A bounded `shutdown_timeout` (default `5.0` seconds) caps the flush on exit, so a slow or unreachable collector cannot hang shutdown.

    For local development, set `exporter="console"` to print metrics, or `exporter="prometheus"` to expose them on a `/metrics` route. An explicit `none` is different from the auto-disable above: it still installs the provider so custom instruments record, they are just not exported. Leave the exporter on `auto` for the unconditional no-op.

!!! note "Basic auth in one line"
    Backends like OpenObserve want an `Authorization: Basic` header. Pass `basic_auth=(username, password)` and `Metrics` builds and attaches it to the exporter:

    ```python
    Metrics(
        service_name="orders",
        endpoint="https://obs.example.com/api/default/v1/metrics",
        basic_auth=("me@example.com", password),
    )
    ```

    From the environment, set `GREL_METRICS_BASIC_AUTH_USERNAME` and `GREL_METRICS_BASIC_AUTH_PASSWORD` instead. The header is attached on the exporter directly, so it bypasses the `OTEL_EXPORTER_OTLP_HEADERS` encoding where base64 padding (`=`) can be mangled or dropped.

!!! note "The config never prints your credentials"
    An endpoint can embed credentials in its userinfo or query (`https://usr:token@collector/v1`), and header values are API keys. On `MetricsConfig` the `endpoint` field is a [`SecretUrl`][grelmicro.types.SecretUrl] and each `headers` value is a `SecretStr`, so `repr()`, `model_dump()`, and `model_dump_json()` show `***` in their place. The endpoint keeps its scheme, host, and path readable, so you can still see which collector is configured. Read either back with `get_secret_value()`. What grelmicro sends to the collector is unchanged.

`Metrics()` reads `GREL_METRICS_*` environment variables (see `MetricsConfig` for the full field set) or accepts the same fields as keyword arguments. The OTLP and Prometheus exporters require their own packages and are imported only when selected.

## Measure your own functions

`@measure` times a function and counts its calls. It works on sync and async functions.

```python
from grelmicro.metrics import measure


@measure
async def charge_card(amount: int) -> None:
    ...


@measure(name="orders.checkout", record_in_flight=True)
async def checkout(cart_id: str) -> None:
    ...
```

`@measure` emits three metrics, named from the function or the `name` you pass:

- `<name>.duration`: a histogram of seconds.
- `<name>.calls`: a counter with an `outcome` attribute set to `success` or `error`. On failure an `error.type` attribute carries the exception class name.
- `<name>.active`: an up_down_counter that rises while the function runs and falls when it returns. Only when `record_in_flight=True`.

Every metric is a no-op when no `Metrics` component is active, so a decorated function is safe to ship even when metrics are off.

## Custom instruments

The component builds OpenTelemetry instruments for you. Each accessor takes a `unit` and a `description`.

```python
async with micro:
    orders = micro.metrics.counter("orders.placed", unit="1")
    orders.add(1, {"channel": "web"})

    latency = micro.metrics.histogram("checkout.latency", unit="s")
    latency.record(0.42)

    in_flight = micro.metrics.up_down_counter("checkout.active", unit="1")
    in_flight.add(1)
```

Use `counter` for values that only increase, `up_down_counter` for values that rise and fall, `gauge` for a last-known value, and `histogram` for distributions. Keep attribute keys bounded: a small fixed set like `channel` is fine, but never use unbounded values like user ids or cache keys.

## Prometheus endpoint

With the `prometheus` exporter, `metrics_router()` adds a `GET /metrics` route that returns the Prometheus exposition format.

```python
--8<-- "metrics/router.py"
```

Pass `prefix`, `path`, and `dependencies` to mount the route elsewhere or gate it behind authentication. The router resolves the default `Metrics` component from the running app, or you can pass one explicitly with `metrics_router(component)`.

## Built-in metrics

When a `Metrics` component is active, grelmicro emits these metrics from its own components. All durations are histograms in seconds. All attributes are bounded: component names are fixed at construction, never per-call keys or ids.

| Metric                                  | Type            | Unit | Attributes                              |
| --------------------------------------- | --------------- | ---- | --------------------------------------- |
| `grelmicro.health.check.up`             | gauge           | 1    | `check.name`, `critical`                |
| `grelmicro.health.check.duration`       | histogram       | s    | `check.name`, `outcome`                 |
| `grelmicro.circuit_breaker.calls`       | counter         | 1    | `circuit_breaker.name`, `result`        |
| `grelmicro.circuit_breaker.transitions` | counter         | 1    | `circuit_breaker.name`, `from`, `to`    |
| `grelmicro.circuit_breaker.state`       | gauge           | 1    | `circuit_breaker.name`                  |
| `grelmicro.retry.attempts`              | counter         | 1    | `retry.name`, `outcome`                 |
| `grelmicro.retry.duration`              | histogram       | s    | `retry.name`                            |
| `grelmicro.rate_limiter.decisions`      | counter         | 1    | `rate_limiter.name`, `decision`         |
| `grelmicro.bulkhead.active`             | up_down_counter | 1    | `bulkhead.name`                         |
| `grelmicro.bulkhead.rejections`         | counter         | 1    | `bulkhead.name`                         |
| `grelmicro.timeout.exceeded`            | counter         | 1    | `timeout.name`                          |
| `grelmicro.cache.operations`            | counter         | 1    | `result` (`hit` or `miss`)              |
| `grelmicro.cache.stale_serves`          | counter         | 1    | none                                    |
| `grelmicro.task.runs`                   | counter         | 1    | `task.name`, `outcome`, `error.type`    |
| `grelmicro.task.duration`               | histogram       | s    | `task.name`                             |
| `grelmicro.task.active`                 | up_down_counter | 1    | `task.name`                             |

The `grelmicro.circuit_breaker.state` gauge maps states to codes: `CLOSED` is 0, `OPEN` is 1, `HALF_OPEN` is 2, `FORCED_OPEN` is 3, `FORCED_CLOSED` is 4.
