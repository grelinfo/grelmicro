"""Every backend adapter honors the `bind` contract, discovered, not listed.

[Plugins](../docs/architecture/plugins.md) promises a third-party adapter
three things about `bind`. A hand-written list checks the adapters someone
remembered, which is how the memory circuit breaker ran every algorithm as
consecutive-count while the other three refused: the rule held for a family
and only some of the family was tested.

The sweep walks the package instead, so an adapter added tomorrow is covered
without anyone remembering to add it here, and it refuses to pass on an empty
scan the way `tests/test_adapter_contracts.py` does.
"""

import ast
import importlib
import inspect
from pathlib import Path
from typing import Any

import pytest

import grelmicro
from tests._contract_support import base_names, module_name

PACKAGE_ROOT = Path(grelmicro.__file__).parent

STRATEGY_PROTOCOLS = frozenset({"CircuitBreakerBackend", "RateLimiterBackend"})
"""Backend protocols whose `bind` selects an algorithm from a config kind."""

_MIN_BACKENDS = 8
"""Floor for the sweep, so an empty scan cannot pass silently."""

UNKNOWN_KIND = "failure_rate"
"""A planned algorithm no shipped adapter implements yet.

The config union grows over time, so an adapter that predates a new arm has
to refuse it. Picking a real planned name rather than nonsense keeps the test
honest about what it is guarding.
"""


class UnknownConfig:
    """Stands in for an algorithm config the adapter cannot know."""

    kind = UNKNOWN_KIND


class HostileProvider:
    """Fails on any attribute an adapter reaches for.

    `bind` has to decide on the kind before it touches the client, so
    refusing an unsupported algorithm never needs a live connection. Reading
    the provider first turns a configuration mistake into a connection
    error, which is a much worse thing to debug at three in the morning.
    """

    def __getattr__(self, name: str) -> object:
        """Fail loudly, naming the attribute `bind` reached for."""
        msg = f"bind touched provider.{name} before checking the kind"
        raise AssertionError(msg)


def _discover_backends() -> list[tuple[str, str]]:
    """Return `(module, class)` for every class declaring a strategy backend."""
    found: list[tuple[str, str]] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module = module_name(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if base_names(node) & STRATEGY_PROTOCOLS:
                found.append((module, node.name))
    return found


BACKENDS = _discover_backends()


def test_the_sweep_finds_backends() -> None:
    """An empty or shrunken scan is a failure, never a silent pass."""
    assert len(BACKENDS) >= _MIN_BACKENDS, (
        f"expected at least {_MIN_BACKENDS} backends, found {BACKENDS}"
    )


def _build(module_name: str, class_name: str) -> Any:  # noqa: ANN401
    """Construct an adapter with a provider that refuses to be touched.

    The provider is never opened. An adapter that only stores it satisfies
    the contract, and one that reads it during `bind` fails loudly, which is
    the whole point.
    """
    cls = getattr(importlib.import_module(module_name), class_name)
    if "provider" in inspect.signature(cls).parameters:
        return cls(provider=HostileProvider())
    return cls()


@pytest.mark.parametrize(
    ("module_name", "class_name"),
    BACKENDS,
    ids=[name for module, name in BACKENDS],
)
def test_bind_refuses_an_unknown_kind_without_touching_the_provider(
    module_name: str,
    class_name: str,
) -> None:
    """`bind` raises `NotImplementedError` naming the kind, offline.

    Naming the kind matters: the operator set an algorithm this backend does
    not carry, and the message is what tells them which one to change.
    """
    backend = _build(module_name, class_name)
    if hasattr(backend, "_provider"):
        backend._provider = HostileProvider()
    with pytest.raises(NotImplementedError, match=UNKNOWN_KIND):
        _bind(backend, UnknownConfig())


def _bind(backend: Any, config: object) -> object:  # noqa: ANN401
    """Call `bind` through whichever signature the family uses.

    Circuit breakers key their state per name so they take one, rate
    limiters do not. The contract under test is the same either way.
    """
    parameters = inspect.signature(backend.bind).parameters
    if "name" in parameters:
        return backend.bind(name="sweep", config=config)
    return backend.bind(config)


@pytest.mark.parametrize(
    ("module_name", "class_name"),
    BACKENDS,
    ids=[name for module, name in BACKENDS],
)
def test_backend_declares_its_scope(
    module_name: str,
    class_name: str,
) -> None:
    """A backend states how far it reaches, so the scope check can run.

    An undeclared scope would let a memory backend pass for a cluster one,
    which is the check that stops a `Lock` excluding nothing across replicas.
    """
    backend = _build(module_name, class_name)
    scope = getattr(backend, "scope", None)
    assert scope in {"process", "host", "cluster"}, (
        f"{class_name}.scope is {scope!r}, expected process, host or cluster"
    )
