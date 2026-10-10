"""`OutcomeFilter`, the config field type every `when=` resolves through."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter

from grelmicro._describe import _config_of
from grelmicro.resilience import (
    CircuitBreaker,
    ConsecutiveCountConfig,
    Match,
    RetryConfig,
    Shield,
)
from grelmicro.resilience._when import OutcomeFilter


def test_outcome_filter_class_filter_dumps_as_class_names() -> None:
    """A class filter dumps to JSON as the class names the environment reads."""
    # Arrange
    config = ConsecutiveCountConfig(when=(KeyError, ValueError))

    # Act
    dumped = config.model_dump(mode="json")

    # Assert
    assert dumped["when"] == ["builtins.KeyError", "builtins.ValueError"]


def test_outcome_filter_class_filter_round_trips_through_json() -> None:
    """A class filter read back from its JSON dump equals the original."""
    # Arrange
    config = RetryConfig(when=(KeyError, ValueError))

    # Act
    restored = RetryConfig.model_validate_json(config.model_dump_json())

    # Assert
    assert restored == config


def test_outcome_filter_other_filter_dumps_as_its_repr() -> None:
    """A filter that is not a plain class test dumps to JSON as its repr."""
    # Arrange
    config = ConsecutiveCountConfig(when=Match.not_exception(ValueError))

    # Act
    dumped = config.model_dump(mode="json")

    # Assert
    assert dumped["when"] == "Match.not_exception(ValueError)"


def test_outcome_filter_python_dump_keeps_the_match() -> None:
    """A Python dump keeps the `Match` itself, so a copy stays equal."""
    # Arrange
    config = ConsecutiveCountConfig(when=ValueError)

    # Act
    dumped = config.model_dump()

    # Assert
    assert dumped["when"] is config.when


def test_outcome_filter_json_schema_describes_class_names() -> None:
    """The input schema reads a class name or a list of class names."""
    # Act
    schema = TypeAdapter(OutcomeFilter).json_schema()

    # Assert
    assert schema == {
        "anyOf": [
            {"type": "string"},
            {"items": {"type": "string"}, "type": "array"},
        ]
    }


def test_describe_shows_a_circuit_breaker_outcome_filter() -> None:
    """A breaker's resolved config reports its filter as class names."""
    # Arrange
    breaker = CircuitBreaker.consecutive_count("described", env_load=False)

    # Act
    config = _config_of(breaker)

    # Assert
    assert config["when"] == ["builtins.Exception"]


def test_describe_shows_a_shield_outcome_filter() -> None:
    """A Shield's resolved config reports its filter as class names."""
    # Arrange
    shield = Shield.api("described", when=ValueError)

    # Act
    config = _config_of(shield)

    # Assert
    assert config["when"] == ["builtins.ValueError"]


class _Outer:
    """Holds a nested exception class."""

    class NestedError(Exception):
        """An exception class nested in another class."""


class ModuleLevelError(Exception):
    """An exception class defined at module level."""


def test_outcome_filter_nested_class_dumps_as_repr() -> None:
    """A nested class cannot be read back by name, so it dumps as repr."""
    # Arrange
    config = ConsecutiveCountConfig(when=_Outer.NestedError)

    # Act
    dumped = config.model_dump(mode="json")

    # Assert
    assert dumped["when"] == repr(config.when)


def test_outcome_filter_local_class_dumps_as_repr() -> None:
    """A class defined in a function dumps as repr."""

    # Arrange
    class _LocalError(Exception):
        """An exception class local to this test."""

    config = ConsecutiveCountConfig(when=_LocalError)

    # Act
    dumped = config.model_dump(mode="json")

    # Assert
    assert dumped["when"] == repr(config.when)


def test_outcome_filter_module_level_class_round_trips_through_json() -> None:
    """A module-level class dumps by name and reads back to an equal filter."""
    # Arrange
    config = ConsecutiveCountConfig(when=ModuleLevelError)

    # Act
    restored = ConsecutiveCountConfig.model_validate_json(
        config.model_dump_json()
    )

    # Assert
    assert config.model_dump(mode="json")["when"] == [
        f"{__name__}.ModuleLevelError"
    ]
    assert restored == config


@pytest.mark.parametrize(
    "error_class",
    [
        type("ModuleLevelError", (Exception,), {"__module__": __name__}),
        type("GhostError", (Exception,), {"__module__": "no_such_module_here"}),
    ],
    ids=["shadowed-name", "unimportable-module"],
)
def test_outcome_filter_class_its_name_does_not_find_dumps_as_repr(
    error_class: type[Exception],
) -> None:
    """A class its own name does not read back to dumps as repr."""
    # Arrange
    config = ConsecutiveCountConfig(when=error_class)

    # Act
    dumped = config.model_dump(mode="json")

    # Assert
    assert dumped["when"] == repr(config.when)


class _SubclassedMatch(Match):
    """A `Match` subclass that changes nothing."""

    __slots__ = ()


def test_outcome_filter_match_subclass_dumps_as_repr() -> None:
    """A `Match` subclass filter dumps as its repr, never as plain names."""
    # Arrange
    config = ConsecutiveCountConfig(when=_SubclassedMatch.exception(KeyError))

    # Act
    dumped = config.model_dump(mode="json")

    # Assert
    assert dumped["when"] == repr(config.when)
