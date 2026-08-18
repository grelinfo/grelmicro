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
from tests._contract_support import (
    call_targets,
    called_names,
    module_name,
)

PACKAGE_ROOT = Path(grelmicro.__file__).parent

_MIN_DOORS = 25
"""Exact count today, so a door dropping out of discovery fails.

Slack would hide erosion. `_discover_from_config` reads `node.body`, so
moving `from_config` onto a shared base would remove those classes from the
sweep, and a floor with room to spare would stay green while it happened.
"""


def _discover_from_config() -> list[tuple[str, str]]:
    """Return `(module, class)` for every class defining `from_config`."""
    found: list[tuple[str, str]] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = module_name(path)
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

IDS = [name for module, name in DOORS]


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


def _module_functions(module: Any) -> dict[str, str]:  # noqa: ANN401
    """Return `{name: source}` for every function the module defines."""
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(module)))
    except (OSError, TypeError):  # pragma: no cover  # no readable source
        return {}
    # Module level only, deliberately. A method such as `_setup` is shared
    # by both construction doors and builds the env prefix for the door that
    # needs it, so following it would report a path `from_config` never
    # takes. A bypass helper realistically lives at module level, which is
    # the case this closes.
    return {
        node.name: ast.unparse(node)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _imported_helper(module: Any, name: str) -> tuple[str, Any] | None:  # noqa: ANN401
    """Return a grelmicro function imported into `module`, with its module.

    Following only same-module helpers left the obvious bypass open: put
    the read in a neighbouring grelmicro module and import it. Only
    first-party functions are followed, so the walk stops at the package
    boundary instead of descending into pydantic or the stdlib.

    The function is unwrapped first. `lru_cache` and friends return a
    non-function, and `_build_settings_cls` is exactly that shape: a cached
    first-party helper that builds an environment-reading settings class.
    """
    target = getattr(module, name, None)
    target = getattr(target, "__wrapped__", target)
    if not inspect.isfunction(target):
        return None
    origin = getattr(target, "__module__", "")
    if not origin.startswith("grelmicro"):
        return None
    try:
        source = textwrap.dedent(inspect.getsource(target))
    except (OSError, TypeError):  # pragma: no cover  # no readable source
        return None
    return source, sys.modules.get(origin, module)


def _env_names_reached(source: str, module: Any) -> set[str]:  # noqa: ANN401
    """Return the env-reading names `source` reaches, following its callees.

    Checking only `from_config`'s own body was not enough: moving the read
    one function away passed the sweep while breaking R8. Helpers are
    followed transitively, whether the module defines them or imports them
    from elsewhere in grelmicro.
    """
    seen: set[str] = set()
    pending: list[tuple[str, Any]] = [(source, module)]
    found: set[str] = set()
    while pending:
        body, owner = pending.pop()
        tree = ast.parse(body)
        found |= called_names(tree) & ENV_READERS
        # Each body resolves names in *its own* module. Keeping the original
        # module pinned made the walk one hop deep, so a neighbour that
        # delegated once more stayed invisible.
        functions = _module_functions(owner)
        for name in sorted(call_targets(tree) - seen):
            seen.add(name)
            if name in functions:
                pending.append((functions[name], owner))
                continue
            found_helper = _imported_helper(owner, name)
            if found_helper is not None:
                pending.append(found_helper)
    return found


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
    ids=[name for module, name in ENV_FREE_DOORS],
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
    reached = _env_names_reached(source, sys.modules[cls.__module__])
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


_RELOAD_ADDRESS = "_env_prefix"
"""The one attribute the declarative door is expected to leave unset.

It is the address live reload uses to find an instance, so omitting it is
the mechanism behind R8 rather than an oversight.
"""


def _calls_the_constructor(source: str) -> bool:
    """Return whether the source contains a real `cls(...)` call."""
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "cls"
        for node in ast.walk(ast.parse(textwrap.dedent(source)))
    )


def _assigned_attributes(cls: Any, method: str) -> set[str]:  # noqa: ANN401
    """Return the `self.<name>` attributes a method assigns."""
    function = getattr(cls, method, None)
    if function is None:
        return set()
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    except (OSError, TypeError):  # pragma: no cover  # no readable source
        return set()
    return {
        target.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        if isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    }


@pytest.mark.parametrize(("module_name", "class_name"), DOORS, ids=IDS)
def test_both_doors_leave_the_same_attributes(
    module_name: str, class_name: str
) -> None:
    """Everything `__init__` sets, the declarative door sets too.

    `from_config` skips `__init__` entirely through `__new__` plus
    `_setup`, so an attribute assigned only in `__init__` is missing on
    every instance built declaratively, and fails at the first use rather
    than at construction. #755 records this as audited by hand.

    Read from the source rather than by constructing, so all of the family
    is covered instead of the few whose arguments are easy to supply. The
    reload address is exempt: the declarative door omits it on purpose,
    which is R8 in structural form.
    """
    cls = _resolve(module_name, class_name)
    source = inspect.getsource(cls.from_config)
    if "_setup" not in source:
        # Asserted rather than skipped. A door that builds through the
        # ordinary constructor satisfies the invariant by construction, and
        # saying so is worth more than a skip line that records nothing.
        # Parsed, not grepped. A substring would match `cls(` inside a
        # docstring or a comment, and `return cls` would admit
        # `return cls._instances[name]`, an object that never ran
        # `__init__` and the exact case this rejects.
        assert _calls_the_constructor(source), (
            f"{class_name}.from_config neither uses _setup nor calls "
            f"cls(...), so which attributes it leaves is unknown"
        )
        return
    only_in_init = (
        _assigned_attributes(cls, "__init__")
        - _assigned_attributes(cls, "_setup")
        - {_RELOAD_ADDRESS}
    )
    assert not only_in_init, (
        f"{class_name}.__init__ sets {sorted(only_in_init)} which _setup "
        f"does not, so from_config leaves the instance incomplete"
    )
