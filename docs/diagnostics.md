# Diagnostics

grelmicro reports a handful of problems at startup: a variable that will not be
applied, a backend that cannot keep its promise, a middleware that is missing.
Each one carries a stable **code** so you can grep it, look it up, and assert on
it in a test without pinning the wording.

A code is a slug such as `backend-scope`. It carries no severity letter,
because the same problem is a warning in one deployment and an error in
another. An unmet backend scope is reported when no tier is declared and
refused in `staging` and `production`. Severity belongs to the report, not to
the identity of the problem.

## Reading a report

The code trails the sentence, followed by the page that explains it:

```
Coordination('default') is bound to MemoryLockAdapter, which provides scope
'process', but requires scope 'cluster'. [backend-scope] https://grelmicro.grel.info/diagnostics/#backend-scope
```

The same code travels as a structured field on the log record, so a JSON log
stream can be filtered on `diagnostic` without parsing the message.

## Silencing

There are two kinds of report and they are silenced differently. grelmicro does
not pretend otherwise.

**A warning** has a category of its own. Filter on the category, never on the
message text:

```toml
# pyproject.toml
[tool.pytest.ini_options]
filterwarnings = [
    "error",
    "ignore::grelmicro.BackendScopeWarning",
]
```

Every category derives from `GrelmicroConfigWarning`, so one filter silences
them all:

```toml
filterwarnings = ["ignore::grelmicro.GrelmicroConfigWarning"]
```

The same names work with `-W` and `PYTHONWARNINGS`, and `-W error` promotes any
of them to an exception.

**An error** is raised and cannot be filtered. It is averted by fixing the
configuration it names. Each entry below says which.

## The codes

| Code | Warning category | Error | What it means |
|---|---|---|---|
| `env-load-off` | `EnvLoadOffWarning` | none | A `GREL_*` variable is set but `GREL_ENV_LOAD` is off, so it was not applied. |
| `unknown-environment` | `UnknownEnvironmentWarning` | none | `GREL_ENVIRONMENT` names no known tier, so the backend check runs as if undeclared. |
| `backend-scope` | `BackendScopeWarning` | `BackendScopeError` | A bound backend reaches less far than its component requires. |
| `ambient-binding` | `AmbientBindingWarning` | `AmbientBindingError` | Ambient components are registered but the binding middleware is missing. |
| `provider-order` | none | `LifecycleOrderError` | A Provider is listed after the Component that borrows it. |
| `sentinel-password` | `SentinelPasswordWarning` | none | A Sentinel password is set but the URL scheme cannot apply it. |

### `env-load-off`

Environment-driven configuration is opt-in. Set `GREL_ENV_LOAD=1` to turn it
on, or pass the value directly in code. See [Configuration](config.md).

### `unknown-environment`

Set `GREL_ENVIRONMENT` to `development`, `test`, `staging`, or `production`.

### `backend-scope`

Wire a backend that reaches far enough, or pass `requires=` to declare the
reach you meant. A warning with no tier declared, a `BackendScopeError` in
`staging` and `production`. See [the backend check](deployment.md#the-backend-check).

### `ambient-binding`

Call `micro.install(app)`, including on every mounted sub-application. Raises
`AmbientBindingError` under `Grelmicro(strict=True)` and when another
middleware wraps `GrelmicroMiddleware`. See [Wiring](wiring.md).

### `provider-order`

List providers before the components that borrow them. grelmicro reorders them
for you by default, and raises `LifecycleOrderError` under
`Grelmicro(strict=True)`, which asks for the list you wrote to be the list that
runs.

### `sentinel-password`

The password configures the Sentinel servers, which only a `redis+sentinel://`
URL connects to. Set it with `sentinel_password=` or the matching variable.
