"""Property-based tests for the `Bulkhead` `uses=` scope.

Every finding this scope collected during review was the same shape: some
combination of runs, bulkheads and shared items opened one item twice, or
closed it while something still held it. Example tests only cover the
combinations someone thought of.

Hypothesis builds the combinations here instead. Each item refuses to open
while it is already open and refuses to close while it is not, so a broken
interleaving fails at the operation that broke it rather than at a count
compared afterwards.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from grelmicro import Grelmicro, NoActiveAppError, OutOfContextError
from grelmicro.resilience import Bulkhead

if TYPE_CHECKING:
    from types import TracebackType

pytestmark = [pytest.mark.timeout(30)]

_ITEMS = 3
_REFUSALS = (OutOfContextError, NoActiveAppError)
"""What a scope is allowed to answer instead of opening.

Every one of these is documented: the run is shutting down, has gone, or
another run holds the scope and has not finished opening it.
"""


@dataclass
class _Tracked:
    """A scope item that refuses to be opened twice over."""

    label: int
    depth: int = 0
    opens: int = 0
    closes: int = 0
    faults: list[str] = field(default_factory=list)

    async def __aenter__(self) -> Self:
        """Open, unless something already has it open."""
        if self.depth:
            self.faults.append(f"item {self.label} opened while open")
        self.depth += 1
        self.opens += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close, unless nothing had it open."""
        if not self.depth:
            self.faults.append(f"item {self.label} closed while closed")
        self.depth -= 1
        self.closes += 1


async def _drive(
    scopes: list[list[int]],
    app_listed: list[int],
    runs: int,
    order: list[int],
    *,
    overlap: bool,
) -> list[_Tracked]:
    """Run one generated scenario and hand back the items it touched."""
    items = [_Tracked(index) for index in range(_ITEMS)]
    bulkheads = [
        Bulkhead(f"scope-{index}", uses=[items[i] for i in uses])
        for index, uses in enumerate(scopes)
    ]

    async def enter_each() -> None:
        for index in order:
            bulkhead = bulkheads[index % len(bulkheads)]
            try:
                async with bulkhead:
                    # The point of a scope: while inside it, everything it
                    # lists is open. This is the property #791 broke, where
                    # a second run entered nothing and used closed items.
                    for item in bulkhead._uses:
                        if isinstance(item, _Tracked) and not item.depth:
                            item.faults.append(
                                f"item {item.label} closed inside the scope"
                            )
            except _REFUSALS:
                pass

    for _ in range(runs):
        async with Grelmicro(
            uses=[items[i] for i in app_listed], allow_multiple=True
        ):
            await enter_each()
            if overlap:
                async with Grelmicro(allow_multiple=True):
                    await enter_each()
            await enter_each()
    return items


@given(
    scopes=st.lists(
        st.lists(
            st.integers(min_value=0, max_value=_ITEMS - 1),
            min_size=1,
            max_size=_ITEMS,
        ),
        min_size=1,
        max_size=3,
    ),
    app_listed=st.lists(
        st.integers(min_value=0, max_value=_ITEMS - 1),
        max_size=_ITEMS,
        unique=True,
    ),
    runs=st.integers(min_value=1, max_value=3),
    order=st.lists(
        st.integers(min_value=0, max_value=2), min_size=1, max_size=4
    ),
    overlap=st.booleans(),
)
@settings(max_examples=300, deadline=None)
def test_no_item_is_ever_opened_twice_over(
    scopes: list[list[int]],
    app_listed: list[int],
    runs: int,
    order: list[int],
    *,
    overlap: bool,
) -> None:
    """No sequence of runs, scopes and shared items opens one item twice.

    Reopening after a close is fine and expected: a later run opens the
    scope again from the start. What must never happen is a second open
    while the first is still standing.
    """
    items = asyncio.run(
        _drive(scopes, app_listed, runs, order, overlap=overlap)
    )

    for item in items:
        assert item.faults == []


@given(
    scopes=st.lists(
        st.lists(
            st.integers(min_value=0, max_value=_ITEMS - 1),
            min_size=1,
            max_size=_ITEMS,
        ),
        min_size=1,
        max_size=3,
    ),
    app_listed=st.lists(
        st.integers(min_value=0, max_value=_ITEMS - 1),
        max_size=_ITEMS,
        unique=True,
    ),
    runs=st.integers(min_value=1, max_value=3),
    order=st.lists(
        st.integers(min_value=0, max_value=2), min_size=1, max_size=4
    ),
    overlap=st.booleans(),
)
@settings(max_examples=300, deadline=None)
def test_every_open_is_matched_by_a_close(
    scopes: list[list[int]],
    app_listed: list[int],
    runs: int,
    order: list[int],
    *,
    overlap: bool,
) -> None:
    """Nothing the scope opened is still open once every run has gone."""
    items = asyncio.run(
        _drive(scopes, app_listed, runs, order, overlap=overlap)
    )

    for item in items:
        assert item.opens == item.closes
        assert item.depth == 0
