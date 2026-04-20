"""Tests for the DuplicateFilter."""

import logging
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from grelmicro.logging import DuplicateFilter, configure_logging
from tests.logging.conftest import BACKENDS


def _make_record(
    name: str = "grelmicro.test",
    level: int = logging.WARNING,
    msg: str = "example",
    args: tuple[object, ...] = (),
) -> logging.LogRecord:
    """Build a LogRecord for filter testing."""
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


@pytest.fixture
def dedup_logger() -> Generator[logging.Logger, None, None]:
    """Yield a named logger and strip any attached filters after."""
    logger = logging.getLogger("grelmicro.test.dedup_integration")
    logger.setLevel(logging.WARNING)
    try:
        yield logger
    finally:
        for flt in list(logger.filters):
            logger.removeFilter(flt)


def test_allows_first_n_then_drops() -> None:
    """First ``allowed_repetitions`` records pass, subsequent are dropped."""
    filt = DuplicateFilter(allowed_repetitions=3)
    record = _make_record()

    results = [filt.filter(record) for _ in range(5)]

    assert results == [True, True, True, False, False]


def test_default_allowed_repetitions_is_five() -> None:
    """Default allowance matches Logback's DuplicateMessageFilter."""
    expected = 5
    filt = DuplicateFilter()
    record = _make_record()

    passed = sum(1 for _ in range(10) if filt.filter(record))

    assert passed == expected


def test_distinct_keys_tracked_independently() -> None:
    """Different keys do not share repetition counters."""
    filt = DuplicateFilter(allowed_repetitions=2)
    record_a = _make_record(msg="a")
    record_b = _make_record(msg="b")

    results = [
        filt.filter(record_a),
        filt.filter(record_a),
        filt.filter(record_a),
        filt.filter(record_b),
        filt.filter(record_b),
        filt.filter(record_b),
    ]

    assert results == [True, True, False, True, True, False]


def test_different_rendered_messages_dedup_independently() -> None:
    """Same template, different args -> distinct rendered -> own counters."""
    filt = DuplicateFilter(allowed_repetitions=1)
    template = "check %s failed"

    results = [
        filt.filter(_make_record(msg=template, args=("db",))),
        filt.filter(_make_record(msg=template, args=("db",))),
        filt.filter(_make_record(msg=template, args=("redis",))),
        filt.filter(_make_record(msg=template, args=("redis",))),
    ]

    assert results == [True, False, True, False]


def test_fstring_style_dedup() -> None:
    """f-string callers (already-rendered msg, no args) dedup correctly."""
    filt = DuplicateFilter(allowed_repetitions=2)
    record = _make_record(msg="check db failed", args=())

    results = [filt.filter(record) for _ in range(4)]

    assert results == [True, True, False, False]


def test_percent_and_fstring_with_same_output_share_counter() -> None:
    """Two call styles producing the same rendered string collapse."""
    filt = DuplicateFilter(allowed_repetitions=1)
    percent_style = _make_record(msg="check %s failed", args=("db",))
    fstring_style = _make_record(msg="check db failed", args=())

    first = filt.filter(percent_style)
    second = filt.filter(fstring_style)

    assert first
    assert not second


def test_different_levels_do_not_collapse() -> None:
    """Same template at different levels is considered distinct."""
    filt = DuplicateFilter(allowed_repetitions=1)
    template = "boom"
    warning_record = _make_record(msg=template, level=logging.WARNING)
    error_record = _make_record(msg=template, level=logging.ERROR)

    first_warning = filt.filter(warning_record)
    first_error = filt.filter(error_record)
    second_warning = filt.filter(warning_record)

    assert first_warning
    assert first_error
    assert not second_warning


def test_different_loggers_do_not_collapse() -> None:
    """Same template under different logger names is distinct."""
    filt = DuplicateFilter(allowed_repetitions=1)
    record_a = _make_record(name="a", msg="same")
    record_b = _make_record(name="b", msg="same")

    first_a = filt.filter(record_a)
    first_b = filt.filter(record_b)
    second_a = filt.filter(record_a)

    assert first_a
    assert first_b
    assert not second_a


