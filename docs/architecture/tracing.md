# Tracing Architecture

## Context Stack

The tracing module uses a `contextvars`-based context stack defined in `grelmicro/_context.py`. This low-level module is shared by both `log` and `trace` so that neither depends on the other.

Each `@instrument` call or `span()` block pushes a new frame with its fields. Logging backends merge all active frames into the JSON record.

```
@instrument(order_id="ORD-1")     <- frame 1: {order_id: "ORD-1"}
  add_context(status="pending")   <- frame 1: {order_id: "ORD-1", status: "pending"}
  with span("db", table="users")  <- frame 2: {table: "users"}
    logger.info("query")          <- merged: {order_id, status, table}
  # frame 2 popped               <- merged: {order_id, status}
```

## Concurrency Safety

The stack is stored as a `ContextVar[tuple[dict[str, Any], ...]]`. Both the tuple and the dicts are replaced (never mutated) to ensure concurrent async tasks sharing a parent context are isolated:

- Each `asyncio.Task` gets a copy of the current context when created.
- `asyncio.to_thread()` also copies the context into the thread.
- `add_context()` replaces the top frame (not mutates it), so sibling tasks cannot interfere.

This follows the same pattern used by OpenTelemetry Python for `Context` propagation.

## Decoupled Layering

```
grelmicro/_context.py    <- owns ContextVar (no dependencies)
    ├── log/             <- imports _context (merge into log records)
    └── trace/           <- imports _context (push/pop spans, add_context)
```

The `_context` module has no imports from `log` or `trace`, preventing circular dependencies and allowing users to configure logging without loading the tracing module.
