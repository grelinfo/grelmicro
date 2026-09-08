"""How long a test waits for a container to announce itself.

The integration tier runs under `-n auto`, so around twenty container
modules start at once. A Redis Sentinel that comes up in five seconds on
an idle host can take far longer to write its first log line while the
workers compete for the same cores, and a tight wait then expires on a
container that was only slow.

The bound only bites when a container is genuinely broken, so a generous
one costs nothing on a healthy run, where the wait returns as soon as
the line appears.

A test that starts containers marks its own timeout `func_only=True`, so
the marker covers the body and not the fixture that waits here. Without
that, the two bounds are coupled: a marker that fires while a wait is
still running reports only that the test ran long, where the wait names
the container that never came up.
"""

from __future__ import annotations

CONTAINER_LOG_TIMEOUT = 90
"""Seconds a test waits for a container's startup line, under full load."""
