---
title: "The same query is fast on a fresh connection and slow forever after the fifth run"
date: 2026-08-22
tags:
  - target/postgres
  - layer/database
  - symptom/latency
status: solved
severity: P2
env: "PostgreSQL 15 / pgjdbc 42.7 with server-side prepared statements / orders table ~180M rows"
symptom: "duration: 4231.882 ms  plan: Seq Scan on orders  (cost=0.00..3921884.00 rows=612 width=148)"
root_cause: "After five executions PostgreSQL switched the prepared statement to a generic plan built on average selectivity. A backfill had made the status column extremely skewed, so the generic plan chose a sequential scan for the selective value the application actually queries."
---

*Example note — the format applied to a well-documented failure mode, not a specific engagement.*

## Symptom

- **Since when:** p99 on the orders API started climbing three days after a data backfill, with no deploy in between. It got worse over the following week as connections lived longer.
- **Reproduces when:** the same prepared statement is executed six or more times on one connection. Execution six onward takes ~4s where the first five took ~20ms.
- **Does not reproduce when:** running the query as plain SQL from psql, or immediately after a connection is established. Both are fast, every time, which is why the first day of investigation found nothing.

There is no error here — this note is titled and tagged by latency, and the verbatim line is from `auto_explain` rather than from an error log:

```text
duration: 4231.882 ms  plan:
Seq Scan on orders  (cost=0.00..3921884.00 rows=612 width=148) (actual time=1204.551..4231.402 rows=57 loops=1)
  Filter: (status = $1)
  Rows Removed by Filter: 179428913
```

The estimate says 612 rows and the plan is a sequential scan over 180 million. Those two facts do not belong in the same plan, and that mismatch is the whole diagnosis.

## Environment

| | |
| --- | --- |
| Product / version | PostgreSQL 15.4, pgjdbc 42.7 with server-side prepared statements enabled |
| Deployment | primary + one read replica, HikariCP with `maxLifetime` 30 minutes |
| Scale | orders table ~180M rows, ~400 queries/s against it |
| **Last change** | **a backfill three days earlier moved the `status` distribution from roughly 60/40 to 99.5% `done` / 0.5% everything else. No code, schema, index, or configuration change.** |

Worth stating plainly because it is the first note here where it is true: **the change was data, not a deploy.** Every "what shipped recently" search came back empty, and the change that caused this was sitting in a migration job's output the whole time.

## Investigation

| Hypothesis | How it was checked | Verdict |
| --- | --- | --- |
| The index on `status` is missing or was dropped | `\d orders` — the partial index exists, is valid, and `EXPLAIN` from psql uses it | rejected |
| Table bloat, autovacuum falling behind after the backfill | `n_dead_tup` at 0.3%, `last_autovacuum` two hours old. Bloat was plausible given a backfill and was the first thing everyone assumed | rejected |
| Stale statistics after the bulk update | `ANALYZE orders` run manually. Statistics were already current, and it changed nothing | rejected |
| Lock contention or replica lag | The slow execution is slow with the table otherwise idle, on the primary, with no waiters in `pg_locks` | rejected |
| The plan depends on how many times the statement has run on that connection | `EXPLAIN (ANALYZE) EXECUTE` six times in a row — the plan changes on the sixth | **accepted** |

The reproduction takes four lines and no load, and it is the thing to reach for whenever "fast from psql, slow from the app" survives the first round of checks:

```sql
PREPARE p(text) AS SELECT id, total, updated_at FROM orders WHERE status = $1;
EXPLAIN (ANALYZE) EXECUTE p('pending');   -- runs 1-5: Index Scan, ~20ms
EXPLAIN (ANALYZE) EXECUTE p('pending');
-- ... on the 6th: Seq Scan, ~4s
```

`SET plan_cache_mode = force_generic_plan;` produces the bad plan on execution one, which turns a six-step reproduction into a one-step one and was how the fix was verified.

## Root cause

