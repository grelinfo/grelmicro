"""An argument that picks a mode takes a string literal, matched in any case."""

import annotationlib
import importlib
import json
import re
import warnings
from collections.abc import Callable

import pytest
from pydantic import BaseModel, ValidationError

from grelmicro import Grelmicro
from grelmicro._config import resolve_config
from grelmicro.cache import Cache, cached
from grelmicro.errors import (
    GrelmicroConfigWarning,
    GrelmicroFutureWarning,
    SettingsValidationError,
)
from grelmicro.log import (
    DuplicateFilterConfig,
    Log,
    RateLimitFilterConfig,
    configure,
)
from grelmicro.log.config import LogConfig
from grelmicro.metrics import Metrics
from grelmicro.metrics.config import MetricsConfig
from grelmicro.providers.memory import MemoryProvider
from grelmicro.resilience.backoffs import ExponentialBackoff
from grelmicro.resilience.circuitbreaker.consecutive_count import (
    ConsecutiveCountConfig,
)
from grelmicro.task._cron import CronTask
from grelmicro.task._interval import IntervalTask
from grelmicro.trace import Trace
from grelmicro.trace.config import TraceConfig

CHOICES = [
    (MetricsConfig, "exporter", "Prometheus", "prometheus"),
    (TraceConfig, "exporter", "OTLP-HTTP", "otlp-http"),
    (TraceConfig, "processor", "Simple", "simple"),
    (TraceConfig, "sampler", "TraceIdRatio", "traceidratio"),
    (LogConfig, "level", "debug", "DEBUG"),
    (LogConfig, "format", "logfmt", "LOGFMT"),
    (LogConfig, "backend", "StructLog", "structlog"),
    (LogConfig, "json_serializer", "STDLIB", "stdlib"),
]
"""Each mode field, a spelling in another case, and the stored spelling."""

REMOVED_ENUMS = [
    ("grelmicro.metrics", "MetricsExporterType"),
    ("grelmicro.trace", "TraceExporterType"),
    ("grelmicro.trace", "TraceProcessorType"),
    ("grelmicro.trace", "TraceSamplerType"),
    ("grelmicro.log", "LogLevelType"),
    ("grelmicro.log", "LogFormatType"),
    ("grelmicro.log", "LogBackendType"),
    ("grelmicro.log", "LogSerializerType"),
]
"""Each mode enum removed in 0.44.0, and the package that exported it."""


@pytest.mark.parametrize(("config", "field", "_any", "stored"), CHOICES)
def test_the_stored_spelling_is_accepted(
    config: type[BaseModel], field: str, _any: str, stored: str
) -> None:
    """The documented string validates and reads back as an equal `str`."""
    # Act
    value = getattr(config(**{field: stored}), field)

    # Assert
    assert value == stored
    assert type(value) is str


@pytest.mark.parametrize(("config", "field", "spelling", "stored"), CHOICES)
def test_any_case_reads_as_the_stored_spelling(
    config: type[BaseModel], field: str, spelling: str, stored: str
) -> None:
    """A string in another case is stored in the documented spelling."""
    # Act
    value = getattr(config(**{field: spelling}), field)

    # Assert
    assert value == stored
    assert type(value) is str


@pytest.mark.parametrize(("config", "field", "spelling", "stored"), CHOICES)
def test_any_case_reads_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    config: type[BaseModel],
    field: str,
    spelling: str,
    stored: str,
) -> None:
    """An environment variable in another case is stored the same way."""
    # Arrange
    monkeypatch.setenv(f"GREL_CHOICE_{field.upper()}", spelling)

    # Act
    resolved = resolve_config(
        config,
        explicit=None,
        kwargs={},
        env_prefix="GREL_CHOICE_",
        env_load=True,
    )

    # Assert
    value = getattr(resolved, field)
    assert value == stored
    assert type(value) is str


@pytest.mark.parametrize(("config", "field", "_any", "_stored"), CHOICES)
def test_the_default_reads_back_as_a_plain_str(
    config: type[BaseModel], field: str, _any: str, _stored: str
) -> None:
    """A mode field left at its default holds a plain `str`."""
    # Act
    value = getattr(config(), field)

    # Assert
    assert type(value) is str


