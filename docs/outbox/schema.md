# Schema

The Postgres adapter uses one table. Column names follow the common outbox convention, so change-data-capture tooling can read it directly:

| column | type | purpose |
|---|---|---|
| `id` | `uuid` primary key | stable message id and idempotency key, time-ordered UUIDv7 |
| `topic` | `text` | routes to the handler |
| `key` | `text` null | ordering or partition key |
| `payload` | `jsonb` | the message body |
| `headers` | `jsonb` | metadata and trace context |
| `dedup_key` | `text` null | producer-side deduplication |
| `attempts` | `int` | delivery attempt counter |
| `available_at` | `timestamptz` | when the message is next actionable, for delay, retry, and lease |
| `state` | `text` | `pending`, `processing`, `delivered`, or `dead` |
| `last_error` | `text` null | the last handler error, for dead messages |
| `delivered_at` | `timestamptz` null | delivery time, set on success, anchors the retention window |
| `created_at` | `timestamptz` | staged time |

Ids are time-ordered UUIDv7, so the claim orders by `(available_at, id)` for stable, index-friendly delivery. A partial index on `available_at` for non-terminal rows serves the claim query, and a unique partial index on `dedup_key` (where it is set) backs deduplication. The table is created on first connect unless `auto_migrate=False`, guarded so replicas booting together do not race the DDL.

## Managing the schema with Alembic

Set `auto_migrate=False` and run the DDL from your own migration. `PostgresOutboxAdapter` returns the exact statements the outbox runs, so your migration never drifts from the library:

```python
from grelmicro.outbox.postgres import PostgresOutboxAdapter


def upgrade() -> None:
    op.execute(PostgresOutboxAdapter.create_table_sql())


def downgrade() -> None:
    op.execute(PostgresOutboxAdapter.drop_table_sql())
```

Pass a table name to either method to match a custom `table`. If you prefer autogenerate, model the table from this DDL in your own metadata.

Set `table` and `auto_migrate` on the component, see
[Configuration](relay.md#configuration).
