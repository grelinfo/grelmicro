# Logging

- **Start here**: [Logging guide](../logging/index.md)
- **Common recipes**: [`configure(...)`](../logging/index.md) for JSON, LOGFMT, TEXT, PRETTY output. Filters: `DuplicateFilter`, `RateLimitFilter`.

::: grelmicro.log
    options:
      show_submodules: true
      members:
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

::: grelmicro.log.uvicorn
    options:
      members:
        - UvicornFormatter
        - UvicornAccessFormatter
