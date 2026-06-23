# Reconfigure from a ConfigMap

[`reconfigure(new_config)`](../architecture/reconfigure.md) swaps a live component's configuration without rebuilding it. The `ExternalConfig` component automates the whole loop: it reads a mounted `ConfigMap`, `Secret`, `.env`, or JSON file, and reapplies every changed value to the live components, with no per-component wiring.

## The ConfigMap

Keys are the same `GREL_...` names the [Environmental path](../config.md) reads from the environment:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: grelmicro-config
data:
  GREL_LOCK_LEDGER_LEASE_DURATION: "30"
  GREL_LOCK_LEDGER_RETRY_INTERVAL: "0.5"
  GREL_RATELIMITER_API_LIMIT: "200"
```

Mount it as a volume:

```yaml
volumeMounts:
  - name: config
    mountPath: /etc/grelmicro
volumes:
  - name: config
    configMap:
      name: grelmicro-config
```

## The component

Add `ExternalConfig` to the app and point it at the mount:

```python
from grelmicro import ExternalConfig, Grelmicro
from grelmicro.coordination import Coordination, Lock
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import RateLimiter, RateLimiters

redis = RedisProvider("redis://localhost:6379/0")

ledger_lock = Lock("ledger")
api_limiter = RateLimiter.sliding_window("api", limit=100, window=60)

micro = Grelmicro(uses=[
    Coordination(redis),
    RateLimiters(redis.ratelimiter()),
    ExternalConfig("/etc/grelmicro"),
])
```

Editing the `ConfigMap` now updates the lock's lease and the limiter's quota on the next poll, without a restart. A held lease and in-flight acquires survive the swap. See [Live reconfiguration](../architecture/reconfigure.md) for the contract.

## What reloads, and what does not

- Every named pattern built programmatically or from the environment is addressable by its `GREL_{PATTERN}_{NAME}_` keys, including patterns that do not read env vars at construction (`GREL_RATELIMITER_{NAME}_`, `GREL_CIRCUITBREAKER_{NAME}_`).
- Instances built with `from_config` stay static. The declarative path hands ownership of the config tree to your settings layer, so `ExternalConfig` leaves it alone.
- Identity fields (a lock `worker`) and keys that match no config field are ignored.
- A value that fails validation is logged (field names only, never values) and skipped. The component keeps the last good configuration.

## Polling and tests

`ExternalConfig` polls the source (default every 10 seconds) and skips the apply pass when nothing changed. Call `reload()` for a deterministic pass, which is what tests want:

```python
async def test_lease_reload(tmp_path):
    (tmp_path / "GREL_LOCK_LEDGER_LEASE_DURATION").write_text("30")
    external = ExternalConfig(tmp_path)
    ledger_lock = Lock("ledger")

    async with Grelmicro(uses=[external]):
        await external.reload()
        assert ledger_lock.config.lease_duration == 30
```

## File formats

A mounted `ConfigMap` or `Secret` is a directory, one file per key. The file name is the `GREL_...` key and the file content is its value. This is the default mounted shape, so point `ExternalConfig` at the mount directory.

A single file source is read by its extension instead:

- `.json`, `.yaml`, `.yml`, and `.toml` files hold a mapping document.
- Any other file is read as `.env` lines (`KEY=VALUE`, blanks and `#` comments ignored).

A mapping document is either flat or nested. A flat mapping uses the `GREL_...` keys directly:

```yaml
GREL_LOCK_LEDGER_LEASE_DURATION: 30
GREL_RATELIMITER_API_LIMIT: 200
```

A nested mapping joins its segments with `_` and uppercases them. The mapping below reads as `GREL_LOCK_LEDGER_LEASE_DURATION=30` and `GREL_RATELIMITER_API_LIMIT=200`:

```yaml
grel:
  lock:
    ledger:
      lease_duration: 30
  ratelimiter:
    api:
      limit: 200
```

Values are stringified. A bool becomes `true` or `false`. A number becomes its digits. A document that is not a mapping raises `ValueError`.

YAML needs PyYAML. Install it with the `yaml` extra:

```bash
pip install "grelmicro[yaml]"
```

## Secrets

A mounted `Secret` is a directory of files too, so the same adapter reads it. Pass it via `secrets=` to keep the two sources separate:

```python
ExternalConfig("/etc/grelmicro", secrets="/etc/grelmicro-secrets")
```

## Manual control

For a trigger the component does not cover (a `SIGHUP` handler, an admin endpoint), call `reload()` from your own code, or call `reconfigure(new_config)` directly on one component for full manual control. The [Live reconfiguration](../architecture/reconfigure.md) page documents the per-component contract.
