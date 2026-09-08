"""How long a test waits for a container to announce itself.

The unit tier runs under `-n auto`, so a full run saturates the machine.
A Redis Sentinel that starts in five seconds on an idle host can take
far longer to write its first log line while twelve workers compete for
the same cores, and the wait then expires on a container that was only
slow.

The bound only bites when a container is genuinely broken, so a generous
one costs nothing on a healthy run and a tight one turns machine load
into a failure that reads like a bug in the code under test.
"""

from __future__ import annotations

CONTAINER_LOG_TIMEOUT = 90
"""Seconds a test waits for a container's startup line, under full load."""

CONTAINER_TEST_TIMEOUT = 240
"""Seconds `pytest.mark.timeout` allows a test that starts two containers.

It has to stay above the sum of the waits below it. A marker that fires
at the same moment as the last wait reports only that the test ran long,
where the wait names the container that never came up.
"""
