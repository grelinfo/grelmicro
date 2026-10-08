"""Test Task Utilities."""

import string

import pytest
from hypothesis import given
from hypothesis import strategies as st

from grelmicro.coordination.lock import LOCK_NAME_MAX_LENGTH, validate_lock_name
from grelmicro.task._utils import lock_key

KEPT = frozenset(string.ascii_letters + string.digits + "_.:")
PREFIX = "task-"

_segment = st.text(alphabet="a_é9.", min_size=1, max_size=300)
_long_segment = st.text(alphabet="aé", min_size=45, max_size=120)
auto_names = st.one_of(
    st.builds(
        lambda module, qualname: f"{module}:{qualname}", _segment, _segment
    ),
    st.builds(lambda module: f"_{module}:job", _long_segment),
)
"""Names like the `module:qualname` a task takes from its function.

The second arm crosses the length limit, often inside an escape.
"""


def _decode(key: str) -> str | None:
    """Return the name a mapped key spells in full, or None if it has none."""
    if not key.startswith(PREFIX):
        return key
    body = key[len(PREFIX) :]
    chars: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char in KEPT:
            chars.append(char)
            index += 1
            continue
        end = body.find("-", index + 1)
        if char != "-" or end == -1:
            return None
        try:
            chars.append(chr(int(body[index + 1 : end], 16)))
        except ValueError, OverflowError:
            return None
        index = end + 1
    return "".join(chars)


@pytest.mark.parametrize(
    ("reference", "key"),
    [
        ("tests.task.samples:test1", "tests.task.samples:test1"),
        ("main:job", "main:job"),
        ("__main__:job", "task-__main__:job"),
        ("_private:job", "task-_private:job"),
        ("été:job", "task--e9-t-e9-:job"),
    ],
)
def test_lock_key_auto_task_name_returns_valid_lock_name(
    reference: str, key: str
) -> None:
    """An auto task name maps to a valid lock name, unchanged when already one."""
    # Act
    result = lock_key(reference)

    # Assert
    assert result == key
    validate_lock_name(result)


def test_lock_key_long_auto_task_name_returns_name_at_length_limit() -> None:
    """An auto task name past the length limit maps to the longest lock name."""
    # Arrange
    reference = "_pkg:" + "a" * 300

    # Act
    key = lock_key(reference)

    # Assert
    assert len(key) == LOCK_NAME_MAX_LENGTH
    validate_lock_name(key)


def test_lock_key_long_auto_task_names_return_distinct_keys() -> None:
    """Two auto task names that share the kept start map to distinct names."""
    # Arrange
    reference = "_pkg:" + "a" * 300

    # Act
    first, second = lock_key(reference), lock_key(reference + "b")

    # Assert
    assert first != second


def test_lock_key_distinct_auto_task_names_return_distinct_keys() -> None:
    """Distinct auto task names never map to the same lock name."""
    # Arrange
    references = [
        "__main__:job",
        "main:job",
        "_main_:job",
        "main__:job",
        "été:job",
        "ete:job",
        "ètè:job",
        "_x:job",
        "x:job",
    ]

    # Act
    keys = {lock_key(reference) for reference in references}

    # Assert
    assert len(keys) == len(references)


@given(auto_names)
def test_lock_key_any_auto_task_name_returns_valid_lock_name(
    reference: str,
) -> None:
    """Every auto task name maps to a valid lock name."""
    # Act
    key = lock_key(reference)

    # Assert
    validate_lock_name(key)


@given(auto_names)
def test_lock_key_untruncated_auto_task_name_round_trips(
    reference: str,
) -> None:
    """A mapped name that fits the limit spells the auto task name in full."""
    # Act
    key = lock_key(reference)

    # Assert
    if len(key) < LOCK_NAME_MAX_LENGTH:
        assert _decode(key) == reference


@given(auto_names)
def test_lock_key_name_spelled_by_another_key_returns_distinct_key(
    reference: str,
) -> None:
    """A name spelled by another name's key, cut included, maps elsewhere."""
    # Arrange
    key = lock_key(reference)
    other = _decode(key)

    # Act / Assert
    if other is not None and other != reference:
        assert lock_key(other) != key


@given(auto_names, auto_names)
def test_lock_key_distinct_auto_task_names_never_share_a_key(
    first: str, second: str
) -> None:
    """Two distinct auto task names never map to the same lock name."""
    # Act / Assert
    if first != second:
        assert lock_key(first) != lock_key(second)
