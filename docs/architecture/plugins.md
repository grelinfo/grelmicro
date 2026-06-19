# Plugins

grelmicro discovers Providers and Adapters through entry-point groups. A
third-party package registers under these groups and resolves by short name,
so grelmicro never has to depend on the vendor. First-party Providers and
Adapters use the very same path: there is no special case.

## The two groups

| Group | Maps | Example |
|---|---|---|
| `grelmicro.providers` | a vendor short name to a `Provider` class | `redis = "grelmicro.providers.redis:RedisProvider"` |
| `grelmicro.{kind}.adapters` | a short name to an Adapter class for one component kind | `redis = "grelmicro.coordination.redis:RedisLockAdapter"` |

A Provider covers the vendor axis: one Provider per vendor. An Adapter covers
the algorithm axis within a kind, so several adapters can share one Provider
(a Redis lock and a Redis cache both run on `RedisProvider`).

The component kinds are `coordination`, `coordination.election`, `coordination.schedule`, `cache`,
`ratelimiter`, and `circuitbreaker`.

## Publish a third-party adapter

Say you ship `grelmicro-mongo` with a Mongo-backed lock. Write the Provider
and the Adapter, then declare them in your package's `pyproject.toml`:

```toml
[project.entry-points."grelmicro.providers"]
mongo = "grelmicro_mongo:MongoProvider"

[project.entry-points."grelmicro.coordination.adapters"]
mongo = "grelmicro_mongo:MongoLockAdapter"
```

Once your package is installed alongside grelmicro, the name `mongo` resolves
through the same loader grelmicro uses for its own backends. Users wire it up
exactly like a first-party backend:

```python
from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro_mongo import MongoProvider

mongo = MongoProvider("mongodb://localhost:27017")
micro = Grelmicro(uses=[Coordination(mongo)])
```

A worked skeleton lives in
[`examples/third-party-adapter/`](https://github.com/grelinfo/grelmicro/tree/main/examples/third-party-adapter).

## How resolution works

Listing entry points never imports the target module. The module loads only
when a name is resolved, so installing many vendor packages stays cheap. An
unknown name raises `ProviderNotRegisteredError` or `AdapterNotRegisteredError`
with the requested name and the names that are installed:

```text
No coordination adapter registered as 'mongo' in the
'grelmicro.coordination.adapters' entry-point group. Available: kubernetes,
memory, postgres, redis, sqlite. Install the package that ships it, or check
the name.
```
