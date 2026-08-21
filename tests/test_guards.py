"""Shape tests that never raise on a value the caller supplied."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from grelmicro._guards import (
    UNNAMEABLE,
    is_class,
    is_instance,
    is_subclass,
    name_of,
    type_name,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class _LazyProxy:
    """Forwards `__class__` to a target that is not bound yet.

    `werkzeug.local.LocalProxy` and `django.utils.functional.SimpleLazyObject`
    both behave this way while unbound.
    """

    @property
    def __class__(self) -> type:  # type: ignore[override]
        """Raise, the way an unbound proxy does."""
        msg = "proxy is not bound"
        raise RuntimeError(msg)


class _ClaimsToBeAClass:
    """Reports `type` as its class without being one."""

    @property
    def __class__(self) -> type:  # type: ignore[override]
        """Report `type`, so `isinstance(x, type)` says yes."""
        return type


class _HostileMeta(type):
    """A metaclass whose `__name__` raises, blocking the last resort."""

    @property
    def __name__(cls) -> str:
        """Raise, so even the type name cannot be read."""
        msg = "metaclass __name__"
        raise RuntimeError(msg)

    def __repr__(cls) -> str:
        """Stay readable, so a failing test still says which class it is."""
        return f"<{cls.__qualname__}>"


class _Unnameable(metaclass=_HostileMeta):
    """A value that refuses every way of naming it."""


class _RefusesEverything:
    """Raises an interrupt from the dunder each guard reads."""

    @property
    def __class__(self) -> type:  # type: ignore[override]
        """Raise an interrupt, which no guard may swallow."""
        raise KeyboardInterrupt


class _InterruptingMeta(type):
    """A metaclass whose `__name__` raises an interrupt."""

    @property
    def __name__(cls) -> str:
        """Raise an interrupt, which no guard may swallow."""
        raise KeyboardInterrupt


class _NameInterrupts(metaclass=_InterruptingMeta):
    """A value whose type name raises an interrupt."""


def test_ordinary_values_answer_the_way_the_builtins_do() -> None:
    """The guards are the builtins for every value that behaves."""
    assert is_instance("text", str) is True
    assert is_instance(1, str) is False
    assert is_instance(1, (int, str)) is True
    assert is_class(ValueError) is True
    assert is_class(ValueError()) is False
    assert is_subclass(ValueError, Exception) is True
    assert is_subclass(Exception, ValueError) is False
    assert type_name(1) == "int"
    assert type_name(ValueError()) == "ValueError"
    assert type_name(ValueError) == "type"
    assert name_of(ValueError) == "ValueError"
    assert name_of(ValueError()) == "ValueError"
    assert name_of(1) == "int"


def test_a_lazy_proxy_answers_false_rather_than_raising() -> None:
    """`isinstance` reads `__class__`, which an unbound proxy raises from."""
    assert is_instance(_LazyProxy(), str) is False
    assert is_class(_LazyProxy()) is False


def test_an_object_posing_as_a_class_answers_false() -> None:
    """It passes the class check, then `issubclass` refuses it.

    `issubclass` raises `TypeError` for a non-class, and answering False
    sends the value to the argument error it was going to get.
    """
    assert is_class(_ClaimsToBeAClass()) is True
    assert is_subclass(_ClaimsToBeAClass(), Exception) is False


def test_an_unnameable_value_gets_a_placeholder() -> None:
    """A metaclass can define `__name__` as a property that raises."""
    assert type_name(_Unnameable()) == UNNAMEABLE
    assert name_of(_Unnameable) == UNNAMEABLE


class _InterruptingClassName(metaclass=_InterruptingMeta):
    """A class whose own name raises an interrupt."""


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda: is_instance(_RefusesEverything(), str), id="is-instance"
        ),
        pytest.param(lambda: is_class(_RefusesEverything()), id="is-class"),
        pytest.param(lambda: name_of(_InterruptingClassName), id="name-of"),
        pytest.param(
            lambda: is_subclass(_ClaimsToBeAClass(), _InterruptingParent),
            id="is-subclass",
        ),
        pytest.param(lambda: type_name(_NameInterrupts()), id="type-name"),
    ],
)
def test_a_real_interrupt_is_never_swallowed(
    call: Callable[[], object],
) -> None:
    """The guards absorb a hostile value, not a genuine Ctrl-C."""
    with pytest.raises(KeyboardInterrupt):
        call()


class _InterruptingSubclassCheck(type):
    """A metaclass whose subclass check raises an interrupt."""

    def __subclasscheck__(cls, subclass: type) -> bool:
        """Raise an interrupt, which no guard may swallow."""
        raise KeyboardInterrupt


class _InterruptingParent(metaclass=_InterruptingSubclassCheck):
    """A parent whose subclass check raises an interrupt."""


class _EvilName(str):
    """A name that runs caller code the moment it is interpolated."""

    __slots__ = ()

    def __format__(self, _spec: str) -> str:
        """Raise, the way a hostile subclass does when interpolated."""
        msg = "format exploded"
        raise RuntimeError(msg)


class _SubclassNameMeta(type):
    """A metaclass whose `__name__` is a string subclass."""

    @property
    def __name__(cls) -> str:
        """Return a hostile subclass, which `str()` accepts."""
        return _EvilName("Evil")


class _NamedBySubclass(metaclass=_SubclassNameMeta):
    """A class whose name is not an exact `str`."""


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(type_name(_NamedBySubclass()), id="type-name"),
        pytest.param(name_of(_NamedBySubclass), id="name-of"),
    ],
)
def test_a_name_is_an_exact_str(name: str) -> None:
    """A name is read to be put in a message, which is an interpolation.

    `str()` hands back a subclass unchanged, and a subclass runs caller
    code again from `__format__`, so the guard that read the name safely
    would blow up at the line that uses it.
    """
    assert type(name) is str
    assert f"{name}" == name
