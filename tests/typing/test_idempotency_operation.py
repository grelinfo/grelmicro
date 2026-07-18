"""Static typing samples for the idempotency `Operation` replay path.

Runs as a pytest module so the imports execute, and is also picked up
by `uv run ty check` so the `assert_type` call validates that
`op.result()` reads as `T` (not `T | None`) on the replay path. A
regression that widens it back to `T | None` fails ty (the replay
branch would need a `cast`/`assert` again) even when all runtime tests
pass.
"""

from __future__ import annotations

from typing import assert_type

from grelmicro.idempotency import Idempotency

idem = Idempotency[dict]("charge", ttl=3600)


async def charge(key: str, amount: int) -> dict:
    """Replay branch returns `op.result()` directly, no cast."""
    async with idem(key) as op:
        if op.replayed:
            assert_type(op.result(), dict)
            return op.result()
        response = {"amount": amount}
        op.store(response)
        return response
