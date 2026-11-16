"""`LogConfig.format` takes a format in any case and reads it back in lowercase."""

import json
import logging
import pickle
import warnings
from collections.abc import Callable, Generator
from contextlib import contextmanager

import pytest

from grelmicro._config import resolve_config
from grelmicro.log import LogConfig, configure
from tests.logging.conftest import parse_json_log

FORMATS = ["auto", "json", "logfmt", "text", "pretty"]
"""The built-in formats, in the documented spelling."""


@contextmanager
def _silent() -> Generator[None]:
    """Fail on any warning raised inside the block."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        yield


@pytest.mark.parametrize("name", FORMATS)
@pytest.mark.parametrize("case", [str.lower, str.upper, str.title])
def test_a_format_in_any_case_reads_back_in_lowercase(
    name: str, case: Callable[[str], str]
) -> None:
    """`json`, `JSON` and `Json` all read back as the plain `str` `"json"`."""
    # Act
    with _silent():
        value = LogConfig(format=case(name)).format

    # Assert
    assert value == name
    assert type(value) is str


def test_the_default_reads_back_as_auto() -> None:
    """Left at its default, the format reads back as `"auto"`."""
    # Act
    value = LogConfig().format

    # Assert
    assert value == "auto"
    assert type(value) is str


@pytest.mark.parametrize("spelling", ["json", "JSON"])
def test_the_environment_takes_a_format_in_any_case(
    monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    """`GREL_LOG_FORMAT=JSON` reads back as `"json"`."""
    # Arrange
    monkeypatch.setenv("GREL_LOG_FORMAT", spelling)

    # Act
    config = resolve_config(
        LogConfig,
        explicit=None,
        kwargs={},
        env_prefix="GREL_LOG_",
        env_load=True,
    )

    # Assert
    assert config.format == "json"


def test_a_custom_template_reads_back_as_written() -> None:
    """A loguru template is kept as written, in its own case."""
    # Act
    value = LogConfig(format="{level} | {MESSAGE}").format

    # Assert
    assert value == "{level} | {MESSAGE}"
    assert type(value) is str


def test_an_uppercase_comparison_no_longer_matches() -> None:
    """`config.format == "JSON"` is false, and comparing warns nothing."""
    # Arrange
    config = LogConfig(format="JSON")

    # Act
    with _silent():
        uppercase = config.format == "JSON"
        lowercase = config.format == "json"

    # Assert
    assert not uppercase
    assert lowercase


@pytest.mark.parametrize("mode", ["python", "json"])
def test_a_dump_holds_the_lowercase_name_and_rebuilds_an_equal_config(
    mode: str,
) -> None:
    """`model_dump()` writes `"json"`, and the dump rebuilds an equal config."""
    # Arrange
    config = LogConfig(format="JSON")

    # Act
    dumped = config.model_dump(mode=mode)

    # Assert
    assert dumped["format"] == "json"
    assert LogConfig(**dumped) == config
    assert hash(LogConfig(**dumped)) == hash(config)


def test_a_dump_excluding_defaults_drops_the_default_format() -> None:
    """Left at its default, the format is dropped from the dump."""
    # Act
    excluded = LogConfig().model_dump(exclude_defaults=True)
    kept = LogConfig(format="JSON").model_dump(exclude_defaults=True)

    # Assert
    assert "format" not in excluded
    assert kept["format"] == "json"


def test_the_json_schema_carries_the_lowercase_default() -> None:
    """The schema default is `"auto"`, and the JSON dump writes `"json"`."""
    # Act
    schema = LogConfig.model_json_schema()
    text = LogConfig(format="JSON").model_dump_json()

    # Assert
    assert schema["properties"]["format"]["default"] == "auto"
    assert json.loads(text)["format"] == "json"


def test_a_pickled_config_loads_equal() -> None:
    """A config survives pickle with its lowercase format."""
    # Arrange
    config = LogConfig(format="JSON")

    # Act
    loaded = pickle.loads(pickle.dumps(config))  # noqa: S301

    # Assert
    assert loaded == config
    assert loaded.format == "json"


@pytest.mark.usefixtures("reset_backend")
def test_configure_and_logging_are_silent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Configuring and writing a record read the format without a warning."""
    # Arrange
    monkeypatch.delenv("GREL_LOG_FORMAT", raising=False)

    # Act
    with _silent():
        configure(format="JSON", otel_enabled=False, env_load=False)
        logging.getLogger("app").warning("hello")

    # Assert
    assert parse_json_log(capsys.readouterr().out)["msg"] == "hello"
