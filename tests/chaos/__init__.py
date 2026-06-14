"""Chaos / fault-injection tests against real infrastructure.

Each module brings up a real backend container, drives traffic, then
injects a real fault (docker pause/unpause or stop/start) mid-test and
asserts the documented graceful-degradation behavior. The tests are
marked ``integration`` and ``slow`` so they stay out of the default
unit run and only fire where Docker is available.
"""
