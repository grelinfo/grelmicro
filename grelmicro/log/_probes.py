"""Health probe access log filter.

Stdlib :class:`logging.Filter` that drops uvicorn access lines for the
health endpoints. See the logging user guide for semantics and examples.
"""

from logging import Filter, LogRecord, getLogger
from typing import Annotated

from typing_extensions import Doc

DEFAULT_PROBE_PATHS: tuple[str, ...] = ("/livez", "/readyz", "/healthz")
"""Endpoint suffixes `health_router()` serves."""

_UVICORN_ACCESS_LOGGER = "uvicorn.access"
_ACCESS_RECORD_ARGS = 5
_PATH_INDEX = 2
_STATUS_INDEX = 4
_FIRST_ERROR_STATUS = 400


class ProbeFilter(Filter):
    """Drop successful health probe lines from an access log.

    Kubernetes polls `/livez`, `/readyz` and `/healthz` every few seconds
    for the life of the pod, and an access logger reports each one, so the
    probes crowd out everything else.

    A probe that fails is kept. A failing readiness check is often the only
    thing in the log that says the kubelet asked and was refused, so it is
    the line worth reading.

    Paths are matched by suffix, so a router mounted under a prefix is
    covered without configuration.
    """

    def __init__(
        self,
        paths: Annotated[
            "tuple[str, ...] | None",
            Doc(
                """
                Endpoint suffixes to drop. Defaults to the three
                `health_router()` serves. Pass your own to cover extra
                probe endpoints, such as a metrics scrape.
                """,
            ),
        ] = None,
    ) -> None:
        """Initialize the filter."""
        super().__init__()
        self._paths = tuple(paths) if paths is not None else DEFAULT_PROBE_PATHS

    def filter(self, record: LogRecord) -> bool:
        """Return False for a probe request that succeeded."""
        args = record.args
        if not isinstance(args, tuple) or len(args) != _ACCESS_RECORD_ARGS:
            return True
        path = str(args[_PATH_INDEX]).split("?", 1)[0]
        if not path.endswith(self._paths):
            return True
        try:
            status = int(str(args[_STATUS_INDEX]))
        except ValueError:
            return True
        return status >= _FIRST_ERROR_STATUS


def silence_probe_access_logs(
    paths: Annotated[
        "tuple[str, ...] | None",
        Doc("Endpoint suffixes to drop. Defaults to the health endpoints."),
    ] = None,
) -> ProbeFilter:
    """Stop successful health probes from reaching the access log.

    Attaches a `ProbeFilter` to the `uvicorn.access` logger and returns it,
    so it can be removed again:

    ```python
    from grelmicro.log import silence_probe_access_logs

    silence_probe_access_logs()
    ```

    Call it once at startup, after logging is configured. Failing probes
    still appear, so a readiness check that starts refusing traffic is not
    hidden along with the noise.
    """
    probe_filter = ProbeFilter(paths)
    getLogger(_UVICORN_ACCESS_LOGGER).addFilter(probe_filter)
    return probe_filter
