!!! note "Environment variables are read by default"
    A `GREL_*` variable that fills a component field is read unless
    `GREL_ENV_LOAD=0`. With `GREL_ENV_LOAD=0`, a variable that is set is
    ignored, the default applies, and startup warns with its name. Passing
    `env_load=False` to a component turns its reads off and stays quiet. See
    [How a value is resolved](https://grelmicro.grel.info/config/#how-a-value-is-resolved)
    for the three ways to configure a component, including a local `.env`.
