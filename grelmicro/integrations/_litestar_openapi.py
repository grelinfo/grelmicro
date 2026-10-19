"""Describing the components in the OpenAPI schema Litestar builds.

Litestar builds the schema as an `OpenAPI` object, reached through
`app.openapi_schema`, and renders every document it serves from it. That
object is edited in place: each component's edits run on its rendering,
and what they add or change is carried back as Litestar's own spec
objects. A change made to `app.openapi_schema` reaches the served
document, as it does on any Litestar app.

The schema is described at `install`, so `app.openapi_schema` says it
before the app starts, and again each time a route registered later makes
Litestar build it anew, before or after the app started.
"""

from __future__ import annotations

import copy
import enum
import types
import typing
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import fields
from typing import TYPE_CHECKING, Any, Final

from litestar.openapi import spec
from litestar.openapi.spec import BaseSchemaObject, Reference
from litestar.plugins import ReceiveRoutePlugin

from grelmicro.integrations._litestar_internals import watch_routes

if TYPE_CHECKING:
    from litestar import Litestar
    from litestar.openapi.spec import OpenAPI
    from litestar.routes import BaseRoute

Edit = Callable[[dict[str, Any]], None]
"""An edit a component makes to the rendered schema, in place."""

_STATE: Final = "grelmicro_openapi"
"""Where the app's state keeps what describes its schema."""

_MISSING: Final[Any] = object()
"""Marks a member the rendered schema did not carry."""

_UNIONS: Final = (typing.Union, types.UnionType)
"""How a union type reports its origin."""

_MAPPINGS: Final = (dict, Mapping)
"""How a mapping type reports its origin."""

_SEQUENCES: Final = (list, Sequence)
"""How a sequence type reports its origin."""

_MEMBERS: Final[dict[type, dict[str, tuple[str, Any]]]] = {}
"""Each spec class's members, read once from how Litestar renders it."""


class _Described:
    """The edits that describe an app's schema, and how far they reached."""

    __slots__ = ("applied", "edits", "openapi")

    def __init__(self) -> None:
        self.edits: dict[Hashable, Edit] = {}
        self.openapi: OpenAPI | None = None
        self.applied = 0


class _Redescribing(ReceiveRoutePlugin):
    """Describes the schema Litestar builds anew for each route registered."""

    __slots__ = ("app", "described")

    def __init__(self, app: Litestar, described: _Described) -> None:
        self.app = app
        self.described = described

    def receive_route(self, route: BaseRoute) -> None:  # noqa: ARG002
        """Describe the schema the registered route makes Litestar build."""
        _describe(self.app, self.described)


def add_openapi_edit(
    app: Litestar,
    edit: Edit,
    *,
    key: Hashable,
    describe: bool = True,
) -> None:
    """Apply `edit` to every schema Litestar builds for the app.

    Edits run in the order they were added, one per `key`, so adding a
    second under the same key adds nothing. The current schema is given
    the edits it lacks now, unless `describe` is false because another
    edit follows within the same `install`, and every schema a route
    registered later makes Litestar build is given them all. An app built
    with `openapi_config=None` publishes no schema, so nothing is added.

    Raises:
        RuntimeError: When Litestar moved its route plugins.
    """
    if app.openapi_config is None:
        return
    described: _Described | None = getattr(app.state, _STATE, None)
    if described is None:
        described = _Described()
        watch_routes(app, _Redescribing(app, described))
        setattr(app.state, _STATE, described)
    described.edits.setdefault(key, edit)
    if describe:
        _describe(app, described)


def _describe(app: Litestar, described: _Described) -> None:
    """Apply each edit the current schema object has not been given yet.

    A schema Litestar built anew is given every edit, in order.
    """
    openapi = app.openapi_schema
    if openapi is not described.openapi:
        described.openapi, described.applied = openapi, 0
    pending = list(described.edits.values())[described.applied :]
    if not pending:
        return
    rendered = openapi.to_schema()
    edited = copy.deepcopy(rendered)
    for edit in pending:
        edit(edited)
    _merge(openapi, rendered, edited, type(openapi))
    described.applied = len(described.edits)


def _merge(node: Any, before: Any, after: Any, hint: Any) -> Any:  # noqa: ANN401
    """Return `node`, which renders as `before`, edited to render as `after`.

    What is unchanged keeps the object Litestar built, and only what an
    edit added or changed is built anew.

    Raises:
        TypeError: When an edit adds a member no field of the spec class
            holds, such as an `x-` extension, naming it.
    """
    if before is not _MISSING and before == after:
        return node
    edited = isinstance(after, dict) and isinstance(before, dict)
    if (
        edited
        and isinstance(node, BaseSchemaObject)
        and _holds(node, before, after)
    ):
        # Still the class it was, such as a response gaining a header. One
        # an edit turned into another, such as a schema into a `$ref`, is
        # built anew below.
        members = _members(type(node))
        for key in (before.keys() - after.keys()) & members.keys():
            setattr(node, members[key][0], None)
        for key, value in after.items():
            if key not in members:
                # A member the node renders itself, such as one an app's
                # `operation_class` adds, unchanged by every edit.
                continue
            name, member_hint = members[key]
            setattr(
                node,
                name,
                _merge(
                    getattr(node, name),
                    before.get(key, _MISSING),
                    value,
                    member_hint,
                ),
            )
        return node
    if edited and isinstance(node, dict):
        value_hint = _value_hint(hint)
        for key in before.keys() - after.keys():
            node.pop(key, None)
        for key, value in after.items():
            node[key] = _merge(
                node.get(key), before.get(key, _MISSING), value, value_hint
            )
        return node
    if (
        isinstance(after, list)
        and isinstance(before, list)
        and isinstance(node, list)
        and len(node) == len(before)
    ):
        item_hint = _value_hint(hint)
        return [
            _merge(node[index], before[index], value, item_hint)
            if index < len(before)
            else _build(item_hint, value)
            for index, value in enumerate(after)
        ]
    return _build(hint, after)


