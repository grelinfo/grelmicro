"""Memory outbox backends that record what the relay asks of them."""

import asyncio
from datetime import timedelta
from typing import Literal

from grelmicro.outbox.memory import MemoryOutboxAdapter


class PurgeSpy(MemoryOutboxAdapter):
    """Memory backend that records every purge it receives."""

    def __init__(self) -> None:
        """Start with no purge recorded."""
        super().__init__()
        self.calls: list[tuple[timedelta | None, tuple[str, ...]]] = []
        self.purged = asyncio.Event()

    async def purge(
        self,
        *,
        older_than: timedelta | None = None,
        states: tuple[Literal["delivered", "dead"], ...] = (
            "delivered",
            "dead",
        ),
    ) -> int:
        """Record `older_than` and `states`, then purge."""
        self.calls.append((older_than, states))
        self.purged.set()
        return await super().purge(older_than=older_than, states=states)
