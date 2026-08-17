"""Every class with a config class offers one declarative door, and only one.

[API conventions](../docs/architecture/api-conventions.md) settles two rules
that hold for a family rather than for a class, which is exactly the shape a
per-class test cannot keep honest. Both were audited by hand during the 0.39.0
review and both turned up outliers, so they are swept here instead.

R8 in [Configuration internals](../docs/architecture/config.md) is the third:
`from_config` takes the config as-is, reads no variable, and does not register
the instance for live reload.
"""

import ast
import importlib
import inspect
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

import grelmicro
from grelmicro._config import reconfigurable_instances
from grelmicro.resilience import (
    Retry,
    RetryConfig,
    Timeout,
    TimeoutConfig,
)

PACKAGE_ROOT = Path(grelmicro.__file__).parent

_MIN_DOORS = 25
"""Exact count today, so a door dropping out of discovery fails.

Slack would hide erosion. `_discover_from_config` reads `node.body`, so
moving `from_config` onto a shared base would remove those classes from the
sweep, and a floor with room to spare would stay green while it happened.
"""


def _module_name(path: Path) -> str:
    """Return the importable module name for a source path.

    A package is named by its directory, never by its `__init__`. Importing
    `pkg.__init__` re-executes the module under a second name, so the sweep
    would check a duplicate class object that nobody imports, and re-run any
    registration the module does at import time.
    """
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    if relative.name == "__init__":
        relative = relative.parent
    dotted = relative.as_posix().replace("/", ".")
    return f"grelmicro.{dotted}" if dotted else "grelmicro"


