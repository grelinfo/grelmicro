# Resilience

The `resilience` package provides higher-order functions (decorators) that implement resilience patterns to improve fault tolerance and reliability in distributed systems.


- **[Circuit Breaker](#circuit-breaker)**: Automatically detects repeated failures and temporarily blocks calls to unstable services, allowing them time to recover.

!!! note
    Additional resilience patterns may be added in the future. Currently, the circuit breaker is the primary mechanism provided for building robust microservices.

## Circuit Breaker

A **Circuit Breaker** prevents repeated failures when calling unreliable services. It monitors call outcomes and, after too many consecutive failures, "opens" to block further calls for a period, allowing recovery.

**Why Circuit Breakers?**

- Prevent cascading failures
- Improve stability and user experience
- Provide observability into service health

### State Machine

The Circuit Breaker has three normal states and two manual (forced) states:

| State         | Description                                                        |
|---------------|--------------------------------------------------------------------|
| **CLOSED**        | Normal operation, calls are allowed.                               |
| **OPEN**          | Calls are blocked to allow recovery.                              |
| **HALF_OPEN**     | Allows limited calls to test if the service has recovered.         |
| **FORCED_OPEN**   | Manual state to block calls regardless of health checks.          |
| **FORCED_CLOSED** | Manual state to allow calls regardless of health checks.          |

### Usage

```python
{!> ../examples/resilience/circuitbreaker.py!}
```

!!! warning
    **Thread Safety:** The Circuit Breaker is not thread-safe. Decorated sync functions or `from_thread` methods will ensure state change logic runs safely within the async event loop. Threaded usage is supported only in AnyIO worker threads and may be slower than pure async usage.
