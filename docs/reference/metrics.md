# Metrics

- **Start here**: [Metrics guide](../metrics.md)
- **Common recipes**: `Metrics()` component to install an OTel `MeterProvider` for the app's lifetime. `@measure` to time and count a function. `metrics_router()` to expose Prometheus metrics.

::: grelmicro.metrics
    options:
      show_submodules: true
      members:
        - Metrics
        - MetricsConfig
        - MetricsError
        - MetricsExporterType
        - MetricsSettingsValidationError
        - measure
        - metrics_router
