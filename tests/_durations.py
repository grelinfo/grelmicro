"""Shared helpers for duration tests.

A monotonic clock in whole nanoseconds that a test moves, the values a
duration refuses, and a strategy for any duration a setting takes:

```python
from tests._durations import DURATIONS, FLOATS, NanosecondClock
```
"""

from datetime import timedelta

import pytest
from hypothesis import strategies as st

from grelmicro._duration import MICROSECOND, nanoseconds

START_NS = 1_000_000_000_000
"""Where a `NanosecondClock` starts."""

NANOSECOND = 1
"""One nanosecond, the step a `NanosecondClock` moves by."""

FLOATS = [
    pytest.param(12.5, id="float"),
    pytest.param(12.0, id="whole-float"),
    pytest.param(True, id="bool"),
]
"""A float or a bool, which a duration refuses."""

DURATIONS = st.timedeltas(min_value=MICROSECOND, max_value=timedelta(days=2))
"""Any duration a setting takes, to the microsecond."""


class NanosecondClock:
    """A monotonic clock in whole nanoseconds that the test moves."""

    def __init__(self) -> None:
        """Start at a fixed point."""
        self.now = START_NS

    def __call__(self) -> int:
        """Return the current time."""
        return self.now

    def advance(self, duration: timedelta, extra_ns: int = 0) -> None:
        """Move forward by `duration`, plus `extra_ns` nanoseconds."""
        self.now += nanoseconds(duration) + extra_ns
