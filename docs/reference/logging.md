# Logging

- **Start here**: [Logging guide](../logging.md)
- **Common recipes**: [`configure(...)`](../logging.md) for JSON, LOGFMT, TEXT, PRETTY output. Filters: `DuplicateFilter`, `RateLimitFilter`.

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
        - LogSettingsValidationError
        - RateLimitFilter
        - RateLimitFilterConfig
        - configure
        - configure_with

::: grelmicro.log.uvicorn
    options:
      members:
        - UvicornFormatter
        - UvicornAccessFormatter
