"""The worked third-party integration in `examples/third-party-integration/`.

Each test loads `tinyweb` and `grelmicro_tinyweb` from the example
directory, publishes the integration under the `grelmicro.integrations`
entry point its `pyproject.toml` declares, and drives the app through
`micro.install(app)` and an ASGI client with the lifespan run.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import itertools
import sys
import tomllib
from http import HTTPStatus
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from starlette.testclient import TestClient

from grelmicro import Grelmicro, RoutesNotDeclaredWarning
from grelmicro.cache import Cache
from grelmicro.cache.memory import MemoryCacheAdapter
from grelmicro.http import (
    AuthenticatedRequests,
    CachedResponses,
    ErrorResponses,
    IdempotentRequests,
)
from grelmicro.providers.memory import MemoryProvider
from grelmicro.testing import FakeVerifier, fake_claims

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from starlette.types import Scope

pytestmark = [pytest.mark.timeout(5)]

EXAMPLE = Path(__file__).parent.parent / "examples" / "third-party-integration"
"""The example's directory, holding both modules and `pyproject.toml`."""

MODULES = ("tinyweb", "grelmicro_tinyweb")
"""The modules the example ships: the framework and its integration."""

ALICE = {"authorization": "Bearer alice"}
"""A caller holding no scope."""

BOB = {"authorization": "Bearer bob"}
"""A caller holding `orders:read`."""

GROUP = "grelmicro.integrations"
"""The entry-point group integrations register under."""


class Example(SimpleNamespace):
    """The example's modules, loaded from its directory."""

    tinyweb: Any
    integration: ModuleType
    entry_point: importlib.metadata.EntryPoint


@pytest.fixture
def example(monkeypatch: pytest.MonkeyPatch) -> Iterator[Example]:
    """Load the example by path, and publish its integration for `tinyweb`.

    The entry-point lookup of `grelmicro._discovery` lists the integration
    as an installed `grelmicro-tinyweb` would. The modules are dropped once
    the test ends.
    """
    monkeypatch.syspath_prepend(str(EXAMPLE))
    for name in MODULES:
        monkeypatch.delitem(sys.modules, name, raising=False)
    published = importlib.metadata.EntryPoint(
        "tinyweb", "grelmicro_tinyweb", GROUP
    )
    found = importlib.metadata.entry_points

    def entry_points(*, group: str) -> list[importlib.metadata.EntryPoint]:
        listed = list(found(group=group))
        return [*listed, published] if group == GROUP else listed

    monkeypatch.setattr("grelmicro._discovery.entry_points", entry_points)
    yield Example(
        tinyweb=importlib.import_module("tinyweb"),
        integration=importlib.import_module("grelmicro_tinyweb"),
        entry_point=published,
    )
    for name in MODULES:
        sys.modules.pop(name, None)


def text(answer: str) -> Callable[[Scope], Any]:
    """Return an endpoint answering `answer`."""

    async def endpoint(scope: Scope) -> str:  # noqa: ARG001
        return answer

    return endpoint


def counting() -> Callable[[Scope], Any]:
    """Return an endpoint answering how many times it ran."""
    calls = itertools.count(1)

    async def endpoint(scope: Scope) -> str:  # noqa: ARG001
        return str(next(calls))

    return endpoint


def checking_caller() -> Callable[[Scope], Any]:
    """Return an endpoint that reads its caller off `x-api-key` itself.

    It answers the caller and how many times it ran.
    """
    calls = itertools.count(1)

    async def endpoint(scope: Scope) -> str:
        caller = dict(scope["headers"]).get(b"x-api-key", b"").decode()
        return f"{caller} {next(calls)}"

    return endpoint


def authenticated() -> AuthenticatedRequests:
    """Return the authentication: `alice` holds no scope, `bob` holds `orders:read`."""
    return AuthenticatedRequests(
        FakeVerifier(
            alice=fake_claims("alice"),
            bob=fake_claims("bob", "orders:read"),
        )
    )


def install(app: object) -> Grelmicro:
    """Install the authentication on `app`, answering errors as problem details."""
    micro = Grelmicro(uses=[ErrorResponses(), authenticated()])
    micro.install(app)
    return micro


def shop(example: Example) -> Any:  # noqa: ANN401
    """Return a `TinyWeb` app with an anonymous, a protected and a scoped route."""
    route = example.tinyweb.Route
    return example.tinyweb.TinyWeb(
        route("/catalog", text("catalog"), anonymous=True),
        route("/cart", text("cart")),
        route("/orders", text("orders"), scopes=frozenset({"orders:read"})),
    )


def test_third_party_integration_anonymous_route_without_credential_is_served(
    example: Example,
) -> None:
    """A route declared `anonymous` answers a caller with no credential."""
    # Arrange
    app = shop(example)
    install(app)

    # Act
    with TestClient(app) as client:
        response = client.get("/catalog")

    # Assert
    assert response.status_code == HTTPStatus.OK
    assert response.text == "catalog"


def test_third_party_integration_protected_route_without_credential_is_refused_401(
    example: Example,
) -> None:
    """A route that declares nothing needs a caller, and serves one."""
    # Arrange
    app = shop(example)
    install(app)

    # Act
    with TestClient(app) as client:
        anonymous = client.get("/cart")
        alice = client.get("/cart", headers=ALICE)

    # Assert
    assert anonymous.status_code == HTTPStatus.UNAUTHORIZED
    assert alice.status_code == HTTPStatus.OK


