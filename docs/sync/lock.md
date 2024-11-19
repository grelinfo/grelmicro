# Lock

Specifications:
- Async: The lock must be acquired and released asynchronously.
- Distributed: The lock must be distributed across multiple workers.
- Reentrant: The lock must allow the same token to acquire it multiple times to extend the lease.
- Expiring: The lock must have a timeout to auto-release after an interval to prevent deadlocks.
- Non-blocking: Lock operations must not block the async event loop.
- Vendor-agnostic: Must support multiple backends (Redis, Postgres, ConfigMap, etc.).

Notes:
- Not Thread-Safe: This lock is not thread-safe. It is designed to be used in async tasks within the same event loop.
