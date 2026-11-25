"""Validation-boundary tests for the `@cached` decorator.

The broader suite checks the common decorator paths. These pin the `early` and
`stale_ttl` validation boundaries, so a flipped comparison (`0 <= early` to
`0 < early`, `stale_ttl <= 0` to `<= 1`) is caught at decoration time.
"""

from __future__ import annotations

import re
from typing import Any

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
        @cached(ttl=_TTL, key_template="repo:{key}")
        async def load(self, key: str) -> str:
            return key

    assert Repo.load is not None


def test_cached_method_with_a_key_function_is_accepted() -> None:
    """A `key` function names the entry just as explicitly as `key_template`."""

    class Repo:
        @cached(ttl=_TTL, key=lambda func, args, kwargs: "repo")  # noqa: ARG005
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


_KEY_SOURCES = pytest.mark.parametrize(
    ("source", "label"),
    [("key_template", "Cache key template"), ("tags", "Cache tag template")],
    ids=["key_template", "tags"],
)


_SOURCES = pytest.mark.parametrize("source", ["key_template", "tags"])


def _template_kwargs(source: str, template: str) -> dict[str, Any]:
    """Return the `@cached` keyword passing `template` through `source`."""
    if source == "tags":
        return {"tags": [template]}
    return {"key_template": template}


@_KEY_SOURCES
@pytest.mark.parametrize(
    "template",
    ["u:{}", "u:{0}", "u:{0[0]}", "u:{.x}", "u:{user_id:{0}}"],
    ids=["auto", "index", "index-item", "empty-attribute", "nested"],
)
def test_a_positional_template_field_is_refused_when_decorating(
    source: str, label: str, template: str
) -> None:
    """A positional field names no parameter, so decorating refuses it."""
    expected = (
        f"{label} {template!r} reads a positional field. Name the parameter "
        'instead, as in "user:{user_id}".'
    )

    with pytest.raises(ValueError, match="^" + re.escape(expected) + "$"):

        @cached(ttl=_TTL, **_template_kwargs(source, template))
        async def load(user_id: int) -> int:
            return user_id


@_KEY_SOURCES
@pytest.mark.parametrize(
    ("template", "names"),
    [
        ("u:{nope}", "nope"),
        ("u:{nope.x}", "nope"),
        ("u:{nope[0]}", "nope"),
        ("u:{user_id:{width}}", "width"),
        ("u:{b}:{a}", "a, b"),
    ],
    ids=["name", "attribute", "item", "nested", "two"],
)
def test_a_template_naming_an_unknown_parameter_is_refused_when_decorating(
    source: str, label: str, template: str, names: str
) -> None:
    """A template naming a parameter the function lacks is refused."""

    async def load(user_id: int) -> int:
        return user_id

    expected = (
        f"{label} {template!r} names {names}, which {load.__qualname__} "
        "does not take."
    )

    with pytest.raises(ValueError, match="^" + re.escape(expected) + "$"):
        cached(ttl=_TTL, **_template_kwargs(source, template))(load)


@_KEY_SOURCES
def test_a_name_only_reached_through_var_keyword_is_refused(
    source: str, label: str
) -> None:
    """`**kwargs` binds under its own name, so `{user_id}` names nothing."""
    with pytest.raises(ValueError, match=f"^{label} .* does not take"):

        @cached(ttl=_TTL, **_template_kwargs(source, "u:{user_id}"))
        async def load(**kwargs: int) -> int:
            return len(kwargs)


@_KEY_SOURCES
@pytest.mark.parametrize(
    ("template", "reason"),
    [
        ("u:{user_id", "expected '}' before end of string"),
        ("u:{user_id}}x}", "Single '}' encountered in format string"),
    ],
    ids=["open", "close"],
)
def test_a_template_that_cannot_be_read_is_refused_when_decorating(
    source: str, label: str, template: str, reason: str
) -> None:
    """A template with an unmatched brace is refused with the parser's reason."""
    expected = f"{label} {template!r} cannot be read: {reason}."

    with pytest.raises(ValueError, match="^" + re.escape(expected) + "$"):

        @cached(ttl=_TTL, **_template_kwargs(source, template))
        async def load(user_id: int) -> int:
            return user_id


def test_a_tag_with_a_lone_closing_brace_is_refused_when_decorating() -> None:
    """Every tag is rendered, so a lone `}` in a literal tag is refused."""
    expected = (
        "Cache tag template 'u:}' cannot be read: "
        "Single '}' encountered in format string."
    )

    with pytest.raises(ValueError, match="^" + re.escape(expected) + "$"):

        @cached(ttl=_TTL, tags=["u:}"])
        async def load(user_id: int) -> int:
            return user_id


@_KEY_SOURCES
def test_an_unknown_conversion_is_refused_when_decorating(
    source: str, label: str
) -> None:
    """Only `!r`, `!s` and `!a` convert a field."""
    expected = (
        f"{label} 'u:{{user_id!z}}' converts user_id with !z. Use !r, !s or !a."
    )

    with pytest.raises(ValueError, match="^" + re.escape(expected) + "$"):

        @cached(ttl=_TTL, **_template_kwargs(source, "u:{user_id!z}"))
        async def load(user_id: int) -> int:
            return user_id


async def test_a_key_template_without_a_field_stays_literal() -> None:
    """A key template with no `{` is the key as written, braces included."""
    calls: list[int] = []

    @cached(ttl=_TTL, key_template="u:}")
    async def load(user_id: int) -> int:
        calls.append(user_id)
        return user_id

    assert await load(1) == 1
    assert await load(2) == 1
    assert calls == [1]


@_SOURCES
@pytest.mark.parametrize(
    "template",
    [
        "u:{user_id}",
        "u:{{user_id}}",
        "u:{user_id!r}",
        "u:{user_id:>{width}}",
    ],
    ids=["field", "escaped", "conversion", "nested"],
)
async def test_a_valid_template_still_renders(
    source: str, template: str
) -> None:
    """A template naming real parameters decorates and renders per call."""
    calls: list[int] = []

    @cached(ttl=_TTL, **_template_kwargs(source, template))
    async def load(user_id: int, width: int = 3) -> int:
        del width
        calls.append(user_id)
        return user_id

    assert await load(1) == 1
    assert await load(1) == 1
    assert calls == [1]


@_SOURCES
async def test_a_positional_only_parameter_is_named_by_the_template(
    source: str,
) -> None:
    """A positional-only parameter binds under its name, so it renders."""

    @cached(ttl=_TTL, **_template_kwargs(source, "u:{user_id}"))
    async def load(user_id: int, /) -> int:
        return user_id

    assert await load(1) == 1


@_SOURCES
async def test_a_var_keyword_item_is_named_through_its_parameter(
    source: str,
) -> None:
    """`{kwargs[user_id]}` reads the item from the `**kwargs` parameter."""

    @cached(ttl=_TTL, **_template_kwargs(source, "u:{kwargs[user_id]}"))
    async def load(**kwargs: int) -> int:
        return kwargs["user_id"]

    assert await load(user_id=1) == 1
