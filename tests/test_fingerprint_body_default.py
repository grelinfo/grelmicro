"""`fingerprint_body` left at its default warns until it becomes `True` in 0.46.0."""

import warnings
from collections.abc import Callable

import pytest
from starlette.applications import Starlette

from grelmicro._deprecation import DEFAULT_FINGERPRINT_BODY
from grelmicro.errors import Grelmicro046Warning, GrelmicroFutureWarning
from grelmicro.http import (
    IdempotencyMiddleware,
    IdempotentRequests,
    IdempotentRequestsConfig,
)
from grelmicro.idempotency import Idempotency
from tests._future import assert_future_warning

_MESSAGE = (
    "`IdempotentRequests(fingerprint_body=)` defaults to `True` from 0.46.0. "
    "Pass `fingerprint_body=False` to keep the current behavior, or "
    "`fingerprint_body=True` to adopt the new one now."
)
"""The warning a default `fingerprint_body` gives, word for word."""

_MIDDLEWARE_MESSAGE = _MESSAGE.replace(
    "IdempotentRequests(", "IdempotencyMiddleware("
)
"""The same warning, from a middleware built by hand."""

_DOORS = 3
"""The component, `from_config` and the middleware built by hand."""


def _middleware(**options: object) -> IdempotencyMiddleware:
    """Build the middleware by hand around an empty app."""
    return IdempotencyMiddleware(
        Starlette(),
        idempotency=Idempotency("http"),
        **options,  # type: ignore[arg-type] # ty: ignore[invalid-argument-type]
    )


def _future_warnings(build: Callable[[], object]) -> list[str]:
    """Return the message of each future warning `build` emits."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        build()
    return [
        str(warning.message)
        for warning in caught
        if issubclass(warning.category, GrelmicroFutureWarning)
    ]


def test_message_names_fingerprint_body() -> None:
    """The message names the argument, the release and both settings."""
    assert DEFAULT_FINGERPRINT_BODY.message == _MESSAGE
    assert DEFAULT_FINGERPRINT_BODY.category is Grelmicro046Warning


def test_component_left_at_default_warns() -> None:
    """A component left at the default warns, and one that passes it does not."""
    assert_future_warning(
        DEFAULT_FINGERPRINT_BODY,
        default=IdempotentRequests,
        explicit=lambda: IdempotentRequests(fingerprint_body=True),
    )


@pytest.mark.parametrize("value", [True, False])
def test_component_passed_a_value_warns_nothing(value: bool) -> None:  # noqa: FBT001
    """`True` and `False` passed in both warn nothing."""
    assert (
        _future_warnings(lambda: IdempotentRequests(fingerprint_body=value))
        == []
    )


def test_component_keeps_the_current_default() -> None:
    """Left at the default, the body is not fingerprinted."""
    with pytest.warns(Grelmicro046Warning):
        component = IdempotentRequests()

    assert component.config.fingerprint_body is False


@pytest.mark.parametrize(
    "variable",
    [
        "GREL_IDEMPOTENT_REQUESTS_FINGERPRINT_BODY",
        "GREL_IDEMPOTENT_REQUESTS_ORDERS_FINGERPRINT_BODY",
    ],
)
@pytest.mark.parametrize("value", ["true", "false"])
def test_component_set_through_the_environment_warns_nothing(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    """A value from the instance or the kind-wide variable warns nothing."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.setenv(variable, value)
    name = "orders" if "_ORDERS_" in variable else "default"

    assert _future_warnings(lambda: IdempotentRequests(name=name)) == []


def test_component_read_from_the_environment_left_unset_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the environment read and no variable set, it still warns."""
    monkeypatch.setenv("GREL_ENV_LOAD", "1")
    monkeypatch.delenv(
        "GREL_IDEMPOTENT_REQUESTS_FINGERPRINT_BODY", raising=False
    )

    assert _future_warnings(IdempotentRequests) == [_MESSAGE]


def test_from_config_left_at_default_warns() -> None:
    """A config that leaves the field out warns from `from_config`."""
    assert_future_warning(
        DEFAULT_FINGERPRINT_BODY,
        default=lambda: IdempotentRequests.from_config(
            IdempotentRequestsConfig()
        ),
        explicit=lambda: IdempotentRequests.from_config(
            IdempotentRequestsConfig(fingerprint_body=True)
        ),
    )


@pytest.mark.parametrize("value", [True, False])
def test_from_config_read_from_a_file_warns_nothing(value: bool) -> None:  # noqa: FBT001
    """A config validated from file data that sets the field warns nothing."""
    config = IdempotentRequestsConfig.model_validate(
        {"fingerprint_body": value}
    )

    assert (
        _future_warnings(lambda: IdempotentRequests.from_config(config)) == []
    )


def test_hand_built_middleware_left_at_default_warns() -> None:
    """A middleware built by hand warns the same way, naming itself."""
    assert _future_warnings(_middleware) == [_MIDDLEWARE_MESSAGE]


@pytest.mark.parametrize("value", [True, False])
def test_hand_built_middleware_passed_a_value_warns_nothing(
    value: bool,  # noqa: FBT001
) -> None:
    """`True` and `False` passed to the middleware both warn nothing."""
    assert _future_warnings(lambda: _middleware(fingerprint_body=value)) == []


def test_component_middleware_warns_nothing() -> None:
    """The middleware a component builds warns nothing of its own."""
    middleware, options = IdempotentRequests(
        fingerprint_body=False
    ).asgi_middleware()

    assert _future_warnings(lambda: middleware(Starlette(), **options)) == []


def test_warning_points_at_the_line_that_built_it() -> None:
    """Each door reports the line in the caller's file."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        IdempotentRequests()
        IdempotentRequests.from_config(IdempotentRequestsConfig())
        IdempotencyMiddleware(Starlette(), idempotency=Idempotency("http"))

    future = [
        warning
        for warning in caught
        if issubclass(warning.category, Grelmicro046Warning)
    ]
    assert len(future) == _DOORS
    assert {warning.filename for warning in future} == {__file__}
