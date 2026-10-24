"""An argument that picks a mode takes a string literal, matched in any case."""

import annotationlib
import copy
import importlib
import json
import pickle
import re
import warnings
from collections.abc import Callable
from enum import StrEnum
from types import SimpleNamespace
from typing import Literal

import pytest
from pydantic import BaseModel, ValidationError

from grelmicro import Grelmicro
from grelmicro._config import resolve_config
from grelmicro._deprecation import (
    ENVIRONMENT_CASE,
    MEMBER_ATTRIBUTES,
    MODE_ENUMS,
    PENDING,
    environment_case,
)
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
from tests._future import assert_future_warning

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

ENUMS = [
    ("grelmicro.metrics.config", "MetricsExporterType", "PROMETHEUS"),
    ("grelmicro.trace.config", "TraceExporterType", "OTLP_HTTP"),
    ("grelmicro.trace.config", "TraceProcessorType", "SIMPLE"),
    ("grelmicro.trace.config", "TraceSamplerType", "TRACEIDRATIO"),
    ("grelmicro.log.config", "LogLevelType", "DEBUG"),
    ("grelmicro.log.config", "LogFormatType", "LOGFMT"),
    ("grelmicro.log.config", "LogBackendType", "STRUCTLOG"),
    ("grelmicro.log.config", "LogSerializerType", "STDLIB"),
]
"""Each mode enum, the config module it lived in, and a member, in `CHOICES` order."""


NEW_TYPES = {
    "MetricsExporterType": ("grelmicro.metrics", "MetricsExporter"),
    "TraceExporterType": ("grelmicro.trace", "TraceExporter"),
    "TraceProcessorType": ("grelmicro.trace", "TraceProcessor"),
    "TraceSamplerType": ("grelmicro.trace", "TraceSampler"),
    "LogLevelType": ("grelmicro.types", "LogLevel"),
    "LogFormatType": ("grelmicro.log", "LogFormat"),
    "LogBackendType": ("grelmicro.log", "LogBackend"),
    "LogSerializerType": ("grelmicro.log", "LogSerializer"),
}
"""The `Literal` type that replaces each mode enum, and where it lives."""


def _package(module: str) -> str:
    """Return the package that re-exported the enums of `module`."""
    return module.removesuffix(".config")


def _enum(module: str, name: str) -> type[StrEnum]:
    """Return a mode enum, silencing the warning it raises."""
    with pytest.warns(GrelmicroFutureWarning):
        return getattr(importlib.import_module(module), name)


@pytest.mark.parametrize(("config", "field", "_any", "stored"), CHOICES)
def test_the_stored_spelling_is_accepted(
    config: type[BaseModel], field: str, _any: str, stored: str
) -> None:
    """The documented string validates and reads back as an equal `str`."""
    # Act
    value = getattr(config(**{field: stored}), field)

    # Assert
    assert value == stored
    assert isinstance(value, str)


@pytest.mark.parametrize(("config", "field", "spelling", "stored"), CHOICES)
def test_any_case_reads_as_the_stored_spelling(
    config: type[BaseModel], field: str, spelling: str, stored: str
) -> None:
    """A string in another case is stored in the documented spelling."""
    # Act
    value = getattr(config(**{field: spelling}), field)

    # Assert
    assert value == stored
    assert isinstance(value, str)


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
    assert isinstance(value, str)


@pytest.mark.parametrize(
    ("choice", "enum"), list(zip(CHOICES, ENUMS, strict=True))
)
def test_an_enum_member_still_validates_as_a_string(
    choice: tuple[type[BaseModel], str, str, str],
    enum: tuple[str, str, str],
) -> None:
    """A member of the old enum is accepted and read back as that member."""
    # Arrange
    config, field, _, stored = choice
    module, name, member = enum
    value = _enum(module, name)[member]

    # Act
    result = getattr(config(**{field: value}), field)

    # Assert
    assert result == stored
    assert result is value


@pytest.mark.parametrize(
    ("choice", "enum"), list(zip(CHOICES, ENUMS, strict=True))
)
def test_a_string_reads_back_as_the_old_enum_member_until_0_44(
    choice: tuple[type[BaseModel], str, str, str],
    enum: tuple[str, str, str],
) -> None:
    """A string reads back as its member, so `is` and `.value` still work."""
    # Arrange
    config, field, spelling, stored = choice
    module, name, member = enum
    expected = _enum(module, name)[member]

    # Act
    result = getattr(config(**{field: spelling}), field)

    # Assert
    assert result is expected
    with pytest.warns(GrelmicroFutureWarning):
        assert result.value == stored


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


