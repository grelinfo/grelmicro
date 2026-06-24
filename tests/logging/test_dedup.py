"""Tests for the DuplicateFilter."""

import logging
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor

import pytest
from freezegun import freeze_time

from grelmicro.log import (
    DuplicateFilter,
    LogSettingsValidationError,
    configure,
)
from grelmicro.log._dedup import _key_by_rendered
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
    """Default allowance is five records per key."""
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


def test_rendered_mode_distinguishes_args() -> None:
    """Rendered mode: same template, different args -> own counters."""
    filt = DuplicateFilter(allowed_repetitions=1, key_mode="rendered")
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


def test_rendered_mode_percent_and_fstring_share_counter() -> None:
    """Rendered mode: two styles producing the same text collapse."""
    filt = DuplicateFilter(allowed_repetitions=1, key_mode="rendered")
    percent_style = _make_record(msg="check %s failed", args=("db",))
    fstring_style = _make_record(msg="check db failed", args=())

    first = filt.filter(percent_style)
    second = filt.filter(fstring_style)

    assert first
    assert not second


def test_default_key_mode_is_template() -> None:
    """Default key_mode is ``template`` (faster than ``rendered``)."""
    filt = DuplicateFilter()

    assert filt.config.key_mode == "template"


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
    # cache_size=1 keeps a single shared counter so this exercises the
    # lock-protected increment path rather than LRU eviction.
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
    monkeypatch.setenv("GREL_LOG_BACKEND", backend)
    monkeypatch.setenv("GREL_LOG_LEVEL", "WARNING")
    configure()
    # configure() under GREL_LOG_BACKEND=stdlib clears root handlers,
    # removing caplog's capture handler. Re-attach it so records emitted
    # through dedup_logger still reach caplog.records via propagation.
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


def test_rendered_key_fallback_on_render_failure() -> None:
    """``_key_by_rendered`` returns ``str(record.msg)`` when rendering raises."""
    bad = _make_record(msg="needs %d", args=("not an int",))

    key = _key_by_rendered(bad)

    assert key == ("grelmicro.test", logging.WARNING, "needs %d")


def test_key_mode_template_collapses_parameterized_calls() -> None:
    """``key_mode='template'`` dedups `%`-style calls across args."""
    filt = DuplicateFilter(allowed_repetitions=1, key_mode="template")
    template = "check %s failed"

    results = [
        filt.filter(_make_record(msg=template, args=("db",))),
        filt.filter(_make_record(msg=template, args=("redis",))),
        filt.filter(_make_record(msg=template, args=("kafka",))),
    ]

    assert results == [True, False, False]


def test_key_mode_template_ignores_rendering_errors() -> None:
    """Template mode does not call ``getMessage`` so format bugs are moot."""
    filt = DuplicateFilter(allowed_repetitions=1, key_mode="template")
    bad = _make_record(msg="needs %d", args=("not an int",))

    results = [filt.filter(bad) for _ in range(3)]

    assert results == [True, False, False]


def test_key_mode_logger_collapses_across_messages() -> None:
    """``key_mode='logger'`` keys per logger, ignoring the message."""
    filt = DuplicateFilter(allowed_repetitions=1, key_mode="logger")

    results = [
        filt.filter(_make_record(name="svc", msg="first")),
        filt.filter(_make_record(name="svc", msg="second")),
        filt.filter(_make_record(name="other", msg="third")),
    ]

    assert results == [True, False, True]


def test_key_mode_level_collapses_across_loggers() -> None:
    """``key_mode='level'`` keys per level, ignoring logger and message."""
    filt = DuplicateFilter(allowed_repetitions=1, key_mode="level")

    results = [
        filt.filter(_make_record(name="a", level=logging.WARNING, msg="x")),
        filt.filter(_make_record(name="b", level=logging.WARNING, msg="y")),
        filt.filter(_make_record(name="a", level=logging.ERROR, msg="z")),
    ]

    assert results == [True, False, True]


def test_key_mode_global_collapses_everything() -> None:
    """``key_mode='global'`` shares one counter for every record."""
    filt = DuplicateFilter(allowed_repetitions=1, key_mode="global")

    results = [
        filt.filter(_make_record(name="a", level=logging.WARNING, msg="x")),
        filt.filter(_make_record(name="b", level=logging.ERROR, msg="y")),
    ]

    assert results == [True, False]


