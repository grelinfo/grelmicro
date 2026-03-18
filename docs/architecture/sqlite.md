# SQLite Backend

This page documents the internal design of the SQLite [Synchronization Backend](../sync.md#backend).

## Timestamp Strategy

The SQLite backend uses **Python-side wall-clock timestamps** (`time.time()`) stored as `REAL` rather than SQLite-side time functions. This differs from the PostgreSQL backend, which uses server-side `NOW()`.

### Why not SQLite time functions

- **Millisecond precision only**: `unixepoch('now', 'subsec')` provides millisecond precision, while PostgreSQL's `NOW()` provides microsecond precision.
- **SQLite version dependency**: `unixepoch('now', 'subsec')` requires SQLite 3.38.0+ (2022-02-22), which may not be available on all Python 3.11 platforms.
- **No clock skew concern**: SQLite is always local to the application (same machine), so there is no benefit to using database-side time, unlike PostgreSQL where the database may run on a separate server.

### Why not `time.monotonic()`

- **Incompatible with persistence**: Monotonic clock values have an undefined reference point that resets on system reboot. A lock expiry stored with `monotonic()` becomes meaningless after a process restart, breaking SQLite's persistence advantage.
- **Not cross-process comparable**: Multiple processes on the same machine sharing a SQLite file would each have independent monotonic origins, making expiry comparisons incorrect.

### Why `time.time()`

- **Microsecond precision**: Matches PostgreSQL's `TIMESTAMP` precision.
- **Persistence-safe**: Wall-clock timestamps remain meaningful across process restarts and between processes.
- **Industry standard**: All major Python lock libraries (`redis-py`, `python-dynamodb-lock`) use wall-clock time for persisted expiry.
- **Consistency**: The in-memory backend correctly uses `time.monotonic()` (never persisted, single process). The SQLite backend correctly uses `time.time()` (persisted, potentially multi-process). Each backend uses the appropriate clock for its storage model.

### Trade-offs

`time.time()` can go backwards on NTP step corrections or leap seconds. This is acceptable because lock durations are typically seconds to minutes, and the SQLite backend targets home lab and local testing environments.

## WAL Mode

The backend enables [Write-Ahead Logging (WAL)](https://www.sqlite.org/wal.html) on connection with `PRAGMA journal_mode=WAL`. Without WAL, SQLite uses a rollback journal where writers block readers and readers block writers. WAL allows concurrent reads and writes, which is important for async lock operations where multiple tasks may check or acquire locks simultaneously.

WAL mode is persistent per database file. Once enabled, it remains active for all subsequent connections until explicitly changed.
