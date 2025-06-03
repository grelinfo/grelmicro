from grelmicro.resilience.circuitbreaker import CircuitBreaker

circuit_breaker = CircuitBreaker(
    "system_name", ignore_exceptions=FileNotFoundError
)


# --- As context manager ---
async def async_context_manager():
    async with circuit_breaker:
        print("Calling external service (async)...")


# --- As decorator ---
@circuit_breaker
async def async_call():
    print("Calling external service (async)...")


# --- As context manager within AnyIO worker thread ---
def sync_context_manager():
    with circuit_breaker.from_thread:
        print("Calling external service (thread)...")


# --- As decorator within AnyIO worker thread ---
@circuit_breaker
def sync_call():
    print("Calling external service (thread)...")
