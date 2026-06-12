# Config

- **Start here**: [Reconfigure from a ConfigMap](../configuration/reconfigure-from-configmap.md)
- **Common recipes**: add `ExternalConfig("/etc/grelmicro")` to `Grelmicro(uses=[...])` to keep live components in sync with a mounted ConfigMap or Secret. Call `reload()` in tests for a deterministic apply pass.

::: grelmicro.config
    options:
      members:
        - ExternalConfig
        - ConfigBackend
        - FileConfigAdapter
