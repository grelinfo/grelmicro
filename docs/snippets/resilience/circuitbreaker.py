from grelmicro.resilience import CircuitBreaker, Match

circuit_breaker = CircuitBreaker.consecutive_count(
    "system_name", when=Match.not_exception(FileNotFoundError)
)


async def async_context_manager():
    async with circuit_breaker:
        print("Calling external service...")


@circuit_breaker
async def async_call():
    print("Calling external service...")


def sync_context_manager():
    with circuit_breaker.from_thread:
        print("Calling external service from a worker thread...")


@circuit_breaker
def sync_call():
    print("Calling external service from a worker thread...")
