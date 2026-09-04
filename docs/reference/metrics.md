# Metrics

- **Start here**: [Metrics guide](../metrics.md)
- **Common recipes**: `Metrics()` component to install an OTel `MeterProvider` for the app's lifetime. `@measure` to time and count a function. `metrics_router()` to expose Prometheus metrics on FastAPI, `metrics_asgi()` on any other ASGI framework.

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
        - metrics_router
