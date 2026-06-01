from grelmicro.resilience import Bulkhead, BulkheadFullError


async def process(order_id: str) -> str:
    return order_id


# Bound concurrent checkouts to 50. A caller waits up to 2s for a free
# permit, then is rejected.
checkout = Bulkhead("checkout", max_concurrent=50, max_wait=2.0)


async def handle(order_id: str) -> str:
    try:
        async with checkout:
            return await process(order_id)
    except BulkheadFullError:
        return "busy"