def test_lru_evicts_least_recently_seen() -> None:
    """Cache bound evicts the oldest untouched key when full."""
    filt = DuplicateFilter(allowed_repetitions=1, cache_size=2)
    record_a = _make_record(msg="a")
    record_b = _make_record(msg="b")
    record_c = _make_record(msg="c")

    results = [
        filt.filter(record_a),
        filt.filter(record_b),
        filt.filter(record_c),
        filt.filter(record_a),
    ]

    assert results == [True, True, True, True]


def test_lru_keeps_hot_keys() -> None:
    """Recently-hit keys stay in the cache despite size pressure."""
    filt = DuplicateFilter(allowed_repetitions=1, cache_size=2)
    hot = _make_record(msg="hot")
    cold_x = _make_record(msg="x")
    cold_y = _make_record(msg="y")

    results = [
        filt.filter(hot),
        filt.filter(cold_x),
        filt.filter(hot),
        filt.filter(cold_y),
        filt.filter(hot),
    ]

    assert results == [True, True, False, True, False]


def test_key_override() -> None:
    """Custom key function replaces the default fingerprint."""
    filt = DuplicateFilter(
        allowed_repetitions=1,
        key=lambda record: record.levelno,
    )
    record_one = _make_record(msg="one")
    record_two = _make_record(msg="two")

    first = filt.filter(record_one)
    second = filt.filter(record_two)

    assert first
    assert not second


def test_thread_safe_counting() -> None:
    """Concurrent calls produce exactly ``allowed_repetitions`` passes."""
    allowed = 50
    filt = DuplicateFilter(allowed_repetitions=allowed, cache_size=1)

    def hit(_: int) -> bool:
        return filt.filter(_make_record())

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(hit, range(1000)))

    assert sum(results) == allowed


def test_integration_with_logger(
    caplog: pytest.LogCaptureFixture,
    dedup_logger: logging.Logger,
) -> None:
    """Attaching the filter to a logger suppresses repeated records."""
    allowed = 2
    filt = DuplicateFilter(allowed_repetitions=allowed)
    dedup_logger.addFilter(filt)

    with caplog.at_level(logging.WARNING, logger=dedup_logger.name):
        for _ in range(5):
            dedup_logger.warning("flooded")

    records = [r for r in caplog.records if r.name == dedup_logger.name]
    assert len(records) == allowed


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.usefixtures("reset_backend")
def test_works_under_every_backend(
    backend: str,
    caplog: pytest.LogCaptureFixture,
    dedup_logger: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filter behavior is identical across stdlib/loguru/structlog."""
    allowed = 2
    monkeypatch.setenv("LOG_BACKEND", backend)
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    configure_logging()
    logging.getLogger().addHandler(caplog.handler)
    dedup_logger.addFilter(DuplicateFilter(allowed_repetitions=allowed))

    with caplog.at_level(logging.WARNING, logger=dedup_logger.name):
        for _ in range(5):
            dedup_logger.warning("flooded")

    records = [r for r in caplog.records if r.name == dedup_logger.name]
    assert len(records) == allowed


def test_malformed_format_does_not_crash() -> None:
    """A format/args mismatch falls back to ``record.msg`` without raising."""
    filt = DuplicateFilter(allowed_repetitions=2)
    bad = _make_record(msg="needs %d", args=("not an int",))

    results = [filt.filter(bad) for _ in range(4)]

    assert results == [True, True, False, False]


@pytest.mark.parametrize(
    ("allowed", "cache"),
    [(0, 10), (-1, 10), (10, 0), (10, -5)],
)
def test_invalid_config_rejected(allowed: int, cache: int) -> None:
    """Non-positive config values raise a pydantic ValidationError."""
    with pytest.raises(ValidationError, match="greater than 0"):
        DuplicateFilter(allowed_repetitions=allowed, cache_size=cache)


def test_config_exposed() -> None:
    """Config is readable via the ``config`` property."""
    allowed = 7
    cache = 42

    filt = DuplicateFilter(allowed_repetitions=allowed, cache_size=cache)

    assert filt.config.allowed_repetitions == allowed
    assert filt.config.cache_size == cache
