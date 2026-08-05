"""Tests for the health probe access log filter."""

import logging

from grelmicro.log import ProbeFilter


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


def test_attaches_to_a_logger_like_the_other_filters() -> None:
    """It is a plain `logging.Filter`, attached the usual way."""
    logger = logging.getLogger("uvicorn.access")
    probe_filter = ProbeFilter()
    logger.addFilter(probe_filter)
    try:
        # `Logger.filter` returns the record itself when it passes.
        assert not logger.filter(_access_record("/healthz"))
        assert logger.filter(_access_record("/orders"))
    finally:
        logger.removeFilter(probe_filter)
