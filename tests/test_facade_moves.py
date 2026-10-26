"""The names an integration module served in 0.43 keep loading, with a warning.

`from grelmicro.integrations.fastapi import RateLimiter` worked in 0.43.1
because the module imported the class for its own use. Each such name now
loads from its home on access and warns that it leaves the module in 0.45.0.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import warnings
from typing import TYPE_CHECKING

import pytest

from grelmicro._deprecation import FACADE_MOVES
from grelmicro.errors import Grelmicro045Warning

if TYPE_CHECKING:
    from types import ModuleType

_SERVED_IN_0_43: dict[str, frozenset[str]] = {
    "grelmicro.integrations.fastapi": frozenset(
        {
            "ErrorResponses",
            "JWTClaims",
            "Metrics",
            "PROMETHEUS_MEDIA_TYPE",
            "PreconditionRequiredError",
            "Principal",
            "RateLimitExceededError",
            "RateLimiter",
            "Requirement",
            "Trace",
            "TrustedProxies",
            "VerifiedToken",
            "add_error_schema",
            "bucket_of",
            "check_freshness",
            "check_route_limiters",
            "declarations_of",
            "declare_anonymous",
            "declare_cached",
            "declare_from_request",
            "declare_optional",
            "declare_precondition_required",
            "describe_app_schema",
            "describing",
            "exceptions_on_spans",
            "excluding",
            "gate_routes",
            "is_authenticated",
            "is_selected",
            "normal_close",
            "referenced",
            "render_prometheus",
            "requirement_for",
            "shadowed_routes",
            "spend",
            "state_on",
        }
    ),
    "grelmicro.integrations.starlette": frozenset(
        {
            "AUTHENTICATED",
            "BODYLESS_STATUSES",
            "ErrorResponses",
            "HANDLED",
            "RateLimitMiddleware",
            "Requirement",
            "VerifiedToken",
            "declarations_of",
            "declared_scopes",
            "gate_routes",
            "merge_headers",
            "refuse_registered",
            "request_spans",
            "shadowed_routes",
        }
    ),
    "grelmicro.integrations.litestar": frozenset(
        {
            "ANONYMOUS_OPT",
            "AUTHENTICATED",
            "AuthenticatedRequestsMiddleware",
            "BODYLESS_STATUSES",
            "ErrorResponses",
            "HANDLED",
            "METADATA_MARKER",
            "RateLimitMiddleware",
            "Requirement",
            "RouteDeclaration",
            "add_error_schema",
            "add_openapi_edit",
            "declared_scopes",
            "describe_app_schema",
            "describing",
            "merge_headers",
            "read_route",
            "refuse_anonymous_caller",
            "refuse_routes_at_metadata",
            "request_spans",
            "resource_metadata_of",
            "template_under_root",
        }
    ),
}
"""The public names each module served in 0.43.1 and no longer lists.

Computed from `dir()` of each module on 0.43.1, without the leading
underscore names and the standard library modules and functions.
"""

_CASES = [
    (module, name)
    for module, names in sorted(_SERVED_IN_0_43.items())
    for name in sorted(names)
]


def _facade(name: str) -> ModuleType:
    return importlib.import_module(name)


@pytest.mark.parametrize("module", sorted(_SERVED_IN_0_43))
def test_table_holds_every_name_served_in_0_43(module: str) -> None:
    """Each module lists exactly the names it served in 0.43.1 and dropped."""
    assert set(FACADE_MOVES[module].homes) == _SERVED_IN_0_43[module]


@pytest.mark.parametrize(("module", "name"), _CASES)
def test_moved_name_loads_its_home_object_with_a_warning(
    module: str, name: str
) -> None:
    """The name is its home module's object and warns at the caller."""
    # Arrange
    facade = _facade(module)
    home = FACADE_MOVES[module].homes[name]

    # Act
    with pytest.warns(Grelmicro045Warning) as caught:
        value = getattr(facade, name)

    # Assert
    assert value is getattr(importlib.import_module(home), name)
    assert len(caught) == 1
    message = str(caught[0].message)
    assert message.startswith(f"`{name}` moves out of `{module}` in 0.45.0. ")
    assert f"`{home}`" in message
    assert caught[0].filename == __file__


@pytest.mark.parametrize(("module", "name"), _CASES)
def test_moved_name_stays_off_the_public_surface(
    module: str, name: str
) -> None:
    """The name is not in `__all__`, `dir()`, the lazy table or the namespace."""
    # Arrange
    facade = _facade(module)

    # Act
    with pytest.warns(Grelmicro045Warning):
        getattr(facade, name)

    # Assert
    assert name not in facade.__all__
    assert name not in facade._LAZY
    assert name not in dir(facade)
    assert name not in vars(facade)


def test_from_import_warns_at_the_importing_line() -> None:
    """`from ... import` reports the line of the import statement."""
    with pytest.warns(Grelmicro045Warning) as caught:
        from grelmicro.integrations.fastapi import (  # noqa: PLC0415
            RateLimiter,
        )

    assert (
        RateLimiter
        is importlib.import_module("grelmicro.resilience").RateLimiter
    )
    assert caught[0].filename == __file__


def test_public_home_message_names_the_module_to_import_from() -> None:
    """A name with a public home says where to import it from."""
    moved = FACADE_MOVES["grelmicro.integrations.fastapi"]

    assert moved.message_for("RateLimiter") == (
        "`RateLimiter` moves out of `grelmicro.integrations.fastapi` in "
        "0.45.0. Import it from `grelmicro.resilience` instead."
    )


def test_private_home_message_says_the_name_is_private() -> None:
    """A name with no public home says so and names where it lives."""
    moved = FACADE_MOVES["grelmicro.integrations.fastapi"]

    assert moved.message_for("spend") == (
        "`spend` moves out of `grelmicro.integrations.fastapi` in 0.45.0. "
        "It is private and lives in `grelmicro.http._ratelimit`."
    )


@pytest.mark.parametrize("module", sorted(_SERVED_IN_0_43))
def test_star_import_binds_no_moved_name(module: str) -> None:
    """`from module import *` binds `__all__` alone and warns for none."""
    # Arrange
    namespace: dict[str, object] = {}

    # Act
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        exec(f"from {module} import *", namespace)  # noqa: S102

    # Assert
    assert not _SERVED_IN_0_43[module] & set(namespace)


@pytest.mark.parametrize("module", sorted(_SERVED_IN_0_43))
def test_unknown_name_still_raises_attribute_error(module: str) -> None:
    """A name the module never served raises `AttributeError` naming it."""
    facade = _facade(module)

    with pytest.raises(AttributeError, match=f"module '{module}' has no attr"):
        facade.missing_name  # noqa: B018


@pytest.mark.parametrize("module", sorted(_SERVED_IN_0_43))
def test_import_and_private_lookups_load_no_table(module: str) -> None:
    """Importing the module and probing a private name leave the table unloaded."""
    # Arrange
    code = (
        "import importlib, sys\n"
        f"module = importlib.import_module({module!r})\n"
        "assert not hasattr(module, '__path__')\n"
        "assert not hasattr(module, '_private')\n"
        "print('grelmicro._deprecation' in sys.modules)"
    )

    # Act
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        check=True,
        text=True,
    )

    # Assert
    assert result.stdout.strip() == "False"
