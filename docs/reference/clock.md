# Clock

- **Start here**: [Virtual clock](../architecture/testing.md#virtual-clock)
- **Common recipes**: install a `VirtualClock` and call `clock.advance(seconds)` to drive time-dependent primitives without real waiting. `monotonic()` and `sleep()` are the seam primitives read through.

::: grelmicro.clock
    options:
      members:
        - VirtualClock
        - RealClock
        - ClockBackend
        - monotonic
        - sleep
