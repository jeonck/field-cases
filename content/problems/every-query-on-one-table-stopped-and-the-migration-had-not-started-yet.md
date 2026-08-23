---
title: "Every query on one table stopped, and the migration that caused it had not started yet"
date: 2026-08-22
tags:
  - target/postgres
  - layer/database
  - symptom/outage
status: solved
severity: P1
env: "PostgreSQL 15 / migration adding a nullable column / a BI tool connected to the primary two weeks earlier"
symptom: "FATAL: remaining connection slots are reserved for non-replication superuser connections"
root_cause: "An idle-in-transaction session held ACCESS SHARE on the table. The migration's ACCESS EXCLUSIVE request queued behind it, and every subsequent SELECT queued behind the migration — blocked by a lock that was never granted."
---

*Example note — the format applied to a well-documented failure mode, not a specific engagement.*

## Symptom

- **Since when:** 11:04, thirty seconds into a routine deploy. The site was fully down for eight minutes for anything touching one table.
- **Reproduces when:** DDL runs against a table on which any session holds an open transaction. Reproducible in seconds: `BEGIN; SELECT 1 FROM orders LIMIT 1;` in one session, then the migration in another, then any `SELECT` in a third.
- **Does not reproduce when:** no long-lived transaction happens to be open. The same migration had been run against staging and against production tables a dozen times without incident, which is why it was considered safe.

The application saw connection exhaustion, which is a consequence and not the cause:

```text
FATAL:  remaining connection slots are reserved for non-replication superuser connections
```

The database's own view of itself, at the same moment:

```text
  pid  | state               | wait_event_type | wait_event | xact_age  | query
-------+---------------------+-----------------+------------+-----------+---------------------------
 14821 | idle in transaction |                 |            | 00:47:12  | SELECT order_id, total ...
 20104 | active              | Lock            | relation   | 00:00:31  | ALTER TABLE orders ADD ...
 20233 | active              | Lock            | relation   | 00:00:29  | SELECT * FROM orders WH...
 20241 | active              | Lock            | relation   | 00:00:29  | SELECT status FROM orde...
  … 180 more, all Lock/relation
```

CPU was at 4%, disk I/O near zero, and the database was completely down. **An idle database and a total outage at the same time** is a combination almost nothing else produces.

## Environment

| | |
| --- | --- |
| Product / version | PostgreSQL 15.4, `max_connections` 200, no `lock_timeout` set anywhere |
| Deployment | migrations run from the deploy pipeline against the primary |
| Scale | ~1.5k queries/s against the `orders` table |
| **Last change** | **a BI dashboard was pointed at the primary two weeks earlier. Its client runs with autocommit off, so every query leaves a transaction open until the tool is closed. Zero impact for two weeks.** |

Neither half is an incident. Open transactions from a reporting tool are invisible until something needs an exclusive lock; a metadata-only `ADD COLUMN` is genuinely instant until something is in the way. The deploy supplied the second half.

## Investigation

| Hypothesis | How it was checked | Verdict |
| --- | --- | --- |
| The migration is rewriting the table | Adding a nullable column is metadata-only on PostgreSQL 15 — and more decisively, `pg_stat_activity` shows the statement **waiting on a lock**, not executing | rejected |
| The application has a connection leak | Connections were exhausted, but every one of them was in `Lock/relation`. The pool was full of blocked queries, not leaked ones | rejected — effect, not cause |
| A failover or replication problem | One primary, no failover events, replicas following normally | rejected |
| The database is saturated | CPU 4%, I/O idle. Saturation does not look like this and this fact should have redirected the investigation five minutes sooner | rejected |
| Readers are queued behind a lock request that has not been granted | `pg_blocking_pids()` traces the whole queue to one session idle in transaction for 47 minutes | **accepted** |

```sql
SELECT pid, pg_blocking_pids(pid), left(query, 40)
FROM pg_stat_activity
WHERE cardinality(pg_blocking_pids(pid)) > 0;
```