@pytest.mark.parametrize(
    ("config", "field", "_any", "_stored"),
    [choice for choice in CHOICES if choice[1] != "format"],
)
def test_an_unknown_choice_is_refused(
    config: type[BaseModel], field: str, _any: str, _stored: str
) -> None:
    """A string that matches no choice in any case fails validation.

    `format` is left out, since it also takes a custom template.
    """
    # Act / Assert
    with pytest.raises(ValidationError):
        config(**{field: "no-such-choice"})


def test_a_non_string_is_left_for_the_field_to_refuse() -> None:
    """Only a string is matched, anything else fails as before."""
    # Act / Assert
    with pytest.raises(ValidationError):
        MetricsConfig(exporter=1)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_a_custom_format_template_is_kept_as_written() -> None:
    """A loguru template that names no built-in format is stored untouched."""
    # Act
    config = LogConfig(format="{level} | {message}")

    # Assert
    assert config.format == "{level} | {message}"


def test_an_unknown_env_choice_reports_the_field() -> None:
    """A wrong value from the environment still fails with the field named."""
    # Act / Assert
    with pytest.raises(SettingsValidationError, match="exporter"):
        resolve_config(
            MetricsConfig,
            explicit=None,
            kwargs={"exporter": "no-such-exporter"},
            env_prefix="GREL_CHOICE_",
            env_load=False,
        )


@pytest.mark.parametrize(("package", "name"), REMOVED_ENUMS)
def test_a_removed_enum_no_longer_imports(package: str, name: str) -> None:
    """The old enum is gone from its package and its config module."""
    # Act / Assert
    for source in (package, f"{package}.config"):
        with pytest.raises(ImportError, match=name):
            exec(f"from {source} import {name}", {})  # noqa: S102
        with pytest.raises(AttributeError, match=name):
            getattr(importlib.import_module(source), name)


@pytest.mark.parametrize(
    "package", ["grelmicro.metrics", "grelmicro.trace", "grelmicro.log"]
)
def test_a_removed_enum_is_off_the_public_surface(package: str) -> None:
    """`__all__` and `dir()` no longer list the old enums."""
    # Arrange
    module = importlib.import_module(package)
    removed = {name for source, name in REMOVED_ENUMS if source == package}

    # Act / Assert
    assert removed.isdisjoint(module.__all__)
    assert removed.isdisjoint(dir(module))


@pytest.mark.parametrize(("config", "field", "_any", "stored"), CHOICES)
def test_a_mode_field_dumps_to_json_silently(
    config: type[BaseModel], field: str, _any: str, stored: str
) -> None:
    """A config dumps its mode fields to JSON as plain strings, silently."""
    # Arrange
    model = config(**{field: stored})

    # Act
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        dumped = model.model_dump(mode="json")
        text = model.model_dump_json()

    # Assert
    assert dumped[field] == stored
    assert type(dumped[field]) is str
    assert json.loads(text)[field] == stored


def test_a_circuit_breaker_log_level_matches_any_case() -> None:
    """The breaker's `log_level` reads `error` as `ERROR`, like `LogConfig`."""
    # Act
    config = ConsecutiveCountConfig(log_level="error")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    # Assert
    assert config.log_level == "ERROR"


@pytest.mark.parametrize("callable_", [Metrics, Trace, Log, configure])
def test_a_mode_parameter_names_no_removed_enum(
    callable_: Callable[..., object],
) -> None:
    """Each mode parameter takes the `Literal` type, not an old enum."""
    # Arrange
    target = callable_.__init__ if isinstance(callable_, type) else callable_

    # Act
    annotations = annotationlib.get_annotations(
        target, format=annotationlib.Format.STRING
    )

    # Assert
    named = {
        name
        for annotation in annotations.values()
        for name in re.findall(r"\b\w+Type\b", annotation)
    }
    assert named == set()


OTHER_CASE_TIERS = [
    ("Production", "production"),
    ("PRODUCTION", "production"),
    ("pRoDuction", "production"),
    ("STAGING", "staging"),
    ("Test", "test"),
    ("DEVELOPMENT", "development"),
]
"""Each declared tier in another case, and the tier it names."""