def test_third_party_integration_scoped_route_without_scope_is_refused_403(
    example: Example,
) -> None:
    """A caller lacking a scope the route declares is refused, and one holding it is served."""
    # Arrange
    app = shop(example)
    install(app)

    # Act
    with TestClient(app) as client:
        alice = client.get("/orders", headers=ALICE)
        bob = client.get("/orders", headers=BOB)

    # Assert
    assert alice.status_code == HTTPStatus.FORBIDDEN
    assert bob.status_code == HTTPStatus.OK


def test_third_party_integration_unknown_url_without_credential_is_refused_401(
    example: Example,
) -> None:
    """A URL no route answers needs a caller before the framework answers `404`."""
    # Arrange
    app = shop(example)
    install(app)

    # Act
    with TestClient(app) as client:
        anonymous = client.get("/nowhere")
        alice = client.get("/nowhere", headers=ALICE)

    # Assert
    assert anonymous.status_code == HTTPStatus.UNAUTHORIZED
    assert alice.status_code == HTTPStatus.NOT_FOUND


def test_third_party_integration_declared_read_is_served_from_cache_on_second_request(
    example: Example,
) -> None:
    """A route declaring `cache` runs its endpoint once for two reads."""
    # Arrange
    route = example.tinyweb.Route
    app = example.tinyweb.TinyWeb(
        route("/prices", counting(), anonymous=True, cache=True),
    )
    Grelmicro(
        uses=[
            ErrorResponses(),
            authenticated(),
            MemoryProvider(),
            Cache(MemoryCacheAdapter()),
            CachedResponses(ttl=60),
        ]
    ).install(app)

    # Act
    with TestClient(app) as client:
        first = client.get("/prices")
        second = client.get("/prices")

    # Assert
    assert first.text == "1"
    assert second.text == "1"


def test_third_party_integration_route_added_after_install_fails_startup(
    example: Example,
) -> None:
    """A listed route that carries no gate stops the app from starting, named."""
    # Arrange
    app = shop(example)
    install(app)
    app.add_route(example.tinyweb.Route("/late", text("late")))

    # Act / Assert
    with (
        pytest.raises(
            RuntimeError, match=r"GET /late carries no authentication gate"
        ),
        TestClient(app),
    ):
        pass


def test_third_party_integration_without_hooks_every_route_requires_credential(
    example: Example, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An integration that declares no route leaves even an anonymous one authenticated."""
    # Arrange
    monkeypatch.delattr(example.integration, "install_route_gate")
    monkeypatch.delattr(example.integration, "route_declarations")
    app = shop(example)
    with pytest.warns(RoutesNotDeclaredWarning):
        install(app)

    # Act
    with TestClient(app) as client:
        public = client.get("/catalog")
        protected = client.get("/cart")
        nowhere = client.get("/nowhere")
        with_token = client.get("/catalog", headers=ALICE)

    # Assert
    assert public.status_code == HTTPStatus.UNAUTHORIZED
    assert protected.status_code == HTTPStatus.UNAUTHORIZED
    assert nowhere.status_code == HTTPStatus.UNAUTHORIZED
    assert with_token.status_code == HTTPStatus.OK


def test_third_party_integration_route_with_own_checks_never_replays_across_callers(
    example: Example,
) -> None:
    """A second caller reusing a key runs the endpoint on a route with checks of its own.

    The same caller reusing its key replays on a route without them.
    """
    # Arrange
    route = example.tinyweb.Route
    post = frozenset({"POST"})
    app = example.tinyweb.TinyWeb(
        route("/pay", checking_caller(), methods=post, own_checks=True),
        route("/note", checking_caller(), methods=post),
    )
    Grelmicro(
        uses=[
            ErrorResponses(),
            MemoryProvider(),
            Cache(MemoryCacheAdapter()),
            IdempotentRequests(),
        ]
    ).install(app)
    alice = {"idempotency-key": "k1", "x-api-key": "alice"}
    bob = {"idempotency-key": "k1", "x-api-key": "bob"}

    # Act
    with TestClient(app) as client:
        paid = [client.post("/pay", headers=h).text for h in (alice, bob)]
        noted = [client.post("/note", headers=alice).text for _ in range(2)]

    # Assert
    assert paid == ["alice 1", "bob 2"]
    assert noted == ["alice 1", "alice 1"]


def test_third_party_integration_describe_reports_declared_authentication(
    example: Example,
) -> None:
    """Each endpoint reads as its route declares: public, authenticated or scoped."""
    # Arrange
    app = shop(example)
    micro = install(app)

    # Act
    endpoints = micro.describe(app).endpoints

    # Assert
    assert [(row.method, row.path, row.applies) for row in endpoints] == [
        ("GET", "/cart", ("authenticated",)),
        ("GET", "/catalog", ("anonymous",)),
        ("GET", "/orders", ("authenticated orders:read",)),
    ]


def test_third_party_integration_example_entry_point_matches_pyproject(
    example: Example,
) -> None:
    """The entry point the tests publish is the one `pyproject.toml` declares."""
    # Arrange
    pyproject = tomllib.loads((EXAMPLE / "pyproject.toml").read_text())

    # Act
    declared = pyproject["project"]["entry-points"][GROUP]

    # Assert
    assert declared == {example.entry_point.name: example.entry_point.value}
    assert example.entry_point.load() is example.integration
    assert example.tinyweb.TinyWeb.__module__ == example.entry_point.name
