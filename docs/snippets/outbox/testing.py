import asyncio

from grelmicro.outbox import Message, Outbox
from grelmicro.outbox.memory import MemoryOutboxAdapter

outbox = Outbox(MemoryOutboxAdapter())
delivered = asyncio.Event()


@outbox.handler("email.welcome")
async def send_welcome(message: Message) -> None:
    print(f"welcome {message.payload['to']}")
    delivered.set()


async def main() -> None:
    # The in-memory backend needs no transaction, so the handle is None.
    async with outbox, asyncio.timeout(5):
        await outbox.publish(None, "email.welcome", {"to": "alice@example.com"})
        await delivered.wait()


if __name__ == "__main__":
    asyncio.run(main())
