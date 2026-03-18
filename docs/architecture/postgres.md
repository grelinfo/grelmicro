# PostgreSQL Backend

This page documents the internal design of the PostgreSQL [Synchronization Backend](../sync.md#backend).

## Timestamp Strategy

The PostgreSQL backend uses **server-side timestamps** via `NOW()` and `make_interval()`. This differs from the SQLite and Kubernetes backends, which use Python-side `time.time()`.

### Why server-side time

- **Clock authority**: The database server is the single source of truth for time, eliminating clock skew between application instances.
- **Microsecond precision**: PostgreSQL's `TIMESTAMP` type provides microsecond precision.
- **Atomic expiry calculation**: `NOW() + make_interval(secs => $1)` computes the expiry within the same transaction, ensuring consistency.

## Lock Cleanup

On exit (`__aexit__`), the backend runs a single bulk query to delete all expired locks:

```sql
DELETE FROM {table_name} WHERE expire_at < NOW();
```

This removes all stale rows in one operation before closing the connection pool.
