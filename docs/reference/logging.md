# Logging

- **Start here**: [Logging guide](../logging/index.md)
- **Common recipes**: [`configure(...)`](../logging/index.md) for JSON, LOGFMT, TEXT, PRETTY output. [`AccessLog()`](../logging/access.md) for one structured record per HTTP request. [`dict_config()`](../logging/integrations.md) to hand the configuration to the application server. Filters: `DuplicateFilter`, `RateLimitFilter`.

::: grelmicro.log
    options:
      show_submodules: true
      members:
        - AccessLog
        - AccessLogMiddleware
        - DuplicateFilter
        - DuplicateFilterConfig
        - ErrorDict
        - JSONRecordDict
        - Log
        - LogConfig
        - LogError
        - RateLimitFilter
        - RateLimitFilterConfig
        - configure
        - configure_with
        - dict_config
        - dict_config_with
        - formatter

::: grelmicro.log.uvicorn
    options:
      members:
        - UvicornFormatter
        - UvicornAccessFormatter