@pytest.mark.parametrize(("module", "name", "_member"), ENUMS)
def test_the_config_module_warns_on_each_access(
    module: str, name: str, _member: str
) -> None:
    """Reading the enum from its config module warns every time, at the caller."""
    # Arrange
    source = importlib.import_module(module)
    message = re.escape(MODE_ENUMS[name].message)

    # Act / Assert
    for _ in range(2):
        with pytest.warns(GrelmicroFutureWarning, match=message) as caught:
            getattr(source, name)
        assert caught[0].filename == __file__


@pytest.mark.parametrize(("module", "name", "_member"), ENUMS)
def test_the_package_warns_on_each_access(
    module: str, name: str, _member: str
) -> None:
    """Reading the enum from its package warns every time, at the caller."""
    # Arrange
    package = importlib.import_module(_package(module))
    message = re.escape(MODE_ENUMS[name].message)

    # Act / Assert
    for _ in range(2):
        with pytest.warns(GrelmicroFutureWarning, match=message) as caught:
            getattr(package, name)
        assert caught[0].filename == __file__
    assert name not in vars(package)
    assert name in package.__all__


@pytest.mark.parametrize(
    "package", ["grelmicro.metrics", "grelmicro.trace", "grelmicro.log"]
)
def test_star_import_binds_the_old_enums_and_warns(package: str) -> None:
    """`from grelmicro.log import *` still binds each old enum, with a warning."""
    # Arrange
    names = {name for module, name, _ in ENUMS if _package(module) == package}
    namespace: dict[str, object] = {}

    # Act
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        exec(f"from {package} import *", namespace)  # noqa: S102

    # Assert
    future = [
        warning
        for warning in caught
        if issubclass(warning.category, GrelmicroFutureWarning)
    ]
    assert {str(warning.message) for warning in future} == {
        MODE_ENUMS[name].message for name in names
    }
    assert {warning.filename for warning in future} == {"<string>"}
    for name in names:
        assert namespace[name] is _enum(f"{package}.config", name)


@pytest.mark.parametrize(("module", "name", "_member"), ENUMS)
def test_from_import_warns(module: str, name: str, _member: str) -> None:
    """`from ... import` of an old enum warns."""
    # Act / Assert
    for source in (module, _package(module)):
        with pytest.warns(GrelmicroFutureWarning):
            exec(f"from {source} import {name}", {})  # noqa: S102


@pytest.mark.parametrize(("module", "name", "_member"), ENUMS)
def test_the_package_and_the_module_serve_the_same_enum(
    module: str, name: str, _member: str
) -> None:
    """Both locations return one class, so an `is` check keeps working."""
    # Act
    from_module = _enum(module, name)
    from_package = _enum(_package(module), name)

    # Assert
    assert from_module is from_package


@pytest.mark.parametrize(("module", "name", "member"), ENUMS)
def test_an_old_enum_still_looks_up_in_any_case(
    module: str, name: str, member: str
) -> None:
    """`MetricsExporterType("Prometheus")` keeps returning the member."""
    # Arrange
    enum = _enum(module, name)

    # Act
    found = enum(str(enum[member]).swapcase())

    # Assert
    assert found is enum[member]


@pytest.mark.parametrize("value", [1, "no-such-choice"])
@pytest.mark.parametrize(("module", "name", "_member"), ENUMS)
def test_an_old_enum_refuses_an_unknown_value(
    module: str, name: str, _member: str, value: object
) -> None:
    """A lookup by a non-string or an unknown string matches no member."""
    # Arrange
    enum = _enum(module, name)

    # Act / Assert
    with pytest.raises(ValueError, match="is not a valid"):
        enum(value)


@pytest.mark.parametrize(
    "module",
    [
        "grelmicro.log",
        "grelmicro.log.config",
        "grelmicro.metrics.config",
        "grelmicro.trace.config",
    ],
)
def test_an_unknown_name_still_raises_attribute_error(module: str) -> None:
    """The module `__getattr__` serves the old enums and nothing else."""
    # Arrange
    source = importlib.import_module(module)

    # Act / Assert
    with pytest.raises(AttributeError, match=f"module '{module}' has no attr"):
        source.missing_name  # noqa: B018


