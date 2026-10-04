# Metrics

- **Start here**: [Metrics guide](../metrics.md)
- **Common recipes**: `Metrics()` component to install an OTel `MeterProvider` for the app's lifetime. `@measure` to time and count a function. `metrics_asgi()` to expose Prometheus metrics on any ASGI framework, or `metrics_router()` from `grelmicro.integrations.fastapi` on FastAPI.

::: grelmicro.metrics
    options:
      show_submodules: true
      members:
        - Metrics
        - MetricsConfig
        - MetricsError
        - MetricsExporterType
        - measure
        - metrics_asgi
