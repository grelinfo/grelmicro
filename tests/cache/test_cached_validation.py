"""Validation-boundary tests for the `@cached` decorator.

The broader suite checks the common decorator paths. These pin the `early` and
`stale_ttl` validation boundaries, so a flipped comparison (`0 <= early` to
`0 < early`, `stale_ttl <= 0` to `<= 1`) is caught at decoration time.
"""

from __future__ import annotations

import pytest

from grelmicro.cache.cached import cached

_TTL = 60


def test_early_zero_is_accepted() -> None:
    """`early=0.0` is a valid lower bound (the guard allows `0 <= early`)."""

    @cached(ttl=_TTL, early=0.0)
    async def fn() -> int:
        return 1

    assert fn is not None


def test_stale_ttl_of_one_is_accepted() -> None:
    """`stale_ttl=1` is valid (the guard rejects `<= 0`, not `<= 1`)."""

    @cached(ttl=_TTL, stale_ttl=1)
    async def fn() -> int:
        return 1

    assert fn is not None


def test_method_without_an_explicit_key_is_refused() -> None:
    """A method needs a key, because the default one embeds `repr(self)`.

    Without this guard two instances whose repr matches silently share one
    entry, and a default repr carries a memory address, so the key changes
    on every restart.
    """
    with pytest.raises(TypeError, match="needs an explicit key="):

        class Repo:
            @cached(ttl=_TTL)
            async def load(self, key: str) -> str:
                return key


def test_method_with_an_explicit_key_is_accepted() -> None:
    """Naming what identifies the entry is what the guard asks for."""

    class Repo:
        @cached(ttl=_TTL, key="repo:{key}")
        async def load(self, key: str) -> str:
            return key

    assert Repo.load is not None


def test_method_with_a_key_maker_is_accepted() -> None:
    """A `key_maker` names the entry just as explicitly as `key`."""

    class Repo:
        @cached(ttl=_TTL, key_maker=lambda func, args, kwargs: "repo")  # noqa: ARG005
        async def load(self, key: str) -> str:
            return key

    assert Repo.load is not None


def test_a_staticmethod_is_accepted() -> None:
    """A `staticmethod` carries no instance, so nothing unstable reaches the key."""

    class Repo:
        @staticmethod
        @cached(ttl=_TTL)
        async def pure(key: str) -> str:
            return key

    assert Repo.pure is not None


def test_a_classmethod_is_accepted() -> None:
    """A `classmethod` receives a class, whose `repr()` is stable."""

    class Repo:
        @classmethod
        @cached(ttl=_TTL)
        async def make(cls, key: str) -> str:
            return key

    assert Repo.make is not None


def test_a_function_nested_in_a_function_is_not_a_method() -> None:
    """`<locals>` in the qualname means a closure, which keys on its own args."""

    def outer() -> object:
        @cached(ttl=_TTL)
        async def inner(key: str) -> str:
            return key

        return inner

    assert outer() is not None
