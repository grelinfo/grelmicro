from grelmicro.outbox import Message, Outbox
from grelmicro.outbox.memory import MemoryOutboxAdapter

outbox = Outbox(MemoryOutboxAdapter())


@outbox.handler("email.welcome")
async def send_welcome(message: Message) -> None:
    print(f"welcome {message.payload['to']}")


async def main() -> None:
    # The in-memory backend needs no transaction, so the handle is None.
    async with outbox:
        await outbox.publish(None, "email.welcome", {"to": "alice@example.com"})
