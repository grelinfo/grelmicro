import asyncio

from grelmicro.coordination import Lock
from grelmicro.coordination.memory import MemoryLockAdapter


class Resource:
    """A protected resource that records the highest fencing token it accepts."""

    def __init__(self) -> None:
        self.highest_token = 0

    def write(self, *, fencing_token: int, value: str) -> bool:
        """Accept the write only when its token beats every prior one."""
        if fencing_token <= self.highest_token:
            return False
        self.highest_token = fencing_token
        return True


async def main() -> None:
    resource = Resource()

    async with MemoryLockAdapter() as backend:
        lock = Lock("cart", backend=backend)

        # A stale holder writes with an old token.
        stale = await lock.acquire()
        await lock.release()

        # The new holder gets a strictly greater token.
        async with lock as held:
            assert resource.write(fencing_token=held.fencing_token, value="new")

        # The stale token is now too low: the resource rejects it.
        assert not resource.write(
            fencing_token=stale.fencing_token, value="stale"
        )


asyncio.run(main())
