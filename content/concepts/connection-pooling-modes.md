---
title: "Connection pooling modes"
description: "Session, transaction, and statement pooling — and the list of things each one quietly takes away."
tags:
  - meta
---

A connection pooler in front of PostgreSQL — PgBouncer, pgcat, RDS Proxy — multiplexes many client connections onto few server connections. The mode decides **when a server connection goes back into the pool**, and that single choice determines what the application is still allowed to assume.

| Mode | Server connection returned | Connections saved | What breaks |
| --- | --- | --- | --- |
| `session` | when the client disconnects | little | nothing |
| `transaction` | at every `COMMIT` / `ROLLBACK` | a lot | anything that outlives a transaction |
| `statement` | after every statement | most | the above, plus multi-statement transactions |

Almost everyone runs `transaction`, because it is the mode that actually reduces connection count, and almost everyone discovers the second column the hard way.

## What "outlives a transaction" means

Under transaction pooling, two consecutive statements from the same client may run on **different** server connections, and one server connection serves **different** clients over time. Anything PostgreSQL scopes to a session therefore becomes unreliable:

- **Server-side prepared statements** — `PREPARE` lands on one connection, `EXECUTE` may not find it, or collides with another client's identically-named one. This is [[problems/prepared-statement-errors-appear-only-under-concurrency|the error that shows up only under concurrency]].
- **`SET` / `SET LOCAL`** — a plain `SET search_path` leaks to the next client on that connection, or vanishes before your next statement. `SET LOCAL` is transaction-scoped and safe.
- **Advisory locks** — `pg_advisory_lock()` is session-scoped. Taken on one connection and released from another, it is not a lock, it is a leak. Nothing errors.
- **`LISTEN` / `NOTIFY`** — the listener is bound to a session the pooler will reassign.
- **Cursors** declared `WITHOUT HOLD`, temporary tables, `WITH HOLD` snapshots — same story.

The dangerous half of that list is the half that does not raise an error. Prepared statements fail loudly and get fixed; a `SET search_path` bleeding into another tenant's queries does not, and looks like a data bug months later.

PgBouncer 1.21 added `max_prepared_statements`, which tracks and re-prepares statements per server connection and removes the first item from the list. It does nothing for the rest.