def _holds(node: BaseSchemaObject, before: dict[str, Any], after: Any) -> bool:  # noqa: ANN401
    """Return whether `node` can render as `after` once its fields are edited.

    Every member an edit added or changed needs a field. One the node
    rendered itself, unchanged, stays as the node renders it.
    """
    members = _members(type(node))
    return all(
        key in members or before.get(key, _MISSING) == value
        for key, value in after.items()
    )


def _build(hint: Any, value: Any) -> Any:  # noqa: ANN401
    """Return `value`, a rendered member, as the spec object `hint` names.

    Raises:
        TypeError: When the spec class has no field for a member, naming it.
    """
    origin = typing.get_origin(hint)
    if origin in _UNIONS:
        return _build(_pick(typing.get_args(hint), value), value)
    if _is_spec(hint):
        members = _members(hint)
        unknown = sorted(value.keys() - members.keys())
        if unknown:
            msg = (
                f"Litestar's {hint.__name__} has no field for "
                f"{', '.join(unknown)}, so app.openapi_schema cannot carry it."
            )
            raise TypeError(msg)
        return hint(
            **{
                members[key][0]: _build(members[key][1], member)
                for key, member in value.items()
            }
        )
    if isinstance(hint, type) and issubclass(hint, enum.Enum):
        return hint(value)
    if isinstance(value, dict) and origin in _MAPPINGS:
        value_hint = _value_hint(hint)
        return {
            key: _build(value_hint, member) for key, member in value.items()
        }
    if isinstance(value, list) and origin in _SEQUENCES:
        item_hint = _value_hint(hint)
        return [_build(item_hint, member) for member in value]
    return value


def _pick(options: tuple[Any, ...], value: Any) -> Any:  # noqa: ANN401
    """Return the member of a union that a rendered value is an instance of.

    A mapping is a `Reference` when it carries `$ref`, else the spec class
    or the mapping the union names. A sequence is the sequence it names,
    and a scalar the enum holding it. Anything else is kept as it is.
    """
    # A spec class first, so a member typed as a class or a mapping is built
    # as the class.
    ordered = sorted(options, key=lambda option: not _is_spec(option))
    return next((option for option in ordered if _fits(option, value)), Any)


def _fits(option: Any, value: Any) -> bool:  # noqa: ANN401
    """Return whether a rendered value is an instance of one member of a union."""
    if isinstance(value, dict):
        if "$ref" in value:
            return option is Reference
        return (_is_spec(option) and option is not Reference) or (
            typing.get_origin(option) in _MAPPINGS
        )
    if isinstance(value, list):
        return typing.get_origin(option) in _SEQUENCES
    return (
        isinstance(option, type)
        and issubclass(option, enum.Enum)
        and value in {member.value for member in option}
    )


def _is_spec(option: Any) -> bool:  # noqa: ANN401
    """Return whether a type is one of Litestar's spec classes."""
    return isinstance(option, type) and issubclass(option, BaseSchemaObject)


def _value_hint(hint: Any) -> Any:  # noqa: ANN401
    """Return what a mapping holds as values, or a sequence as items."""
    if typing.get_origin(hint) in _UNIONS:
        hint = next(
            (
                option
                for option in typing.get_args(hint)
                if typing.get_origin(option) in (*_MAPPINGS, *_SEQUENCES)
            ),
            Any,
        )
    arguments = typing.get_args(hint)
    return arguments[-1] if arguments else Any


def _members(cls: type[BaseSchemaObject]) -> dict[str, tuple[str, Any]]:
    """Return each member a spec class renders, as its field name and type.

    The key of each field is read from Litestar's own rendering of an
    object of its spec class holding that field alone. The spec class is
    the one `litestar.openapi.spec` defines, so a subclass of an app's,
    such as an `operation_class`, renders nothing of its own here, and its
    own fields have no member. A field the class renders only beside
    another, such as a query parameter's `allowEmptyValue`, has none either.
    """
    found = _MEMBERS.get(cls)
    if found is not None:
        return found
    litestar_class = typing.cast(
        "type[BaseSchemaObject]",
        next(
            kind
            for kind in cls.__mro__
            if kind.__module__.startswith(spec.__name__)
        ),
    )
    hints = typing.get_type_hints(litestar_class, localns=vars(spec))
    probe = litestar_class.__new__(litestar_class)
    for field in fields(litestar_class):
        setattr(probe, field.name, None)
    found = _MEMBERS[cls] = {}
    for field in fields(litestar_class):
        setattr(probe, field.name, True)
        for key in probe.to_schema():
            found[key] = (field.name, hints[field.name])
        setattr(probe, field.name, None)
    return found
