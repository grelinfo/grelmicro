"""A set of path patterns is a set, and a string is not one.

`exclude="/internal/*"` is a missing comma. It is a sequence of characters,
so the matcher walks it one at a time, and the single `*` matches every path
as a prefix: the middleware then acts on nothing, or on everything, with
nothing said. Every door that takes patterns refuses one.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from grelmicro.http import (
    ConditionalRequests,
    ConditionalRequestsMiddleware,
    IdempotencyMiddleware,
    IdempotentRequests,
)
from grelmicro.idempotency import Idempotency
from grelmicro.log import AccessLog, AccessLogMiddleware


async def app(scope: object, receive: object, send: object) -> None:
    """Stand in for the app a middleware wraps."""


def components() -> list[tuple[str, Any]]:
    """Return every component that takes path patterns, with its name."""
    return [
        ("AccessLog", AccessLog),
        ("IdempotentRequests", IdempotentRequests),
        ("ConditionalRequests", ConditionalRequests),
    ]


@pytest.mark.parametrize(("name", "component"), components())
@pytest.mark.parametrize("field", ["include", "exclude"])
def test_a_component_refuses_a_bare_string(
    name: str,  # noqa: ARG001
    component: Any,  # noqa: ANN401
    field: str,
) -> None:
    """The component says so where the mistake is written."""
    mistake = cast("Any", {field: "/internal/*"})

    with pytest.raises(TypeError, match="is a string"):
        component(**mistake)


@pytest.mark.parametrize("field", ["include", "exclude"])
def test_the_conditional_middleware_refuses_a_bare_string(
    field: str,
) -> None:
    """The middleware is public too, and wired by hand as often as not."""
    mistake = cast("Any", {field: "/internal/*"})

    with pytest.raises(TypeError, match="is a string"):
        ConditionalRequestsMiddleware(app, **mistake)


@pytest.mark.parametrize("field", ["include", "exclude"])
def test_the_idempotency_middleware_refuses_a_bare_string(
    field: str,
) -> None:
    """The same, for the one that replays a stored response."""
    mistake = cast("Any", {field: "/internal/*"})

    with pytest.raises(TypeError, match="is a string"):
        IdempotencyMiddleware(app, idempotency=Idempotency("test"), **mistake)


@pytest.mark.parametrize("field", ["include", "exclude", "quiet"])
def test_the_access_log_middleware_refuses_a_bare_string(
    field: str,
) -> None:
    """And the one that writes a record for every request."""
    mistake = cast("Any", {field: "/internal/*"})

    with pytest.raises(TypeError, match="is a string"):
        AccessLogMiddleware(app, **mistake)


def test_a_tuple_of_patterns_is_taken_as_it_is() -> None:
    """The shape that was always meant still works, list or tuple."""
    assert AccessLog(exclude=("/internal/*",))
    assert AccessLogMiddleware(app, exclude=cast("Any", ["/internal/*"]))
