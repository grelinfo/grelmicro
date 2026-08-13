# SQLite Backend

This page documents the internal design of the SQLite [Coordination backend](../coordination/index.md#backends).

## One Connection

`SQLiteProvider` owns the connection and a lock that serializes it. Every adapter on the same provider borrows both, so an app running a lock, a schedule, and a cache against one file holds a single connection. Writes run inside a `BEGIN IMMEDIATE` transaction: the provider's lock serializes them within the process, and the transaction's write lock serializes them across processes sharing the file.

## WAL Mode

The provider enables [Write-Ahead Logging (WAL)](https://www.sqlite.org/wal.html) on connection with `PRAGMA journal_mode=WAL`. Without WAL, SQLite uses a rollback journal where writers block readers and readers block writers. WAL allows concurrent reads and writes, which is important for async lock operations where multiple tasks may check or acquire locks simultaneously.

WAL mode is persistent per database file. Once enabled, it remains active for all subsequent connections until explicitly changed.
