# Tracing

- **Start here**: [Tracing guide](../tracing.md)
- **Common recipes**: `@instrument` to emit spans and enrich log records. `Trace()` component to install an OTel `TracerProvider` for the app's lifetime.

::: grelmicro.trace
    options:
      show_submodules: true
      members:
        - Trace
        - TraceConfig
        - TraceError
        - TraceExporterType
        - TraceProcessorType
        - TraceSamplerType
        - TraceSettingsValidationError
        - add_context
        - get_context
        - instrument
        - span
