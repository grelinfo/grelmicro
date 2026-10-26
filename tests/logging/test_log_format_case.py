"""`LogConfig.format` takes a format in any case and reads it back in uppercase until 0.45.0."""

import copy
import json
import logging
import pickle
import re
import warnings
from collections.abc import Callable, Generator
from contextlib import contextmanager

import pytest

from grelmicro._config import resolve_config
from grelmicro._deprecation import FORMAT_CASE, PENDING, format_case
from grelmicro.errors import Grelmicro045Warning
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
def test_a_format_in_any_case_is_accepted(
    name: str, case: Callable[[str], str]
) -> None:
    """`json`, `JSON` and `Json` all name the JSON format."""
    # Act
    with _silent():
        config = LogConfig(format=case(name))

    # Assert
    assert config.format.lower() == name


@pytest.mark.parametrize("name", FORMATS)
def test_a_format_reads_back_in_uppercase_until_0_45(name: str) -> None:
    """The documented `json` reads back as `"JSON"`, a `str`."""
    # Act
    value = LogConfig(format=name).format

    # Assert
    assert str(value) == name.upper()
    assert isinstance(value, str)


def test_the_default_reads_back_as_auto() -> None:
    """Left at its default, the format reads back as `"AUTO"`."""
    # Assert
    assert str(LogConfig().format) == "AUTO"


