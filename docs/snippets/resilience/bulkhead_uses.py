from grelmicro.coordination import Lock
from grelmicro.providers.redis import RedisProvider
from grelmicro.resilience import Bulkhead

# A dedicated Redis for checkout, isolated from the app's default
# backends. The bulkhead opens it on first entry, scopes every kind it
# serves, and closes it at app shutdown.
checkout_redis = RedisProvider("redis://localhost:6379/1")
checkout = Bulkhead(
    "checkout",
    max_concurrent=50,
    uses=[checkout_redis],
)

cart_lock = Lock("cart")


async def handle_checkout(cart_id: str) -> None:
    # `cart_lock` has no explicit backend, so inside the scope it
    # resolves to the bulkhead's dedicated lock backend.
    async with checkout, cart_lock:
        ...
