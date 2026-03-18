# SQLite Backend

This page documents the internal design of the SQLite [Synchronization Backend](../sync.md#backend).

## WAL Mode

The backend enables [Write-Ahead Logging (WAL)](https://www.sqlite.org/wal.html) on connection with `PRAGMA journal_mode=WAL`. Without WAL, SQLite uses a rollback journal where writers block readers and readers block writers. WAL allows concurrent reads and writes, which is important for async lock operations where multiple tasks may check or acquire locks simultaneously.

WAL mode is persistent per database file. Once enabled, it remains active for all subsequent connections until explicitly changed.