def test_the_environment_takes_a_lowercase_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`GREL_LOG_FORMAT=json` names the JSON format."""
    # Arrange
    monkeypatch.setenv("GREL_LOG_FORMAT", "json")

    # Act
    config = resolve_config(
        LogConfig,
        explicit=None,
        kwargs={},
        env_prefix="GREL_LOG_",
        env_load=True,
    )

    # Assert
    assert str(config.format) == "JSON"


def test_a_custom_template_reads_back_as_written_and_compares_silently() -> (
    None
):
    """A loguru template is a plain `str`, and comparing it warns nothing."""
    # Act
    value = LogConfig(format="{level} | {message}").format

    # Assert
    assert type(value) is str
    with _silent():
        assert value != "JSON"


@pytest.mark.parametrize(
    ("spelling", "name"),
    [("JSON", "json"), ("json", "json"), ("Logfmt", "logfmt")],
)
def test_comparing_with_a_format_name_warns_at_the_caller(
    spelling: str, name: str
) -> None:
    """The answer changes at 0.45.0, so the comparison warns at its own line."""
    # Arrange
    value = LogConfig(format="json").format
    entry = format_case(name)

    # Act
    with pytest.warns(
        Grelmicro045Warning, match=re.escape(entry.message)
    ) as caught:
        result = value == spelling

    # Assert
    assert result is (spelling == "JSON")
    assert caught[0].filename == __file__


@pytest.mark.parametrize(
    "compare",
    [
        pytest.param(lambda value: value == "JSON", id="eq"),
        pytest.param(lambda value: value != "JSON", id="ne"),
        pytest.param(lambda value: value == "TEXT", id="eq-other-format"),
        pytest.param(lambda value: value == "json", id="eq-lowercase"),
        pytest.param(lambda value: value in ("JSON", "TEXT"), id="in-tuple"),
        pytest.param(lambda value: value in {"JSON", "TEXT"}, id="in-set"),
        pytest.param({"JSON": 1}.get, id="dict-key"),
        pytest.param(lambda value: "JSON" == value, id="reflected"),  # noqa: SIM300
    ],
)
def test_every_comparison_with_a_format_name_warns(
    compare: Callable[[str], object],
) -> None:
    """`==`, `!=`, `in` and a dict lookup against a format name all warn."""
    # Arrange
    value = LogConfig(format="JSON").format

    # Act / Assert
    with pytest.warns(Grelmicro045Warning, match="from 0.45.0"):
        compare(value)


def _is_text(value: str) -> bool:
    """Return whether `value` is the text format, through a `match`."""
    match value:
        case "TEXT":
            return True
        case _:  # pragma: no cover
            return False


def test_a_match_on_a_format_name_warns() -> None:
    """A `match` statement compares with `==`, so it warns too."""
    # Arrange
    value = LogConfig(format="text").format

    # Act
    with pytest.warns(Grelmicro045Warning, match=re.escape('"text"')):
        result = _is_text(value)

    # Assert
    assert result


def test_comparing_case_insensitively_is_silent() -> None:
    """`config.format.lower() == "json"` works today and after 0.45.0."""
    # Arrange
    value = LogConfig(format="JSON").format

    # Act / Assert
    with _silent():
        assert value.lower() == "json"
        assert value.casefold() == "json"


def test_every_other_use_of_the_format_is_silent() -> None:
    """Printing, hashing, copying, dumping and comparing two configs warn nothing."""
    # Arrange
    config = LogConfig(format="json")

    # Act / Assert
    with _silent():
        assert str(config.format) == "JSON"
        assert f"{config.format}" == "JSON"
        assert hash(config.format) == hash("JSON")
        assert str(pickle.loads(pickle.dumps(config.format))) == "JSON"  # noqa: S301
        assert str(copy.deepcopy(config.format)) == "JSON"
        assert copy.copy(config.format) is config.format
        assert config == LogConfig(format="JSON")
        assert hash(config) == hash(LogConfig(format="JSON"))
        assert config.format != 1
        assert config.format != "{level} | {message}"
        dumped = config.model_dump(mode="json")
        text = config.model_dump_json()
        excluded = LogConfig().model_dump(exclude_defaults=True)

    # Assert
    assert type(dumped["format"]) is str
    assert str.__eq__(dumped["format"], "JSON")
    assert json.loads(text)["format"] == "JSON"
    assert "format" not in excluded


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
        configure(format="json", otel_enabled=False, env_load=False)
        logging.getLogger("app").warning("hello")

    # Assert
    assert parse_json_log(capsys.readouterr().out)["msg"] == "hello"


def test_the_read_back_change_is_pending_until_0_45() -> None:
    """The stale-entry guard covers the switch."""
    # Assert
    assert FORMAT_CASE in PENDING
    assert FORMAT_CASE.switch_in == "0.45.0"
    assert FORMAT_CASE.category is Grelmicro045Warning


def test_the_warning_names_the_lowercase_comparison() -> None:
    """The message says what reads back and how to compare."""
    # Assert
    assert format_case("json").message == (
        '`LogConfig.format` reads back as `"json"` rather than `"JSON"` '
        'from 0.45.0. Compare `config.format.lower() == "json"` now.'
    )


def test_the_json_schema_carries_a_plain_default() -> None:
    """The schema keeps the `"AUTO"` default, and building it warns nothing."""
    # Act
    with _silent():
        schema = LogConfig.model_json_schema()

    # Assert
    default = schema["properties"]["format"]["default"]
    assert type(default) is str
    assert str.__eq__(default, "AUTO")


def test_a_pickled_format_loads_as_a_plain_str() -> None:
    """A pickle holds a plain `str`, so it loads on a release without the read-back."""
    # Arrange
    value = LogConfig(format="json").format

    # Act
    with _silent():
        loaded = pickle.loads(pickle.dumps(value))  # noqa: S301

    # Assert
    assert type(loaded) is str
    assert str.__eq__(loaded, "JSON")
    assert b"_UppercaseFormat" not in pickle.dumps(value)


@pytest.mark.parametrize("mode", ["python", "json"])
def test_a_dump_holds_a_plain_str_in_every_mode(mode: str) -> None:
    """`model_dump()` returns a plain `str`, and the dump rebuilds an equal config."""
    # Arrange
    config = LogConfig(format="json")

    # Act
    with _silent():
        dumped = config.model_dump(mode=mode)
        rebuilt = LogConfig(**dumped)
        equal = rebuilt == config

    # Assert
    assert type(dumped["format"]) is str
    assert str.__eq__(dumped["format"], "JSON")
    assert equal


@pytest.mark.parametrize("mode", ["python", "json"])
def test_a_dump_excluding_defaults_drops_the_default_format(mode: str) -> None:
    """Left at its default, the format is dropped, and the dump warns nothing."""
    # Act
    with _silent():
        excluded = LogConfig().model_dump(mode=mode, exclude_defaults=True)
        kept = LogConfig(format="json").model_dump(
            mode=mode, exclude_defaults=True
        )

    # Assert
    assert "format" not in excluded
    assert str.__eq__(kept["format"], "JSON")
