from grelmicro.resilience.circuitbreaker import CircuitBreaker

cb = CircuitBreaker("system_name", ignore_exceptions=FileNotFoundError)


# --- As context manager ---
async def async_context_manager():
    async with cb.protect():
        print("Calling external service (async)...")


# --- As decorator ---
@cb.protect()
async def async_call():
    print("Calling external service (async)...")


# --- As context manager within AnyIO worker thread ---
def sync_context_manager():
    with cb.from_thread.protect():
        print("Calling external service (thread)...")


# --- As decorator within AnyIO worker thread ---
@cb.from_thread.protect()
def sync_call():
    print("Calling external service (thread)...")
