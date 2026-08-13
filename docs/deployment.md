# Deployment

What a container image and a Kubernetes manifest need before they run a
grelmicro application in production.

## Declare the environment

Set `GREL_ENVIRONMENT=production` in the manifest of every deployed
environment:

```bash
GREL_ENVIRONMENT=production
```

Four values name a tier: `development`, `test`, `staging`, and `production`.
They are the well-known values of the OpenTelemetry
[`deployment.environment.name`](https://opentelemetry.io/docs/specs/semconv/resource/deployment-environment/)
attribute, and grelmicro writes the declared value into that attribute on the
tracer resource, so the same variable names your environment in every trace.

Declaring `production` or `staging` turns on the backend check below. This is
the one place to set it per environment, so unlike `GREL_ENV_LOAD` it belongs
in the manifest rather than the image.

Any other value names no tier grelmicro can act on, so it neither gates the
check nor reaches the trace attribute. It reads as undeclared, and says so:

```
GrelmicroConfigWarning: GREL_ENVIRONMENT='preprod' is not one of development,
test, staging, production, so the backend check runs as if it were
undeclared.
```

A fleet that calls its tiers `qa` and `preprod` keeps booting, and
`prodution` is loud instead of silent.

The value names a tier, not the name your organisation gave the environment.
Map yours to the closest of the four: a deployed environment that runs more
than one replica is `staging`, a throwaway CI run is `test`. You know which
one an environment called `integration` is, and grelmicro does not.

### The backend check

A `Lock` on a memory backend is not a lock. It excludes nothing once a second
replica runs, and without a check it says nothing about it. So in `production`
and `staging`, a pattern that promises a guarantee across replicas refuses to
start on a backend that cannot deliver it:

```
BackendScopeError: Coordination('default') is bound to MemoryLockAdapter,
which provides scope 'process', but requires scope 'cluster' in environment
'production'. Use a Redis, Valkey, Postgres, or Kubernetes backend, or pass
requires= to say what reach you want.
```

The check runs before the first connection opens, so a misconfigured pod
fails at boot instead of on the request that needed the lock.

A backend **provides** a scope, and a component **requires** one. The message
names both sides, so the fix is in the sentence that reports the problem.

| Backend scope | Backends | State is shared by |
| --- | --- | --- |
| `process` | Memory | One process |
| `host` | SQLite | The processes on one host |
| `cluster` | Redis, Valkey, Postgres, Kubernetes | Every process that connects |

`cluster` means every process connected to that backend, wherever those
processes run. One Redis serving two Kubernetes clusters still holds one lock.

`Coordination` and `Outbox` require `cluster`, because a lock, a leader, a
distributed cron and an outbox all promise something across replicas. `Cache`,
`RateLimiterComponent` and `CircuitBreakerComponent` require `process`: a
per-replica cache and a per-replica circuit breaker are the standard shape,
not a mistake.

Say what you want with `requires=` and the default no longer applies:

```python
--8<-- "deployment/requires.py"
```

It reads in both directions. Lower the bar to accept the scope you have, or
raise it to make the wiring you meant a startup condition: a
`RateLimiterComponent(redis, requires="cluster")` fails at boot the day
someone points it at memory. A `Cache` that an
[`Idempotency`](idempotency/index.md) reads from wants `requires="cluster"` too,
because a replay has to find the stored response wherever the retry lands.

Only a bound backend is checked. A component you never registered is the
[documented way](architecture/backends.md#distribution-model) to say you want
local behavior: a `@cron` with no `Coordination` fires on every replica on
purpose. Wiring a memory schedule backend instead says you expected the fleet
to agree, so that one is reported.

A [`Bulkhead(uses=[...])`](resilience/bulkhead.md) holds components the app
never registers, so those are checked on first entry to the scope instead of
at startup.

### When the environment is not declared

An undeclared environment still reports a backend that cannot keep its
promise. The report is a `GrelmicroConfigWarning` and a `WARNING` on the
`grelmicro` logger, once at startup, on both channels like the
[ignored-variable report](#when-the-flag-is-missing):

```
GrelmicroConfigWarning: Coordination('default') is bound to MemoryLockAdapter,
which provides scope 'process', but requires scope 'cluster'. Set
GREL_ENVIRONMENT to declare where this runs, or pass requires='process' to
say that is the reach you want.
```

So the three states differ in severity, not in what they find:

| `GREL_ENVIRONMENT` | A backend that cannot keep the promise |
| --- | --- |
| `production`, `staging` | `BackendScopeError` at startup |
| unset | Warning on both channels, once |
| `development`, `test` | Nothing |

Declare `test` in your test suite and the memory backends every test wires up
go quiet. [Testing](testing.md#declare-the-test-environment) shows where.

### Check it before it deploys

`micro.check_backends()` asks the question the deployed app will ask, from a
process that declares something else, so a test catches the wiring before a
pod does. It raises the same `BackendScopeError`, naming every binding that
does not hold:

```python
def test_backends_hold_across_replicas() -> None:
    micro.check_backends()
```

It checks against `production` by default. Pass `environment=` to ask about
another one.

## Turn on environment configuration

Set `GREL_ENV_LOAD=1` in the image:

```dockerfile
ENV GREL_ENV_LOAD=1
```

Every `GREL_*` variable that fills a component field is read only when this
flag is truthy (`1`, `true`, `yes`, `on`). Without it, a pod that sets
`GREL_LOG_LEVEL=DEBUG` logs at `INFO`, and a pod that sets
`GREL_LOCK_CART_LEASE_DURATION=120` keeps the 60 second default.

`GREL_ENVIRONMENT` is the exception, along with the flag itself. Neither
fills a component field, and a safety check behind an opt-in flag would be
off in the pods that need it most.

Provider variables are not gated. `REDIS_URL`, `VALKEY_URL`, `POSTGRES_URL`
and `SQLITE_PATH` are read out of the box, because those names belong to your
environment rather than to grelmicro, and a missing one fails at startup
naming the variable it wanted. So a pod without the flag still connects to its
backend and still ignores every `GREL_*` knob, which is what makes the flag
easy to forget.

Set it in the image, not in the manifest. A manifest gets copied from one
environment to the next, and one copy will leave it out.

### When the flag is missing

grelmicro names the ignored variable on two channels. It raises a
`GrelmicroConfigWarning`, which fails a test suite running `-W error`. It
also logs a `WARNING` on the `grelmicro` logger, so on the default backend
the report is a normal record in the log stream:

```json
{"variable":"GREL_LOG_LEVEL","time":"2026-08-06T13:44:04.156560+00:00","level":"WARNING","msg":"GREL_LOG_LEVEL is set but was not applied: environment-driven configuration is opt-in. Set GREL_ENV_LOAD=1 to enable it, or pass the value directly.","logger":"grelmicro"}
```

The variable name is in the `variable` field, so an alert can match on the
field instead of the message text. Each name is reported once per process,
at startup, and only for variables a component actually declares.

Passing `env_load=False` to a component is a decision, so it stays quiet.

## Logs

A container needs no log configuration. The default `AUTO` format writes
`TEXT` to a terminal and `JSON` everywhere else, so the same image gives a
readable stream in development and a parseable one in a pod. Uvicorn's own
lines take the same format.

Set the level per environment, once the flag above is on:

```bash
GREL_LOG_LEVEL=INFO
```

Kubernetes polls the probe endpoints every few seconds for the life of the
pod, and the access log reports each one. Attach `ProbeFilter` to drop them:

```python
--8<-- "log/probes.py"
```

[Logging](logging/index.md) covers the formats, the backends and the other filters.

## Health probes

`health_router()` serves `/livez`, `/readyz` and `/healthz`. Point the
liveness probe at `/livez`, which stays `200` while the process is alive, and
the readiness probe at `/readyz`, which turns `503` as soon as a critical
check fails and takes the pod out of the Service.

Keep the readiness period short and the liveness period long. Readiness
reacts to a lost backend, liveness only to a process that is gone. Use a
`startupProbe` on `/livez` instead of a long `initialDelaySeconds`, so a slow
first connection never counts as a liveness failure.
[Health checks](health.md) covers the component and the endpoint behavior.

## Shutdown

Kubernetes sends `SIGTERM` and waits `terminationGracePeriodSeconds` before
`SIGKILL`. Keep `Tasks(shutdown_timeout=...)` at or below that window. Both
default to 30 seconds, so the defaults already line up.

Removing the pod from the Service and sending `SIGTERM` happen in parallel, so
a request in flight can arrive after the server has started to stop. A short
`preStop` sleep holds the signal back until the endpoint change has spread.

Draining matters for the locks. A task that finishes its iteration releases
its lock through `async with`, and `LeaderElection` releases the leadership
lock so a standby takes over at once. A task force-cancelled at the end of
the window leaves its lease to expire on the backend instead.
[Graceful shutdown](architecture/graceful-shutdown.md) has the full contract.

## Replicas

`Lock`, `TaskLock` and `LeaderElection` coordinate through the backend, so
every replica has to point at the same one. Give them a provider and let the
environment carry the connection:

```bash
REDIS_URL=redis+sentinel://sentinel-0:26379,sentinel-1:26379/mymaster/0
REDIS_PASSWORD=...
REDIS_SENTINEL_PASSWORD=...
```

The composition root then holds no connection code at all:

```python
--8<-- "deployment/composition_root.py"
```

Pods sharing a namespace with another grelmicro application need a `prefix`
so the two do not collide on key or lease names. [Providers](providers/index.md)
covers the URL forms and the backend options.

## Reconfigure without a restart

Mount a ConfigMap or a Secret and grelmicro re-resolves the live components
when the file changes, with no rollout. See
[Live reconfiguration](configuration/reconfigure-from-configmap.md).

## A complete manifest

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cart
spec:
  replicas: 3
  selector:
    matchLabels:
      app.kubernetes.io/name: cart
  template:
    metadata:
      labels:
        app.kubernetes.io/name: cart
    spec:
      terminationGracePeriodSeconds: 30
      securityContext:
        runAsNonRoot: true
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: cart
          image: registry.example.com/cart:1.4.0
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              memory: 512Mi
          ports:
            - containerPort: 8000
          env:
            - name: GREL_ENVIRONMENT
              value: production
            - name: GREL_LOG_LEVEL
              value: INFO
            - name: GREL_LOCK_CART_LEASE_DURATION
              value: "120"
            - name: REDIS_URL
              value: redis://redis:6379/0
            - name: REDIS_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: cart-redis
                  key: password
          startupProbe:
            httpGet:
              path: /livez
              port: 8000
            periodSeconds: 2
            failureThreshold: 30
          livenessProbe:
            httpGet:
              path: /livez
              port: 8000
            periodSeconds: 20
          readinessProbe:
            httpGet:
              path: /readyz
              port: 8000
            periodSeconds: 5
          lifecycle:
            preStop:
              sleep:
                seconds: 5
```

`GREL_ENV_LOAD=1` is missing here on purpose: the image sets it, so no
manifest can forget it. Put it in the `env` block only when you cannot
change the image, and then put it in every copy of the manifest.

## Checklist

- [ ] `GREL_ENVIRONMENT` is set in every deployed environment.
- [ ] `micro.check_backends()` is asserted in a test.
- [ ] `GREL_ENV_LOAD=1` is in the image.
- [ ] The startup logs carry no `variable` field.
- [ ] `GREL_LOG_LEVEL` is set per environment, and the format is left to `AUTO`.
- [ ] `ProbeFilter` is attached to `uvicorn.access`.
- [ ] `/livez` and `/readyz` are wired, with readiness the faster of the two.
- [ ] `terminationGracePeriodSeconds` is at or above `Tasks(shutdown_timeout=...)`.
- [ ] Every replica points at the same backend, through a provider.
