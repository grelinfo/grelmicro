# Deployment

What a container image and a Kubernetes manifest need before they run a
grelmicro application in production.

## Turn on environment configuration

Set `GREL_ENV_LOAD=1` in the image:

```dockerfile
ENV GREL_ENV_LOAD=1
```

Every `GREL_*` variable is read only when this flag is truthy (`1`, `true`,
`yes`, `on`). Without it, a pod that sets `GREL_LOG_LEVEL=DEBUG` logs at
`INFO`, and a pod that sets `GREL_LOCK_CART_LEASE_DURATION=120` keeps the
60 second default.

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

[Logging](logging.md) covers the formats, the backends and the other filters.

## Health probes

`health_router()` serves `/livez`, `/readyz` and `/healthz`. Point the
liveness probe at `/livez`, which stays `200` while the process is alive, and
the readiness probe at `/readyz`, which turns `503` as soon as a critical
check fails and takes the pod out of the Service.

Keep the readiness period short and the liveness period long. Readiness
reacts to a lost backend, liveness only to a process that is gone. Use a
`startupProbe` on `/livez` instead of a long `initialDelaySeconds`, so a slow
first connection never counts as a liveness failure.
[Health checks](health.md) covers the registry and the endpoint behavior.

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
so the two do not collide on key or lease names. [Providers](providers.md)
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

- [ ] `GREL_ENV_LOAD=1` is in the image.
- [ ] The startup logs carry no `variable` field.
- [ ] `GREL_LOG_LEVEL` is set per environment, and the format is left to `AUTO`.
- [ ] `ProbeFilter` is attached to `uvicorn.access`.
- [ ] `/livez` and `/readyz` are wired, with readiness the faster of the two.
- [ ] `terminationGracePeriodSeconds` is at or above `Tasks(shutdown_timeout=...)`.
- [ ] Every replica points at the same backend, through a provider.
