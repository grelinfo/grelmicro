"""Clock abstraction for time-dependent primitives.

`monotonic()` and `sleep()` resolve the active clock. Production code uses the
real clock with no setup. Tests install a `VirtualClock` to drive time-dependent
behavior without real waiting.
"""

from grelmicro.clock._protocol import ClockBackend
from grelmicro.clock._real import RealClock
from grelmicro.clock._seam import monotonic, sleep
from grelmicro.clock._virtual import VirtualClock

__all__ = [
    "ClockBackend",
    "RealClock",
    "VirtualClock",
    "monotonic",
    "sleep",
]
