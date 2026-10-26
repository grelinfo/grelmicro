# Diagnostics

grelmicro reports a handful of configuration problems: a variable that will not
be applied, a backend that cannot keep its promise, a middleware that is
missing, a leader election that never runs.
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

A coming change of default or name warns with `GrelmicroFutureWarning`
instead. It is a `FutureWarning`, not a configuration diagnostic, so it carries
no code and the filters above leave it alone. See
[About deprecation](migration.md#about-deprecation).

**An error** is raised and cannot be filtered. It is averted by fixing the
configuration it names. Each entry below says which.

## The codes

| Code | Warning category | Error | What it means |
|---|---|---|---|
| `env-load-off` | `EnvLoadOffWarning` | none | A `GREL_*` variable is set but `GREL_ENV_LOAD` is off, so it was not applied. |
| `unknown-environment` | `UnknownEnvironmentWarning` | none | `GREL_ENVIRONMENT` names no known tier, so the backend check runs as if undeclared. |
| `backend-scope` | `BackendScopeWarning` | `BackendScopeError` | A bound backend reaches less far than its component requires. |
| `ambient-binding` | `AmbientBindingWarning` | `AmbientBindingError` | Ambient components are registered but the binding middleware is missing. |
| `middleware-placement` | `MiddlewarePlacementWarning` | `MiddlewarePlacementError` | A grelmicro middleware wraps middleware the app declared itself, so it would answer before them, cannot see a path it serves, or cannot be added. |
| `provider-order` | none | `LifecycleOrderError` | A Provider is listed after the Component that borrows it. |
| `sentinel-password` | `SentinelPasswordWarning` | none | A Sentinel password is set but the URL scheme cannot apply it. |
| `leader-not-running` | `LeaderNotRunningWarning` | none | A task is gated on a leader election that does not run, so it skips every fire. |
| `ungated-cron` | `UngatedCronWarning` | none | A cron task with no `gate=` runs each fire on every worker, although a schedule backend is registered. |
| `gate-backend` | none | none | A gated task resolves no backend to claim through, so every fire runs nothing. Reported by `grelmicro check` and `micro.describe()`. |
| `health-not-served` | `HealthNotServedWarning` | none | A `HealthChecks` is registered but nothing serves its endpoints, so probes get 404. |
| `routes-not-declared` | `RoutesNotDeclaredWarning` | none | `AuthenticatedRequests` is installed through an integration with no `install_route_gate`, so every route needs a credential, `Anonymous()` included. `grelmicro check` also reports an HTTP integration with no `route_declarations` when a component acting per endpoint is registered. |

### `env-load-off`

`GREL_ENV_LOAD` is set to a falsy value such as `0`, which turns
environment-driven configuration off. Unset it or set `GREL_ENV_LOAD=1` to read
the variable, or pass the value directly in code. See
[Configuration](config.md).

### `unknown-environment`

Set `GREL_ENVIRONMENT` to `development`, `test`, `staging`, or `production`.

### `backend-scope`

Wire a backend that reaches far enough, or pass `requires=` to declare the
reach you meant. A warning with no tier declared, a `BackendScopeError` in
`staging` and `production`. See [the backend check](deployment.md#the-backend-check).

A `@cached(lock=...)` fold is the exception. It warns in every tier that
reports it, `staging` and `production` included, and its first miss raises.

### `ambient-binding`

Call `micro.install(app)`, including on every mounted sub-application. Raises
`AmbientBindingError` under `Grelmicro(strict=True)` and when another
middleware wraps `GrelmicroMiddleware`. See [Wiring](wiring.md).

### `middleware-placement`

Litestar builds its middleware stack when the app is constructed, so
`micro.install(app)` can only wrap the whole thing. A middleware of ours that
answers a request itself, such as an idempotent replay, would then answer
before the app's own middleware runs, authentication included.

Pass it at construction instead, which puts it inside the stack:

```python
app = Litestar(
    route_handlers=[...],
    middleware=[
        DefineMiddleware(AuthMiddleware),
        DefineMiddleware(IdempotencyMiddleware, idempotency=Idempotency("http")),
    ],
)
micro.install(app)  # finds it already wired, and leaves it alone
```

The OpenAPI schema describes the registered component, not the middleware
passed by hand. Register the component with the same settings you pass, or
the schema leaves the middleware out.

FastAPI and Starlette never raise this: `install` puts grelmicro's middleware
behind everything the app added itself.

On Litestar, `micro.install(app)` also warns when an
`AuthenticatedRequestsMiddleware` passed to `Litestar(middleware=[...])`
publishes protected resource metadata, and a `GET` for the document every
challenge points at never reaches it. With no route at the metadata path,
register `AuthenticatedRequests` and call `micro.install(app)`, which adds that
route. With a route of your own there for other methods only, remove it: the
middleware answers every request at that path.

`micro.install(app)` raises `MiddlewarePlacementError` when
`AuthenticatedRequests` is registered and the app's integration has no
`install_middleware`. Nothing would add `AuthenticatedRequestsMiddleware`, so
no request would be authenticated. The app is refused before anything is
wired. Ship `install_middleware`, `install_route_gate` and `route_declarations`
in the integration, as [Plugins](architecture/plugins.md#declare-the-routes)
shows. An app whose integration's `health_endpoints(app)` returns `None`, which
says the app serves no HTTP, is not refused.

### `provider-order`

List providers before the components that borrow them. grelmicro reorders them
for you by default, and raises `LifecycleOrderError` under
`Grelmicro(strict=True)`, which asks for the list you wrote to be the list that
runs.

### `sentinel-password`

The password configures the Sentinel servers, which only a `redis+sentinel://`
URL connects to. Set it with `sentinel_password=` or the matching variable.

### `leader-not-running`

A `LeaderElection` acquires leadership only while it runs. A task gated on one
that nothing runs skips every fire. The task reports it once, on the first fire
it skips a lease duration after it started. Register the election beside the
task with `tasks.add_task(election)`, or start it yourself. An election started
later, by another `Tasks` or a lifespan, is fine as long as it runs within the
lease duration. See [Leader Election](coordination/leader-election.md).

`python -m grelmicro check` and `micro.describe()` report the same code, as a
warning, for a task gated on an election that no registered `Tasks` holds. An
election you start yourself still shows it, so read it as a reminder.

### `ungated-cron`

A cron task runs each fire on every worker unless it passes `gate=`. With a
schedule backend registered, a task that leaves `gate=` out is reported each
time it starts. Pass `gate="claim"` to run each fire on one worker, or `gate=None` to
keep every worker. Quiet in `development` and `test`. `python -m grelmicro
check` shows the same check. See [Run on one worker](task.md#run-on-one-worker).

### `gate-backend`

A task gated with `"claim"`, a `LeaderElection` or a `TaskLock` claims each
fire through a backend: the schedule backend for a cron task, the lock backend
for an interval task. With none to resolve, every fire reports a
`coordination_error` and runs nothing. Register a
[`Coordination`](coordination/index.md) that holds that backend, or pass
`backend=` to the task or its `TaskLock`.

`python -m grelmicro check` and `micro.describe()` report it as a warning in
every tier, since such a task runs nothing anywhere. In `staging` and
`production` the report says it fails there from 0.45.0, and building the
report warns with `Grelmicro045Warning`.

### `health-not-served`

`micro.install(app)` does not mount the health endpoints. Serve them with
`health_router()` on FastAPI, `health_asgi()` on another ASGI framework, or
`OpsServer` on a port of their own. A router or a mount built for a named
`HealthChecks` serves that one, and one built with no argument, like
`OpsServer`, serves the default. See [Health](health.md#without-a-web-framework).

An `OpsServer` switched off with `enabled=False` or `GREL_OPS_ENABLED=false`
serves nothing, and the message says it is switched off. Set
`GREL_OPS_ENABLED=true` to switch it on.

Reported once the app opens, only when it is installed on an app that serves
HTTP, and quiet in `development` and `test`. `python -m grelmicro check` shows
the same check. If you serve the checks some other way, filter
`HealthNotServedWarning`.

### `routes-not-declared`

`AuthenticatedRequests` decides which route serves a caller with no
credential from the declarations an integration hands it, once the framework
routed the request. An integration without `install_route_gate` declares no
route, so every route answers `401` to a request without a credential, a
route declaring `Anonymous()` included. A token is verified as usual.

Reported by `micro.install(app)`. Ship `install_route_gate` and
`route_declarations` in the integration, as
[Declare the routes](architecture/plugins.md#declare-the-routes) shows. Until
then, list the public paths in `exclude=`.

`python -m grelmicro check` and `micro.describe(app)` report the same code
when a component acting per endpoint, such as `CachedResponses` or
`AccessLog`, is registered and the integration serves HTTP without
`route_declarations`. The endpoint table then stays empty and no
`include=` pattern is checked until the integration ships it.
