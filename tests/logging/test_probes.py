"""Tests for the health probe access log filter."""

import logging

from grelmicro.log import ProbeFilter, silence_probe_access_logs


def _access_record(path: str, status: int = 200) -> logging.LogRecord:
    """Build a record shaped like uvicorn's access log."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:54321", "GET", path, "1.1", status),
        exc_info=None,
    )


def test_successful_probe_is_dropped() -> None:
    """A passing probe carries no information and is dropped."""
    assert ProbeFilter().filter(_access_record("/healthz")) is False


def test_failing_probe_is_kept() -> None:
    """A failing probe is the line worth reading."""
    assert ProbeFilter().filter(_access_record("/readyz", 503)) is True


def test_application_request_is_kept() -> None:
    """A request that is not a probe passes through."""
    assert ProbeFilter().filter(_access_record("/orders")) is True


def test_prefixed_probe_is_dropped() -> None:
    """Suffix matching covers `health_router(prefix=...)` with no configuration."""
    assert ProbeFilter().filter(_access_record("/api/v1/livez")) is False


def test_query_string_is_ignored() -> None:
    """`/healthz?exclude=redis` is still a probe."""
    assert ProbeFilter().filter(_access_record("/healthz?exclude=x")) is False


def test_custom_paths_replace_the_defaults() -> None:
    """`paths=` covers extra endpoints such as a metrics scrape."""
    probe_filter = ProbeFilter(paths=("/metrics",))

    assert probe_filter.filter(_access_record("/metrics")) is False
    assert probe_filter.filter(_access_record("/healthz")) is True


def test_non_access_record_passes_through() -> None:
    """A record that is not an access line is never inspected for a path."""
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Application startup complete.",
        args=None,
        exc_info=None,
    )

    assert ProbeFilter().filter(record) is True


def test_unparsable_status_passes_through() -> None:
    """A record that only looks like an access line is left alone."""
    record = _access_record("/healthz")
    record.args = ("a", "b", "/healthz", "d", "not-a-status")

    assert ProbeFilter().filter(record) is True


def test_silence_attaches_to_the_uvicorn_access_logger() -> None:
    """The helper wires the filter and hands it back for removal."""
    logger = logging.getLogger("uvicorn.access")
    probe_filter = silence_probe_access_logs()
    try:
        assert probe_filter in logger.filters
    finally:
        logger.removeFilter(probe_filter)

    assert probe_filter not in logger.filters
