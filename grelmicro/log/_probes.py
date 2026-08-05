"""Health probe access log filter.

Stdlib :class:`logging.Filter` that drops uvicorn access lines for the
health endpoints. See the logging user guide for semantics and examples.
"""

from logging import Filter, LogRecord
from typing import Annotated

from typing_extensions import Doc

_DEFAULT_PROBE_PATHS = ("/livez", "/readyz", "/healthz")
_ACCESS_RECORD_ARGS = 5
_PATH_INDEX = 2
_STATUS_INDEX = 4
_FIRST_ERROR_STATUS = 400


class ProbeFilter(Filter):
    """Drop successful health probe lines from an access log.

    Kubernetes polls `/livez`, `/readyz` and `/healthz` every few seconds
    for the life of the pod, and an access logger reports each one, so the
    probes crowd out everything else.

    Attach it to the access logger:

    ```python
    logging.getLogger("uvicorn.access").addFilter(ProbeFilter())
    ```

    A probe that fails is kept. A failing readiness check is often the only
    thing in the log that says the kubelet asked and was refused, so it is
    the line worth reading.

    Paths are matched by suffix, so a router mounted under a prefix is
    covered without configuration.
    """

    def __init__(
        self,
        paths: Annotated[
            tuple[str, ...] | None,
            Doc(
                """
                Endpoint suffixes to drop, replacing the default
                `("/livez", "/readyz", "/healthz")`. Pass your own to cover
                other polled endpoints, such as a metrics scrape.
                """,
            ),
        ] = None,
    ) -> None:
        """Initialize the filter."""
        super().__init__()
        self._paths = (
            tuple(paths) if paths is not None else _DEFAULT_PROBE_PATHS
        )

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
