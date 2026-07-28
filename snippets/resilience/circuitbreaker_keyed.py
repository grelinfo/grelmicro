from grelmicro.resilience import CircuitBreaker

# One breaker, one independent circuit per tenant.
upstream = CircuitBreaker.consecutive_count("upstream", error_threshold=5)


async def fetch(tenant: str) -> None:
    async with upstream.keyed(tenant):
        print(f"Calling upstream for {tenant}...")


@upstream.keyed("acme")
async def fetch_acme() -> None:
    print("Calling upstream for acme...")


async def quarantine(tenant: str) -> None:
    # Blocks this tenant only, every other tenant keeps calling.
    await upstream.keyed(tenant).isolate()
    print(upstream.keyed(tenant).state)


async def release(tenant: str) -> None:
    await upstream.keyed(tenant).reset()
