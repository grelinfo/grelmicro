# SQLite Backend

This page documents the internal design of the SQLite [Coordination backend](../coordination/index.md#backends).

## One Connection

`SQLiteProvider` owns the connection and a lock that serializes it. Every adapter on the same provider borrows both, so an app running a lock, a schedule, and a cache against one file holds a single connection. Writes run inside a `BEGIN IMMEDIATE` transaction: the provider's lock serializes them within the process, and the transaction's write lock serializes them across processes sharing the file.

Installing a table takes the same lock. Components open in registration order, and one of them may already have a transaction open on the shared connection when the next one creates its tables. `executescript` commits before it runs, so a schema init that skipped the lock would end that transaction and fail.

## Several Processes

Two processes may open the same file. SQLite allows one writer at a time and refuses the others, so the provider sets `PRAGMA busy_timeout` on every connection it opens. A write that finds the file busy then retries until the timeout instead of failing right away.

The busy timeout does not cover the WAL switch below. Changing the journal mode needs the file to itself, and SQLite reports it as locked immediately rather than waiting. The provider retries that one statement itself, so two processes starting together on a new file both open.

## WAL Mode

The provider enables [Write-Ahead Logging (WAL)](https://www.sqlite.org/wal.html) on connection with `PRAGMA journal_mode=WAL`. Without WAL, SQLite uses a rollback journal where writers block readers and readers block writers. WAL allows concurrent reads and writes, which is important for async lock operations where multiple tasks may check or acquire locks simultaneously.

WAL mode is persistent per database file. Once enabled, it remains active for all subsequent connections until explicitly changed.
