"""The duplicate filter `ttl` takes whole seconds or a `timedelta`.

The silence window is checked at its exact boundary on a clock the test
moves, in whole nanoseconds.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest
from hypothesis import given
from pydantic import ValidationError

from grelmicro._duration import MAX_DURATION, MICROSECOND
from grelmicro.errors import SettingsValidationError
from grelmicro.log import DuplicateFilter, DuplicateFilterConfig
from grelmicro.log import _dedup as dedup_module
from tests._durations import DURATIONS, FLOATS, NANOSECOND, NanosecondClock

WHOLE = 60
UNDER_A_SECOND = timedelta(milliseconds=500)

READ_BACK = [
    pytest.param(WHOLE, timedelta(seconds=WHOLE), id="int"),
    pytest.param(UNDER_A_SECOND, UNDER_A_SECOND, id="timedelta"),
]

FROM_TEXT = [
    pytest.param("60", timedelta(seconds=WHOLE), id="seconds"),
    pytest.param("PT0.5S", UNDER_A_SECOND, id="iso-8601"),
]


def _record(msg: str = "flood") -> logging.LogRecord:
    return logging.LogRecord(
        name="grelmicro.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


def _passes_after(ttl: timedelta, extra_ns: int) -> bool:
    """Return whether a repeat `ttl` plus `extra_ns` after the last passes."""
    clock = NanosecondClock()
    record = _record()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(dedup_module, "monotonic_ns", clock)
        duplicates = DuplicateFilter(
            allowed_repetitions=1, ttl=ttl, env_load=False
        )
        duplicates.filter(record)
        clock.advance(ttl, extra_ns)
        return duplicates.filter(record)


# --- Public API ---


@pytest.mark.parametrize("value", FLOATS)
def test_duplicate_filter_config_float_ttl_refused(value: object) -> None:
    """A float or a bool `ttl` is refused when the config is built."""
    # Act / Assert
    with pytest.raises(
        ValidationError, match="ttl must be whole seconds or a timedelta"
    ):
        DuplicateFilterConfig(ttl=value)


@pytest.mark.parametrize(
    "value", [0, timedelta(0), -1, timedelta(microseconds=-1)]
)
def test_duplicate_filter_config_ttl_of_zero_or_less_refused(
    value: int | timedelta,
) -> None:
    """A `ttl` of zero or less is refused when the config is built."""
    # Act / Assert
    with pytest.raises(ValidationError, match="ttl must be greater than zero"):
        DuplicateFilterConfig(ttl=value)


def test_duplicate_filter_config_ttl_over_a_hundred_years_refused() -> None:
    """A `ttl` over a hundred years is refused when the config is built."""
    # Act / Assert
    with pytest.raises(ValidationError, match="ttl must be at most 100 years"):
        DuplicateFilterConfig(ttl=MAX_DURATION + MICROSECOND)


@pytest.mark.parametrize(("value", "expected"), READ_BACK)
def test_duplicate_filter_config_ttl_reads_back_as_timedelta(
    value: int | timedelta, expected: timedelta
) -> None:
    """Whole seconds and a `timedelta` read back as a `timedelta`."""
    # Act
    config = DuplicateFilterConfig(ttl=value)

    # Assert
    assert config.ttl == expected


def test_duplicate_filter_config_ttl_defaults_to_none() -> None:
    """With no `ttl`, a counter never resets on time alone."""
    # Act
    config = DuplicateFilterConfig()

    # Assert
    assert config.ttl is None


def test_duplicate_filter_config_dumps_ttl_as_iso_8601() -> None:
    """A `ttl` dumps to JSON as ISO 8601 and reads back exactly."""
    # Arrange
    config = DuplicateFilterConfig(ttl=timedelta(milliseconds=1500))

    # Act
    dumped = config.model_dump(mode="json")

    # Assert
    assert dumped["ttl"] == "PT1.5S"
    assert DuplicateFilterConfig.model_validate(dumped).ttl == config.ttl


@pytest.mark.parametrize("value", FLOATS)
def test_duplicate_filter_float_ttl_refused(value: Any) -> None:  # noqa: ANN401
    """A float or a bool `ttl` keyword is refused, naming the setting."""
    # Act / Assert
    with pytest.raises(
        SettingsValidationError,
        match="ttl must be whole seconds or a timedelta",
    ):
        DuplicateFilter(ttl=value, env_load=False)


@pytest.mark.parametrize(("value", "expected"), READ_BACK)
def test_duplicate_filter_ttl_reads_back_as_timedelta(
    value: int | timedelta, expected: timedelta
) -> None:
    """A `ttl` keyword in whole seconds or a `timedelta` reads back."""
    # Act
    duplicates = DuplicateFilter(ttl=value, env_load=False)

    # Assert
    assert duplicates.config.ttl == expected


@pytest.mark.parametrize(("raw", "expected"), FROM_TEXT)
def test_duplicate_filter_ttl_from_environment_reads_as_timedelta(
    raw: str, expected: timedelta, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env text is whole seconds or an ISO 8601 duration."""
    # Arrange
    monkeypatch.setenv("GREL_DUPLICATEFILTER_TTL", raw)

    # Act
    duplicates = DuplicateFilter(env_load=True)

    # Assert
    assert duplicates.config.ttl == expected


@pytest.mark.parametrize("raw", ["none", "None", " NONE "])
def test_duplicate_filter_none_ttl_from_environment_reads_as_no_limit(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env text `none`, in any case and trimmed, reads as no time limit."""
    # Arrange
    monkeypatch.setenv("GREL_DUPLICATEFILTER_TTL", raw)

    # Act
    duplicates = DuplicateFilter(env_load=True)

    # Assert
    assert duplicates.config.ttl is None


def test_duplicate_filter_decimal_ttl_from_environment_refused_without_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decimal number of seconds is refused, and the error never repeats it."""
    # Arrange
    monkeypatch.setenv("GREL_DUPLICATEFILTER_TTL", "4321.5")

    # Act
    with pytest.raises(SettingsValidationError) as refused:
        DuplicateFilter(env_load=True)

    # Assert
    assert "4321.5" not in str(refused.value)


# --- Silence window ---


@given(ttl=DURATIONS)
def test_duplicate_filter_repeat_inside_ttl_dropped(ttl: timedelta) -> None:
    """A repeat one nanosecond before `ttl` has passed is still dropped."""
    # Act
    passed = _passes_after(ttl, -NANOSECOND)

    # Assert
    assert passed is False


@given(ttl=DURATIONS)
def test_duplicate_filter_repeat_at_ttl_passes(ttl: timedelta) -> None:
    """A repeat once `ttl` has passed starts a new burst, to the nanosecond."""
    # Act
    passed = _passes_after(ttl, 0)

    # Assert
    assert passed is True


def test_duplicate_filter_repeat_at_ttl_between_sweeps_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key seen exactly `ttl` ago passes before the next sweep drops it."""
    # Arrange
    clock = NanosecondClock()
    monkeypatch.setattr(dedup_module, "monotonic_ns", clock)
    ttl = timedelta(seconds=10)
    duplicates = DuplicateFilter(allowed_repetitions=1, ttl=ttl, env_load=False)
    record = _record()
    duplicates.filter(_record("seed"))
    clock.advance(timedelta(seconds=5))
    duplicates.filter(record)
    duplicates.filter(record)
    clock.advance(timedelta(seconds=5))
    duplicates.filter(_record("other"))
    clock.advance(timedelta(seconds=5))

    # Act
    passed = duplicates.filter(record)

    # Assert
    assert passed is True
