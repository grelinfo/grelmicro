"""A setting that is renamed or removed refuses its old variable.

Without a refusal the old variable is ignored, and the component runs on
its default instead of the value the operator set.
"""

from __future__ import annotations

import importlib
import json
import logging
import pkgutil
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import BaseModel

import grelmicro
from grelmicro._config import reconfigure_all
from grelmicro.errors import SettingsValidationError
from grelmicro.http import IdempotentRequests
from grelmicro.resilience import CircuitBreaker, Shield

FIELDS_FILE = Path(__file__).parent / "config_fields.json"
"""Every field each `*Config` class has shipped, keyed by its import path."""

RETIRED = [
    pytest.param(
        "GREL_CIRCUITBREAKER_PAY_IGNORE_EXCEPTIONS",
        "builtins.ValueError",
        lambda: CircuitBreaker.consecutive_count("pay"),
        "GREL_CIRCUITBREAKER_PAY_WHEN",
        id="circuit-breaker",
    ),
    pytest.param(
        "GREL_CIRCUITBREAKER_IGNORE_EXCEPTIONS",
        "builtins.ValueError",
        lambda: CircuitBreaker.consecutive_count("pay"),
        "GREL_CIRCUITBREAKER_WHEN",
        id="circuit-breaker-kind",
    ),
    pytest.param(
        "GREL_SHIELD_DB_TIMEOUT_ERRORS",
        "builtins.TimeoutError",
        lambda: Shield("db", when=TimeoutError),
        "GREL_SHIELD_DB_WHEN",
        id="shield",
    ),
    pytest.param(
        "GREL_IDEMPOTENT_REQUESTS_WAIT_TIMEOUT",
        "5",
        IdempotentRequests,
        "GREL_IDEMPOTENT_REQUESTS_MAX_WAIT",
        id="idempotent-requests",
    ),
]


@pytest.mark.parametrize(("variable", "value", "build", "replacement"), RETIRED)
def test_a_retired_variable_refuses_construction(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
    build: Callable[[], object],
    replacement: str,
) -> None:
    """The old variable raises, naming itself and the new one, never the value."""
    # Arrange
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv(variable, value)

    # Act
    with pytest.raises(SettingsValidationError) as error:
        build()

    # Assert
    message = str(error.value)
    assert f"- {variable}: " in message
    assert replacement in message
    assert value not in message


@pytest.mark.parametrize(("variable", "value", "build", "replacement"), RETIRED)
def test_a_retired_variable_with_env_reads_off_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
    build: Callable[[], object],
    replacement: str,  # noqa: ARG001
) -> None:
    """With env reads off, no variable is read, the old one included."""
    # Arrange
    monkeypatch.delenv("GREL_ENV_LOAD", raising=False)
    monkeypatch.setenv(variable, value)

    # Act
    built = build()

    # Assert
    assert built is not None


async def test_a_retired_key_refuses_the_reload_of_its_instance_only(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A mounted old key keeps its instance's config and logs the fix."""
    # Arrange
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    breaker = CircuitBreaker.consecutive_count("reload", error_threshold=3)
    other = CircuitBreaker.consecutive_count("other", error_threshold=3)

    # Act
    with caplog.at_level(logging.WARNING, logger="grelmicro"):
        await reconfigure_all(
            {
                "GREL_CIRCUITBREAKER_RELOAD_IGNORE_EXCEPTIONS": "builtins.Secret",
                "GREL_CIRCUITBREAKER_RELOAD_ERROR_THRESHOLD": "9",
                "GREL_CIRCUITBREAKER_OTHER_ERROR_THRESHOLD": "7",
            }
        )

    # Assert
    assert breaker.config.error_threshold == 3  # noqa: PLR2004
    assert other.config.error_threshold == 7  # noqa: PLR2004
    assert "GREL_CIRCUITBREAKER_RELOAD_IGNORE_EXCEPTIONS" in caplog.text
    assert "GREL_CIRCUITBREAKER_RELOAD_WHEN" in caplog.text
    assert "builtins.Secret" not in caplog.text


def _config_classes() -> dict[str, type[BaseModel]]:
    """Return every `*Config` model grelmicro ships, keyed by import path.

    Every module is imported first, so the answer never depends on which
    tests ran before.
    """
    for module in pkgutil.walk_packages(grelmicro.__path__, "grelmicro."):
        importlib.import_module(module.name)
    found: dict[str, type[BaseModel]] = {}
    pending: list[type[BaseModel]] = [BaseModel]
    while pending:
        for cls in pending.pop().__subclasses__():
            pending.append(cls)
            module = cls.__module__
            if (
                module.split(".")[0] == grelmicro.__name__
                and cls.__name__.endswith("Config")
                and not cls.__name__.startswith("_")
            ):
                found[f"{module}.{cls.__qualname__}"] = cls
    return found


def test_every_shipped_config_field_is_current_or_retired() -> None:
    """A renamed or removed field is retired, so its variable refuses."""
    # Arrange
    shipped: dict[str, list[str]] = json.loads(FIELDS_FILE.read_text())
    classes = _config_classes()

    # Act
    dropped = {
        path: sorted(
            field
            for field in fields
            if field not in classes[path].model_fields
            and field not in getattr(classes[path], "__retired_fields__", {})
        )
        for path, fields in shipped.items()
        if path in classes
    }

    # Assert
    assert {path: fields for path, fields in dropped.items() if fields} == {}


def test_every_current_config_field_is_recorded() -> None:
    """A new config or field is added to the shipped list."""
    # Arrange
    shipped: dict[str, list[str]] = json.loads(FIELDS_FILE.read_text())

    # Act
    missing = {
        path: sorted(set(cls.model_fields) - set(shipped.get(path, [])))
        for path, cls in _config_classes().items()
    }

    # Assert
    assert {path: fields for path, fields in missing.items() if fields} == {}