Everything traced to a single 47-minute-old idle transaction — see [[concepts/lock-queues|lock queues]]. The person holding it had run a query before lunch.

## Root cause

`SELECT` takes `ACCESS SHARE`. `ALTER TABLE` takes `ACCESS EXCLUSIVE`. Those conflict, so the migration waited — which is expected and would have been harmless on its own.

What is not intuitive is that PostgreSQL grants locks in request order, so a request conflicting with an **already-waiting** request also waits. Every `SELECT` arriving after the migration conflicted with the migration's pending `ACCESS EXCLUSIVE` and joined the queue, even though it was entirely compatible with the `ACCESS SHARE` actually held.

So the outage was caused by a lock that was never granted, on behalf of a statement that never executed, blocked by a session that was doing nothing. Then the queue consumed the connection pool, and the connection errors are what everyone saw first.

## Fix

Immediate — cancel the head of the queue, which releases everything behind it at once:

```sql
SELECT pg_cancel_backend(20104);        -- the waiting ALTER
```

The queue drained in under a second. Terminating the idle session instead would also have worked and would have let the migration proceed; cancelling the migration was chosen because it is the statement whose failure is free.

The durable fix is one line in the migration tooling, and it is the entire lesson:

```sql
SET lock_timeout = '2s';
ALTER TABLE orders ADD COLUMN discount_cents integer;
```

A migration that cannot get its lock within two seconds now fails:

```text
ERROR:  canceling statement due to lock timeout
```

which is retried on the next deploy at no cost. Without it, a migration waits — and every second it waits, it lengthens a queue of readers behind it.

Second, remove the blocker class at the source:

```sql
ALTER ROLE analytics SET idle_in_transaction_session_timeout = '60s';
```

## Prevention

- **Detection:** two cheap queries on a schedule — sessions in `idle in transaction` older than 60 seconds, and any session waiting on a lock for more than 5 seconds. The first is the loaded gun and the second is the trigger being pulled. Neither existed.
- **Prevention:** `lock_timeout` is set by the migration framework for every DDL statement, not left to the author of a migration to remember. This is the control that makes the failure bounded regardless of what else is true.
- **Remaining debt:** the BI tool's connection behaviour was not changed — it belongs to another team and the `idle_in_transaction_session_timeout` on the role is a mitigation applied from this side. Any new tool connecting with autocommit off reintroduces the same blocker, and nothing detects that at connection time.

## Open questions

- `lock_timeout` was set in the migration framework rather than on the deploy role, because roles are shared with things that legitimately wait. Whether the framework covers every path that issues DDL — including one-off scripts run by hand during incidents — was not audited.
- The analytics tool's open transactions are assumed read-only. Nobody verified it. A read-write transaction left idle for 47 minutes is a considerably worse problem than this one and would present differently, so the assumption is load-bearing and untested.
- Nobody knows whether other teams' migration pipelines set `lock_timeout`. The framework change covers this one repository; the question was raised, written down, and not pursued.

## Related

Parent concept: [[concepts/lock-queues|lock queues]]. Filed under Database in the [[maps/incidents-moc|incident map]].

Sibling: [[problems/the-same-query-is-fast-on-a-fresh-connection-and-slow-after-the-fifth-run|the query plan that flipped after five executions]]. Both are PostgreSQL incidents where the database did exactly what it is documented to do and the cause was an interaction nobody had modelled — and they fail in opposite directions worth holding side by side. There the database was working extremely hard on the wrong plan; here it was doing nothing at all while being completely unavailable. "The database is idle" rules out one of them and is a positive indicator for the other.

Contrast with [[problems/a-40-second-blip-became-a-25-minute-outage|the retry amplification]]: both incidents are one harmless latent condition plus one routine event, and in both the fix belongs somewhere other than where the pain appeared — retries at the callers rather than capacity at the saturated service, `lock_timeout` on the migration rather than anything about the table that stopped answering. The instinct to fix the thing that is visibly broken is wrong in both, and it is wrong for the same reason.
