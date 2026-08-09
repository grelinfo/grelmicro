# Types

- **Start here**: [Credentials in a config object](../providers.md#credentials-in-a-config-object)
- **Common recipes**: annotate a settings field that holds a connection URL with `SecretUrl[RedisDsn]` so the password never reaches a log line, and read it back with `get_secret_value()`.

::: grelmicro.types
    options:
      members:
        - SecretUrl
        - TimeZoneName
        - LogLevel