@pytest.mark.parametrize(("module", "name", "_member"), ENUMS)
def test_the_old_enum_warns_and_the_new_type_does_not(
    module: str, name: str, _member: str
) -> None:
    """The removal named in the message is the one the old name warns with."""
    # Arrange
    package = importlib.import_module(_package(module))
    new_module, new_name = NEW_TYPES[name]
    new = importlib.import_module(new_module)

    # Act / Assert
    assert_future_warning(
        MODE_ENUMS[name],
        default=lambda: getattr(package, name),
        explicit=lambda: getattr(new, new_name),
    )
    assert f"`{new_name}`" in MODE_ENUMS[name].message


def test_every_old_enum_is_a_pending_removal() -> None:
    """The stale-entry guard covers each alias, so none outlives 0.44.0."""
    # Act
    names = {name for _, name, _ in ENUMS}
    removals = [*MODE_ENUMS.values(), *MEMBER_ATTRIBUTES.values()]

    # Assert
    assert set(MODE_ENUMS) == names
    assert set(MEMBER_ATTRIBUTES) == {
        (name, attribute) for name in names for attribute in ("value", "name")
    }
    assert all(removal in PENDING for removal in removals)
    assert {removal.removed_in for removal in removals} == {"0.44.0"}


@pytest.mark.parametrize(("module", "name", "_member"), ENUMS)
def test_an_old_enum_is_deprecated_for_checkers(
    module: str, name: str, _member: str
) -> None:
    """`@deprecated` carries the runtime message, and adds no warning of its own."""
    # Arrange
    enum = _enum(module, name)

    # Act / Assert
    assert enum.__deprecated__ == MODE_ENUMS[name].message  # type: ignore[attr-defined]  # ty: ignore[unresolved-attribute]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        list(enum)


@pytest.mark.parametrize(("module", "name", "member"), ENUMS)
def test_reading_value_of_a_member_warns_at_the_caller(
    module: str, name: str, member: str
) -> None:
    """`.value` returns the string and warns it is gone from 0.44.0."""
    # Arrange
    item = _enum(module, name)[member]
    message = re.escape(MEMBER_ATTRIBUTES[name, "value"].message)

    # Act
    with pytest.warns(GrelmicroFutureWarning, match=message) as caught:
        value = item.value

    # Assert
    assert value == item
    assert type(value) is str
    assert caught[0].filename == __file__


@pytest.mark.parametrize(("module", "name", "member"), ENUMS)
def test_reading_name_of_a_member_warns_at_the_caller(
    module: str, name: str, member: str
) -> None:
    """`.name` returns the member name and warns it is gone from 0.44.0."""
    # Arrange
    item = _enum(module, name)[member]
    message = re.escape(MEMBER_ATTRIBUTES[name, "name"].message)

    # Act
    with pytest.warns(GrelmicroFutureWarning, match=message) as caught:
        found = item.name

    # Assert
    assert found == member
    assert caught[0].filename == __file__


def test_the_member_attribute_message() -> None:
    """The message names the attribute and what to use instead."""
    assert MEMBER_ATTRIBUTES["LogLevelType", "value"].message == (
        "`LogLevelType.value` is removed in 0.44.0. "
        "Use the member as a plain `str` instead."
    )


class _LevelModel(BaseModel):
    """A model with a `Literal` field, as a user would declare one."""

    level: Literal["DEBUG", "INFO"]


def _silent_uses(enum: type[StrEnum], member: str) -> None:
    """Use a member every way that does not read `.value` or `.name`."""
    item = enum[member]
    text = item._value_
    assert item == text
    assert hash(item) == hash(enum[member])
    assert str(item) == text
    assert f"{item}" == text
    assert member in repr(item)
    assert enum(text) is item
    assert enum(text.swapcase()) is item
    assert item in list(enum)
    assert {text: 1}[item] == 1
    assert json.dumps(item) == json.dumps(text)
    assert pickle.loads(pickle.dumps(item)) is item  # noqa: S301
    assert copy.deepcopy(item) is item


@pytest.mark.parametrize(("module", "name", "member"), ENUMS)
def test_every_other_use_of_a_member_is_silent(
    module: str, name: str, member: str
) -> None:
    """Comparing, hashing, printing and serializing a member warn nothing."""
    # Arrange
    enum = _enum(module, name)

    # Act / Assert
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _silent_uses(enum, member)


