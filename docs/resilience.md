# Resilience

The `resilience` package provides higher-order functions (decorators) for building robust, fault-tolerant microservices.

## Circuit Breaker

A **Circuit Breaker** prevents repeated failures when calling unreliable services. It monitors call outcomes and, after too many consecutive failures, "opens" to block further calls for a period, allowing recovery.

### Why Circuit Breakers?

- Prevent cascading failures
- Improve stability and user experience
- Provide observability into service health

### Features

- **Async & Sync**: Use as async context manager, decorator, or with threads.
- **Configurable**: Set error/success thresholds, reset timeouts, half-open capacity.
- **Exception Handling**: Ignore specific exceptions as errors.
- **Metrics**: Access detailed metrics for monitoring.

!!! Note
    **Thread Safety:** The Circuit Breaker is not thread-safe by default. Decorate sync functions or use `from_thread` sync methods to ensure logic runs safely within the async event loop. Threaded usage is supported only in AnyIO worker threads and may be slower than pure async usage.

## Usage

=== "Async Context Manager"
    ```python
    from grelmicro.resilience.circuitbreaker import CircuitBreaker

    cb = CircuitBreaker("external_api")

    async def call_api():
        async with cb:
            ...
    ```

=== "Decorator"
    ```python
    @cb
    def my_function():
        ...
    ```

=== "Threaded Code"
    ```python
    cb = CircuitBreaker("external_api")

    def sync_code():
        with cb.from_thread.protect():
            ...
    ```

### Configuration

- `error_threshold`: Consecutive errors before opening (default: 5)
- `success_threshold`: Successes in HALF_OPEN before closing (default: 2)
- `reset_timeout`: Seconds to wait before retry (default: 30)
- `half_open_capacity`: Max concurrent calls in HALF_OPEN (default: 1)
- `ignore_exceptions`: Exceptions to ignore
- `log_level`:

### Example

```python
from grelmicro.resilience.circuitbreaker import CircuitBreaker

cb = CircuitBreaker("integration_point", ignore_exceptions=FileNotFoundError)

@cb
def get_data():
    ...
```

### Metrics

```python
metrics = cb.metrics()
print(metrics.state, metrics.total_error_count)
```



---

## More Resilience Patterns

More patterns may be implemented in the future. For now, the circuit breaker is the main tool for robust microservices.

---

**See also:** [Task Scheduler](./task.md), [Synchronization Primitives](./sync.md)
