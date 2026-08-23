---
title: "Lock queues"
description: "A lock that is merely being waited for blocks as effectively as one that is held."
tags:
  - meta
---

PostgreSQL's lock conflict matrix is the part everyone learns: `SELECT` takes `ACCESS SHARE`, `ALTER TABLE` takes `ACCESS EXCLUSIVE`, and those two conflict. Many readers coexist happily; a writer of the schema excludes everyone.

The part that causes outages is the **queue**.

Locks are granted in request order, and a new request that conflicts with an **already-waiting** request must wait behind it. It does not matter that the new request would be perfectly compatible with what is currently *held*:

```text
session A   ACCESS SHARE        held        — an idle transaction, holding, doing nothing
session B   ACCESS EXCLUSIVE    waiting     — ALTER TABLE, blocked by A
session C   ACCESS SHARE        waiting     — a plain SELECT, compatible with A,
                                              blocked by B, which holds nothing at all
```

Session C is stopped by a lock that has not been granted. Every subsequent reader joins the same queue, so one idle transaction plus one DDL statement takes a table completely offline — while the database itself does no work at all. **CPU and I/O near zero is consistent with a total outage here**, and that combination is otherwise so unusual that it is worth treating as a signature.

## Seeing it

```sql
SELECT pid, state, wait_event_type, wait_event,
       now() - xact_start AS xact_age, left(query, 60) AS query
FROM pg_stat_activity
WHERE datname = current_database()
ORDER BY xact_start;

SELECT pg_blocking_pids(pid), pid, left(query, 60) FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0;
```

The head of the queue is the interesting row, and it is usually not the one anyone is looking at: the DDL is the *visible* blocker, but the thing blocking the DDL is generally a session in `idle in transaction` that has been sitting there for a very long time doing nothing.

## Bounding it

**`lock_timeout` before every DDL statement.** One line, and it converts the failure from an outage into a failed migration:

```sql
SET lock_timeout = '2s';
ALTER TABLE orders ADD COLUMN discount_cents integer;
-- ERROR:  canceling statement due to lock timeout
```

A migration that fails is retried five minutes later at no cost. A migration that waits builds a queue behind it for as long as it waits — see [[problems/every-query-on-one-table-stopped-and-the-migration-had-not-started-yet|a table that went offline behind a statement that never ran]].

**`idle_in_transaction_session_timeout`** removes the usual blocker class outright, and is worth setting per role for anything interactive or analytical:

```sql
ALTER ROLE analytics SET idle_in_transaction_session_timeout = '60s';
```

Neither setting makes the lock conflict go away. They decide whether an unlucky moment costs one migration or one table.