@pytest.mark.parametrize(("config", "field", "_any", "stored"), CHOICES)
def test_a_mode_field_dumps_to_json_silently(
    config: type[BaseModel], field: str, _any: str, stored: str
) -> None:
    """A config dumps its mode fields as strings, without reading `.value`."""
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


def test_a_member_in_a_literal_field_is_silent() -> None:
    """Pydantic and `match` take a member without reading `.value`."""
    # Arrange
    members = SimpleNamespace(
        INFO=_enum("grelmicro.log.config", "LogLevelType")["INFO"]
    )

    # Act / Assert
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        model = _LevelModel(level=members.INFO)
        assert model.model_dump() == {"level": "INFO"}
        assert model.model_dump_json() == '{"level":"INFO"}'
        match "INFO":
            case members.INFO:
                pass
            case _:  # pragma: no cover
                pytest.fail("match missed the member")


def test_a_circuit_breaker_log_level_matches_any_case() -> None:
    """The breaker's `log_level` reads `error` as `ERROR`, like `LogConfig`."""
    # Act
    config = ConsecutiveCountConfig(log_level="error")  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

    # Assert
    assert config.log_level == "ERROR"


@pytest.mark.parametrize("callable_", [Metrics, Trace, Log, configure])
def test_a_widened_signature_names_only_pending_enums(
    callable_: Callable[..., object],
) -> None:
    """A signature names an old enum only while its removal is pending.

    Each mode parameter also takes the old enum for type checkers until
    0.44.0. Once `MODE_ENUMS` drops an entry, a signature still naming it
    fails here.
    """
    # Arrange
    target = callable_.__init__ if isinstance(callable_, type) else callable_

    # Act
    annotations = annotationlib.get_annotations(
        target, format=annotationlib.Format.STRING
    )
    named = {
        name
        for annotation in annotations.values()
        for name in re.findall(r"\b\w+Type\b", annotation)
    }

    # Assert
    assert named
    assert named <= set(MODE_ENUMS)


OTHER_CASE_TIERS = [
    ("Production", "production"),
    ("STAGING", "staging"),
    ("Test", "test"),
    ("DEVELOPMENT", "development"),
]
"""Each declared tier in another case, and the tier it names."""


@pytest.mark.parametrize(("spelling", "tier"), OTHER_CASE_TIERS)
def test_an_environment_variable_in_another_case_warns_ahead_of_the_switch(
    monkeypatch: pytest.MonkeyPatch, spelling: str, tier: str
) -> None:
    """`GREL_ENVIRONMENT=Production` reads as undeclared and warns it switches."""
    # Arrange
    entry = environment_case(spelling, tier)

    def other_case() -> Grelmicro:
        monkeypatch.setenv("GREL_ENVIRONMENT", spelling)
        with pytest.warns(GrelmicroConfigWarning, match="is not one of"):
            return Grelmicro()

    def exact_case() -> Grelmicro:
        monkeypatch.setenv("GREL_ENVIRONMENT", tier)
        return Grelmicro()

    # Act / Assert
    assert_future_warning(entry, default=other_case, explicit=exact_case)
    assert entry.message == (
        f"`GREL_ENVIRONMENT={spelling}` reads as undeclared today and as "
        f"`{tier}` from 0.44.0. Set `GREL_ENVIRONMENT={tier}` now."
    )


@pytest.mark.parametrize(("spelling", "tier"), OTHER_CASE_TIERS)
def test_an_environment_variable_in_another_case_reads_as_undeclared(
    monkeypatch: pytest.MonkeyPatch, spelling: str, tier: str
) -> None:
    """Until 0.44.0 the value still names no tier."""
    # Arrange
    monkeypatch.setenv("GREL_ENVIRONMENT", spelling)

    # Act
    with (
        pytest.warns(GrelmicroFutureWarning, match=f"`{tier}`"),
        pytest.warns(GrelmicroConfigWarning),
    ):
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


def test_the_environment_change_is_pending_until_0_44() -> None:
    """The stale-entry guard covers the switch."""
    # Assert
    assert ENVIRONMENT_CASE in PENDING
    assert ENVIRONMENT_CASE.switch_in == "0.44.0"


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