PostgreSQL plans a parameterised prepared statement with the actual values for its first five executions, then builds a generic plan with no values and compares costs — see [[concepts/generic-plans|custom plans and generic plans]]. The generic plan uses average selectivity across the column.

After the backfill, "average" meant 99.5% `done`, so the generic plan was costed as if it would return most of the table, and a sequential scan looked reasonable. The application almost never queries `done`; it queries `pending`, which matches 57 rows out of 180 million. The generic plan was built for a value the application does not use, and once chosen it is never reconsidered for the life of that prepared statement.

Nothing was misconfigured, no statistics were stale, and the planner's decision was correct given what it was asked. The data changed underneath a mechanism that had been making a good choice for a year.

## Fix

Immediate, per-role so it needs no deploy:

```sql
ALTER ROLE app_service SET plan_cache_mode = 'force_custom_plan';
```

Existing connections keep their cached plans, so either wait out `maxLifetime` or recycle the pool. p99 returned to baseline within the 30-minute window.

The driver-side alternative, which disables server-side prepared statements altogether:

```text
jdbc:postgresql://db.example.internal:5432/app?prepareThreshold=0
```

Note that this is **the same knob** that fixes [[problems/prepared-statement-errors-appear-only-under-concurrency|the prepared-statement errors under transaction pooling]], for an entirely unrelated reason. That coincidence is convenient and misleading in equal measure: setting it once and forgetting why makes the next person believe the two problems are the same one.

The targeted fix, applied afterwards, is to stop asking the planner to average a bimodal column at all:

```sql
CREATE INDEX CONCURRENTLY orders_pending_idx ON orders (updated_at) WHERE status <> 'done';
```

## Prevention

- **Detection:** alert on `pg_stat_statements` `mean_exec_time` rising while `calls` is flat — a plan flip shows up there immediately and shows up nowhere else. `auto_explain` with `log_min_duration = 1s` and `log_nested_statements` gives the plan itself, which is what turned a hypothesis into a confirmed cause here.
- **Prevention:** treat bulk data changes as changes. Backfills, migrations and retention jobs that move a column's distribution belong in the same change log as deploys, because the "last change" question is otherwise answered wrongly and confidently.
- **Remaining debt:** `force_custom_plan` was set for the whole application role, which also disables generic plans for the many statements that were benefiting from them. The added planning cost was never measured — it was assumed small because latency looked fine afterwards, which is not the same as having checked.

## Open questions

- Would extended statistics on `status` have let the planner cost the generic plan correctly and avoided the flip entirely? Never tested. It is the better fix if it works and nobody has spent the hour.
- Some pods never went slow. The assumption is that their connections were recycled before any single prepared statement reached five executions, which fits `maxLifetime` — but executions-per-statement-per-connection was never measured, so this is a story that fits rather than a fact.
- Whether the same flip is waiting on other queries against other columns the backfill touched. Only the one that showed up in p99 was examined, and a query that went from 20ms to 200ms would not have been noticed at all.

## Related

Parent concept: [[concepts/generic-plans|custom plans and generic plans]]. Filed under Database in the [[maps/incidents-moc|incident map]] — the first note here whose cause is actually in the database, after two where everything pointed at it and the cause was elsewhere.

Sibling: [[problems/prepared-statement-errors-appear-only-under-concurrency|"prepared statement does not exist" under transaction pooling]]. Both involve server-side prepared statements, both change behaviour on the fifth execution, and both are fixed by `prepareThreshold=0` — and they have nothing else in common. That one is a pooler handing a statement to a connection that never prepared it; this one is the planner making a permanent decision on the basis of a distribution the application does not query. Two different fives, and confusing them leads to disabling a feature without understanding either.

Contrast with [[problems/tls-fails-from-services-but-the-site-loads-fine-in-a-browser|the incomplete certificate chain]]: there the trigger was a scheduled renewal executing a six-week-old change. Here there was no change to find at all in the usual places, because the thing that changed was the data.