def test_explicit_key_callable_overrides_mode() -> None:
    """When ``key=`` is set, ``key_mode`` is ignored."""
    filt = DuplicateFilter(
        allowed_repetitions=1,
        key_mode="template",
        key=lambda record: record.levelno,
    )
    record_a = _make_record(msg="one")
    record_b = _make_record(msg="two")

    first = filt.filter(record_a)
    second = filt.filter(record_b)

    assert first
    assert not second


def test_invalid_key_mode_rejected() -> None:
    """Unknown ``key_mode`` values raise a LogSettingsValidationError."""
    with pytest.raises(LogSettingsValidationError, match="key_mode"):
        DuplicateFilter(key_mode="bogus")  # ty: ignore[invalid-argument-type]


def test_ttl_resets_counter_after_silence() -> None:
    """After ``ttl`` without hits, the counter resets."""
    with freeze_time() as frozen:
        filt = DuplicateFilter(allowed_repetitions=1, ttl=10.0)
        record = _make_record(msg="flood")

        first = filt.filter(record)
        second = filt.filter(record)
        frozen.tick(11)
        third = filt.filter(record)

        assert first
        assert not second
        assert third


def test_ttl_reemits_during_sustained_flood() -> None:
    """A flood that outlives ``ttl`` re-emits once per window."""
    with freeze_time() as frozen:
        filt = DuplicateFilter(allowed_repetitions=1, ttl=10.0)
        record = _make_record(msg="flood")

        first = filt.filter(record)
        dropped_within_window = filt.filter(record)
        frozen.tick(11)
        after_window = filt.filter(record)

        assert first
        assert not dropped_within_window
        assert after_window


def test_ttl_resets_counter_between_sweeps() -> None:
    """A key crossing ``ttl`` between sweeps resets on next sight.

    The once-per-window sweep does not catch a key added after the
    sweep ran, so the per-record ttl check still has to reset it.
    """
    with freeze_time() as frozen:
        filt = DuplicateFilter(allowed_repetitions=1, cache_size=100, ttl=10.0)
        late = _make_record(msg="late")

        # t=0 runs the first sweep (empty) and schedules the next at t=10.
        filt.filter(_make_record(msg="seed"))
        frozen.tick(5)
        # "late" enters at t=5, so the t=10 sweep (cutoff t=0) keeps it.
        first = filt.filter(late)
        dropped = filt.filter(late)
        frozen.tick(5)  # t=10: sweep runs, schedules the next at t=20.
        filt.filter(_make_record(msg="other"))
        frozen.tick(9)  # t=19: before the next sweep, but past "late" ttl.
        reset = filt.filter(late)

        assert first
        assert not dropped
        assert reset


def test_ttl_none_disables_time_expiry() -> None:
    """With ``ttl=None``, long silence does not reset the counter."""
    with freeze_time() as frozen:
        filt = DuplicateFilter(allowed_repetitions=1)
        record = _make_record(msg="flood")

        filt.filter(record)
        filt.filter(record)
        frozen.tick(3600)
        after_long_silence = filt.filter(record)

        assert not after_long_silence


@pytest.mark.parametrize("bad_ttl", [0, -0.5, -1])
def test_non_positive_ttl_rejected(bad_ttl: float) -> None:
    """Non-positive ``ttl`` values raise a LogSettingsValidationError."""
    with pytest.raises(LogSettingsValidationError, match="greater than 0"):
        DuplicateFilter(ttl=bad_ttl)


@pytest.mark.parametrize(
    ("allowed", "cache"),
    [(0, 10), (-1, 10), (10, 0), (10, -5)],
)
def test_invalid_config_rejected(allowed: int, cache: int) -> None:
    """Non-positive config values raise a LogSettingsValidationError."""
    with pytest.raises(LogSettingsValidationError, match="greater than 0"):
        DuplicateFilter(allowed_repetitions=allowed, cache_size=cache)


def test_config_exposed() -> None:
    """Config is readable via the ``config`` property."""
    allowed = 7
    cache = 42

    filt = DuplicateFilter(
        allowed_repetitions=allowed, cache_size=cache, key_mode="template"
    )

    assert filt.config.allowed_repetitions == allowed
    assert filt.config.cache_size == cache
    assert filt.config.key_mode == "template"
