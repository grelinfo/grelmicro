import asyncio

from pydantic import BaseModel


# billing/events.py: the contract, shared by producer and consumer
class OrderPlaced(BaseModel):
    __outbox_topic__ = "billing.order_placed"

    order_id: int


# billing/handlers.py: imports nothing from the app
from grelmicro.outbox import Message, OutboxRouter

router = OutboxRouter()
charged = asyncio.Event()


@router.handler(OrderPlaced)
async def charge(message: Message[OrderPlaced]) -> None:
    assert message.data is not None  # a typed handler always gets its model
    print(f"{message.topic}: charge order {message.data.order_id}")
    charged.set()


# app.py: the wiring module includes the feature routers
from grelmicro.outbox import Outbox
from grelmicro.outbox.memory import MemoryOutboxAdapter

outbox = Outbox(MemoryOutboxAdapter(), routers=[router])


async def main() -> None:
    async with outbox, asyncio.timeout(5):
        # The producer knows the contract, not the router.
        # The in-memory backend needs no transaction, so the handle is None.
        await outbox.publish(None, OrderPlaced(order_id=42))
        await charged.wait()


if __name__ == "__main__":
    asyncio.run(main())
