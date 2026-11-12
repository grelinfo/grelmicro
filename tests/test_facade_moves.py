"""The names an integration module served in 0.43 are gone from it.

`from grelmicro.integrations.fastapi import RateLimiter` worked in 0.43.1
because the module imported the class for its own use. 0.44 served each such
name with a warning, and 0.45.0 removes them, so each raises `AttributeError`
like any missing attribute.
"""

from __future__ import annotations

import importlib
import warnings
from typing import TYPE_CHECKING

import pytest

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


@pytest.mark.parametrize(("module", "name"), _CASES)
def test_moved_name_raises_attribute_error(module: str, name: str) -> None:
    """Reading the name raises the plain `AttributeError` and warns nothing."""
    # Arrange
    facade = _facade(module)

    # Act / Assert
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(
            AttributeError,
            match=f"^module '{module}' has no attribute '{name}'$",
        ):
            getattr(facade, name)


@pytest.mark.parametrize(("module", "name"), _CASES)
def test_moved_name_stays_off_the_public_surface(
    module: str, name: str
) -> None:
    """The name is not in `__all__`, `dir()`, the lazy table or the namespace."""
    # Arrange
    facade = _facade(module)

    # Assert
    assert name not in facade.__all__
    assert name not in facade._LAZY
    assert name not in dir(facade)
    assert name not in vars(facade)


def test_from_import_raises_import_error() -> None:
    """`from ... import` a moved name fails like any missing name."""
    with pytest.raises(ImportError, match="cannot import name 'RateLimiter'"):
        exec("from grelmicro.integrations.fastapi import RateLimiter", {})  # noqa: S102


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
    """Any other missing name raises the same `AttributeError`."""
    facade = _facade(module)

    with pytest.raises(AttributeError, match=f"module '{module}' has no attr"):
        facade.missing_name  # noqa: B018
