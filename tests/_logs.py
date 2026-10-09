"""Shared helpers for tests that read captured log records.

`caplog` captures every logger that writes at or above its level, so a test
reads the records of the logger it is about:

```python
from tests._logs import records_of
```
"""

import logging

import pytest


def records_of(
    caplog: pytest.LogCaptureFixture, name: str
) -> list[logging.LogRecord]:
    """Return the captured records of the `name` logger and its children, in order."""
    prefix = f"{name}."
    return [
        record
        for record in caplog.records
        if record.name == name or record.name.startswith(prefix)
    ]
