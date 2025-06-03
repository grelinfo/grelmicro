# Resilience

The `resilience` package in Grelmicro provides higher-order functions (decorators) to build robust, fault-tolerant microservices.

## Circuit Breaker

A **Circuit Breaker** is a design pattern that helps prevent repeated failures when calling unreliable external services. It monitors the outcome of calls and, if too many failures occur, it "opens" the circuit to block further calls for a period of time. This allows the external service time to recover and prevents your system from being overwhelmed by repeated failures.

### Features
- **Async and Sync Support**: Use as an async context manager, decorator, or with thread-based code.
- **Configurable Thresholds**: Set error and success thresholds, reset timeouts, and half-open capacity.
- **Exception Handling**: Ignore specific exceptions or treat them as errors.
- **Metrics**: Access detailed metrics for monitoring and observability.


!!! warning
    The lock is designed for use within an async event loop and is not thread-safe or process-safe.

### Usage

#### As an Async Context Manager
```python
from grelmicro.resilience.circuitbreaker import CircuitBreaker

cb = CircuitBreaker("external_api")

async def call_api():
    async with cb:
        # Your code here
        ...
```

#### As a Decorator
```python
@cb
def my_function():
    ...
```

#### With Threaded Code
```python
cb = CircuitBreaker("external_api")

def sync_code():
    with cb.from_thread.protect():
        ...
```

#### Configuration
- `error_threshold`: Number of consecutive errors before opening the circuit (default: 5)
- `success_threshold`: Number of consecutive successes in HALF_OPEN before closing (default: 2)
- `reset_timeout`: Seconds to wait before trying again after opening (default: 30)
- `half_open_capacity`: Max concurrent calls in HALF_OPEN (default: 1)
- `ignore_exceptions`: Exceptions to ignore (do not count as errors)

#### Example
```python
from grelmicro.resilience.circuitbreaker import CircuitBreaker

cb = CircuitBreaker("integration_point", ignore_exceptions=FileNotFoundError)

@cb
def get_data():
    ...
```

### Metrics
You can access circuit breaker metrics for monitoring:
```python
metrics = cb.metrics()
print(metrics.state, metrics.total_error_count)
```

---

## Why Use Circuit Breakers?
- Prevents cascading failures in distributed systems
- Improves system stability and user experience
- Provides observability into external service health

---

## More Resilience Patterns
Grelmicro may add more resilience primitives in the future. For now, the circuit breaker is the main tool for building robust, production-grade microservices.

---

**See also:** [Task Scheduler](./task.md), [Synchronization Primitives](./sync.md)