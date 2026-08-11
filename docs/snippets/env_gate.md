!!! warning "Environment variables are opt-in"
    A `GREL_*` variable that fills a component field is read only when
    `GREL_ENV_LOAD` is truthy (`1`, `true`, `yes`, `on`), the flag itself and
    `GREL_ENVIRONMENT` excepted. Without it the variable is ignored and the
    default applies.
    Setting one while the flag is unset warns at startup, so the mistake is not
    silent. Passing `env_load=False` is a deliberate opt-out and stays quiet. See
    [How a value is resolved](https://grelmicro.grel.info/config/#how-a-value-is-resolved)
    for the three ways to configure a component, including a local `.env`.
