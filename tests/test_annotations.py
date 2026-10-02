"""Every annotation in grelmicro evaluates at runtime.

Python 3.14 evaluates an annotation when it is read, so `inspect.signature`,
`help()`, `typing.get_type_hints` and FastAPI raise `NameError` on a name
that is only imported under `TYPE_CHECKING`. A module either keeps its
annotations as strings with `from __future__ import annotations`, or imports
every name its annotations use.
"""

from __future__ import annotations

import annotationlib
import importlib
import inspect
import pkgutil
from typing import Any

import grelmicro


def _annotated(module: Any) -> list[tuple[str, Any]]:  # noqa: ANN401
    """Return every function, class and method `module` defines."""
    found: list[tuple[str, Any]] = []
    for name, value in vars(module).items():
        if getattr(value, "__module__", None) != module.__name__:
            continue
        if inspect.isfunction(value):
            found.append((name, value))
        elif inspect.isclass(value):
            found.append((name, value))
            for member_name, member in vars(value).items():
                if isinstance(member, staticmethod | classmethod):
                    function = member.__func__
                elif isinstance(member, property):
                    function = member.fget
                else:
                    function = member
                if inspect.isfunction(function):
                    found.append((f"{name}.{member_name}", function))
    return found


def test_every_annotation_evaluates() -> None:
    """No annotation names something only a type checker imports."""
    unresolved = []
    for info in pkgutil.walk_packages(grelmicro.__path__, "grelmicro."):
        module = importlib.import_module(info.name)
        for name, value in _annotated(module):
            try:
                annotationlib.get_annotations(
                    value, format=annotationlib.Format.VALUE
                )
            except NameError as error:
                unresolved.append(f"{info.name}.{name}: {error.name}")
    assert unresolved == []
