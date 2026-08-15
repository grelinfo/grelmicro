"""Report models returned by `Grelmicro.describe()`.

A public home for the types `describe()` hands back, so a caller can annotate
a report without reaching into a private module:

```python
from grelmicro import Grelmicro
from grelmicro.describe import AppReport


def assert_wired(micro: Grelmicro) -> AppReport:
    report = micro.describe()
    assert report.ok
    return report
```

The models live in a private module and are re-exported here rather than
from `grelmicro` itself, so `import grelmicro` does not pay for them. The
same reason `Grelmicro.describe` imports them lazily.
"""

from grelmicro._describe import (
    AppReport,
    CheckReport,
    CheckStatus,
    ComponentReport,
    ProviderReport,
)

__all__ = [
    "AppReport",
    "CheckReport",
    "CheckStatus",
    "ComponentReport",
    "ProviderReport",
]
