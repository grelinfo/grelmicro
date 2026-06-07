# Third-party adapter skeleton

A minimal example of publishing a grelmicro backend from an external package,
here a fake `grelmicro-mongo`. See the [Plugins](https://grelinfo.github.io/grelmicro/architecture/plugins/)
docs for the full contract.

## What makes it discoverable

`pyproject.toml` declares the entry points. Nothing else is needed:

```toml
[project.entry-points."grelmicro.providers"]
mongo = "grelmicro_mongo:MongoProvider"

[project.entry-points."grelmicro.coordination.adapters"]
mongo = "grelmicro_mongo:MongoLockAdapter"
```

Once installed alongside grelmicro, the short name `mongo` resolves through the
same loader grelmicro uses for its own backends.

## Use it

```python
from grelmicro import Grelmicro
from grelmicro.coordination import Coordination
from grelmicro_mongo import MongoProvider

mongo = MongoProvider("mongodb://localhost:27017")
micro = Grelmicro(uses=[Coordination(mongo)])
```

The lock methods in `grelmicro_mongo.py` are stubs. Implement `acquire`,
`release`, `locked`, and `owned` with real MongoDB calls to ship a working
backend.