@pytest.mark.parametrize(("spelling", "tier"), OTHER_CASE_TIERS)
def test_an_environment_variable_in_another_case_reads_as_the_tier(
    monkeypatch: pytest.MonkeyPatch, spelling: str, tier: str
) -> None:
    """`GREL_ENVIRONMENT=Production` declares `production`, with no warning."""
    # Arrange
    monkeypatch.setenv("GREL_ENVIRONMENT", spelling)

    # Act
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        micro = Grelmicro()

    # Assert
    assert micro.environment == tier


@pytest.mark.parametrize("value", ["preprod", "Prod", "PRODUCTIONS"])
def test_an_environment_variable_naming_no_tier_reads_as_undeclared(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A value that names no tier in any case keeps today's warning only."""
    # Arrange
    monkeypatch.setenv("GREL_ENVIRONMENT", value)

    # Act
    with warnings.catch_warnings():
        warnings.simplefilter("error", GrelmicroFutureWarning)
        with pytest.warns(GrelmicroConfigWarning, match="is not one of"):
            micro = Grelmicro()

    # Assert
    assert micro.environment is None


@pytest.mark.parametrize(("spelling", "tier"), OTHER_CASE_TIERS)
def test_an_environment_argument_in_another_case_reads_as_the_tier(
    spelling: str, tier: str
) -> None:
    """`environment="Production"` declares `production`, with no warning."""
    # Act
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        micro = Grelmicro(environment=spelling)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    # Assert
    assert micro.environment == tier


@pytest.mark.parametrize("tier", [tier for _, tier in OTHER_CASE_TIERS])
def test_an_exact_case_environment_is_silent(
    monkeypatch: pytest.MonkeyPatch, tier: str
) -> None:
    """A tier spelled as documented warns nothing, from either door."""
    # Arrange
    monkeypatch.setenv("GREL_ENVIRONMENT", tier)

    # Act
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        from_env = Grelmicro()
        from_argument = Grelmicro(environment=tier)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    # Assert
    assert from_env.environment == tier
    assert from_argument.environment == tier


@pytest.mark.parametrize("value", ["preprod", 1])
def test_an_unknown_environment_argument_raises_without_a_future_warning(
    value: object,
) -> None:
    """A value that names no tier in any case keeps today's error only."""
    # Act / Assert
    with warnings.catch_warnings():
        warnings.simplefilter("error", GrelmicroFutureWarning)
        with pytest.raises(SettingsValidationError, match="must be one of"):
            Grelmicro(environment=value)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_a_component_requirement_matches_any_case() -> None:
    """`requires="Cluster"` reads back as `cluster`."""
    # Act
    cache = Cache(MemoryProvider(), requires="Cluster")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    # Assert
    assert cache.requires == "cluster"


def test_a_fold_scope_matches_any_case() -> None:
    """`lock="Cluster"` is read as `cluster`, then checked as usual."""
    # Act / Assert
    cached(ttl=60, lock="PROCESS")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(SettingsValidationError, match="lock='cluster'"):
        cached(ttl=60, lock="Cluster")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    with pytest.raises(SettingsValidationError, match="lock= takes"):
        cached(ttl=60, lock="nowhere")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]


def test_a_backoff_jitter_matches_any_case() -> None:
    """`jitter="Equal"` is stored as `equal`."""
    # Act
    backoff = ExponentialBackoff(jitter="Equal")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    # Assert
    assert backoff.jitter == "equal"


def test_a_filter_key_mode_matches_any_case() -> None:
    """`key_mode="Level"` is stored as `level` on both log filters."""
    # Act
    limited = RateLimitFilterConfig(key_mode="Level")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    deduplicated = DuplicateFilterConfig(key_mode="GLOBAL")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    # Assert
    assert limited.key_mode == "level"
    assert deduplicated.key_mode == "global"


async def _job() -> None:
    """Do nothing, as the body of a task under test."""


def test_a_claim_gate_matches_any_case() -> None:
    """`gate="Claim"` gates an interval task and a cron task as `claim`."""
    # Act
    interval = IntervalTask(interval=5, function=_job, gate="Claim")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
    cron = CronTask(expr="* * * * *", function=_job, gate="CLAIM")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    # Assert
    assert interval._gate_label == "claim"
    assert cron._gate == "claim"