def _discover_from_config() -> list[tuple[str, str]]:
    """Return `(module, class)` for every class defining `from_config`."""
    found: list[tuple[str, str]] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = _module_name(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name.startswith("_"):
                continue
            if any(
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == "from_config"
                for item in node.body
            ):
                found.append((module, node.name))
    return found


DOORS = _discover_from_config()

IDS = [f"{module}.{name}".rsplit(".", 2)[-1] for module, name in DOORS]


def test_the_sweep_finds_declarative_doors() -> None:
    """An empty or shrunken scan is a failure, never a silent pass."""
    assert len(DOORS) >= _MIN_DOORS, (
        f"expected at least {_MIN_DOORS} classes with from_config, got {DOORS}"
    )


def _resolve(module_name: str, class_name: str) -> Any:  # noqa: ANN401
    """Return the class, or skip when its optional dependency is absent."""
    try:
        module = importlib.import_module(module_name)
    except ImportError:  # pragma: no cover  # depends on installed extras
        pytest.skip(f"{module_name} needs an extra that is not installed")
    return getattr(module, class_name)


@pytest.mark.parametrize(("module_name", "class_name"), DOORS, ids=IDS)
def test_no_class_keeps_a_config_keyword(
    module_name: str, class_name: str
) -> None:
    """`from_config` is the one door for a pre-built config.

    A `config=` keyword alongside it is the second door this convention
    exists to remove: two ways to pass the same object, and a reader who has
    to learn which one the docs meant.
    """
    cls = _resolve(module_name, class_name)
    parameters = inspect.signature(cls.__init__).parameters
    assert "config" not in parameters, (
        f"{class_name}.__init__ takes config=, which duplicates from_config"
    )


@pytest.mark.parametrize(("module_name", "class_name"), DOORS, ids=IDS)
def test_from_config_takes_the_config_whole(
    module_name: str, class_name: str
) -> None:
    """The door takes the config object, never a field the config carries.

    The rule is not "one parameter". A name is the instance identity under
    R3, and a backend or a source is the thing the config is bound to, so
    both legitimately sit alongside. What it forbids is `from_config`
    growing a parameter that duplicates a config field, which would give
    that one setting two doors and no answer for which wins.
    """
    cls = _resolve(module_name, class_name)
    signature = inspect.signature(cls.from_config)
    assert "config" in signature.parameters, (
        f"{class_name}.from_config takes no config parameter"
    )
    config_classes = _config_classes(cls)
    assert config_classes, (
        f"{class_name}.from_config declares a config type that does not "
        f"resolve, so the rule cannot be checked"
    )
    fields: set[str] = set()
    for config_cls in config_classes:
        fields |= set(config_cls.model_fields)
    duplicated = sorted(set(signature.parameters) & fields)
    assert not duplicated, (
        f"{class_name}.from_config takes {duplicated}, which the config "
        f"already carries, so that setting has two doors"
    )


ENV_FREE_DOORS = [
    (module, name)
    for module, name in DOORS
    # `Provider` reads its vendor namespace ungated under R1, so it is not
    # governed by the `GREL_ENV_LOAD` gate this rule is about.
    if "providers" not in module
]


ENV_READERS = frozenset(
    {
        "environ",
        "getenv",
        "env_load_default",
        "env_prefixes",
        "default_env_prefix",
        "resolve_config",
        "resolve_config_from_mapping",
        "warn_ignored_env",
    }
)
"""Every name that reaches the environment, not a sample of three.

Grepping for `os.environ` alone passed a `from_config` that called
`env_load_default()`, which is an exported helper doing exactly what R8
forbids. The check walks the call graph for any of these instead.
"""


@pytest.mark.parametrize(
    ("module_name", "class_name"),
    ENV_FREE_DOORS,
    ids=[
        f"{module}.{name}".rsplit(".", 2)[-1] for module, name in ENV_FREE_DOORS
    ],
)
def test_from_config_reads_no_environment_variable(
    module_name: str,
    class_name: str,
) -> None:
    """R8: the config is taken as-is, whatever the environment says.

    Read from the source rather than by construction, because building
    every class here would need each one's backend. Names are collected
    from the whole call expression, so an attribute (`os.environ`) and a
    bare call (`env_load_default()`) are both caught.
    """
    cls = _resolve(module_name, class_name)
    try:
        source = textwrap.dedent(inspect.getsource(cls.from_config))
    except (OSError, TypeError):  # pragma: no cover  # C or builtin
        pytest.skip(f"{class_name}.from_config has no readable source")
    reached = {
        name
        for node in ast.walk(ast.parse(source))
        for name in (
            getattr(node, "id", None),
            getattr(node, "attr", None),
        )
        if name in ENV_READERS
    }
    assert not reached, (
        f"{class_name}.from_config reaches {sorted(reached)}, but R8 says a "
        f"pre-built config is taken as-is, with no variable read"
    )


class _LazyNamespace(dict):  # type: ignore[type-arg]
    """Module globals that also reach names exposed by `__getattr__`.

    A config union declared under `TYPE_CHECKING` and served lazily is
    absent from `vars(module)`, so evaluating the annotation against it
    alone would skip that class instead of checking it.
    """

    def __init__(self, module: Any) -> None:  # noqa: ANN401
        """Seed from the module's real globals."""
        super().__init__(vars(module))
        self._module = module

    def __missing__(self, key: str) -> Any:  # noqa: ANN401
        """Fall back to the module's lazy attribute access."""
        try:
            return getattr(self._module, key)
        except AttributeError as error:
            raise KeyError(key) from error


def _config_classes(cls: Any) -> list[Any]:  # noqa: ANN401
    """Return every pydantic config class `from_config` declares.

    Only the `config` annotation is evaluated. Resolving the whole
    signature fails on the siblings, which name backends and serializers
    imported under `TYPE_CHECKING`, and that failure would silently skip a
    third of the family rather than check it.
    """
    from pydantic import BaseModel  # noqa: PLC0415

    raw = inspect.get_annotations(cls.from_config).get("config")
    namespace = _LazyNamespace(sys.modules[cls.__module__])
    if isinstance(raw, str):
        try:
            raw = eval(raw, namespace)  # noqa: S307
        except Exception:  # noqa: BLE001  # pragma: no cover  # exotic forms
            return []
    seen: list[Any] = [raw]
    found: list[Any] = []
    while seen:
        candidate = seen.pop()
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            found.append(candidate)
            continue
        seen.extend(getattr(candidate, "__args__", ()) or ())
        metadata = getattr(candidate, "__metadata__", ())
        seen.extend(metadata)
    # Every arm, not the first: a union's other arm carries fields too, and
    # a parameter duplicating one of those is the same second door.
    return found


def test_from_config_does_not_register_for_live_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8: an instance built from a config is not reconfigured behind you.

    Registering it would let a mounted file overwrite the object the caller
    passed in code, which is the opposite of what the declarative door
    promises.
    """
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    # Bound to locals on purpose. `_reconfigurables` is a `WeakSet`, so an
    # unbound instance is collected before the assertion runs and a count
    # comparison passes whether or not `from_config` registered.
    timeout = Timeout.from_config("door-timeout", TimeoutConfig(seconds=1))
    retry = Retry.from_config("door-retry", RetryConfig(when=ValueError))
    registered = reconfigurable_instances()
    assert timeout not in registered, (
        "Timeout.from_config registered the instance for live reload, so a "
        "mounted file could overwrite a config passed in code"
    )
    assert retry not in registered, (
        "Retry.from_config registered the instance for live reload, so a "
        "mounted file could overwrite a config passed in code"
    )
