import asyncio
import logging

from grelmicro.log import configure
from grelmicro.trace import add_context, instrument, span

configure(format="JSON")
logger = logging.getLogger(__name__)


@instrument
async def process_order(order_id: str, user_id: str) -> None:
    logger.info("started")

    add_context(payment_status="pending")
    logger.info("payment initiated")

    with span("db_query", table="orders"):
        logger.info("querying")

    # `table` is gone with the span, `payment_status` is still here.
    logger.info("done")


if __name__ == "__main__":
    asyncio.run(process_order(order_id="ORD-1", user_id="USR-1"))
