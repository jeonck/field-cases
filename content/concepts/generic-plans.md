---
title: "Custom plans and generic plans"
description: "PostgreSQL replans a prepared statement five times, then decides it has seen enough — and that decision is permanent for the life of the connection."
tags:
  - meta
---

When PostgreSQL executes a **prepared statement** with parameters, it can plan it two ways:

- a **custom plan**, replanned on every execution with the actual parameter values, so the planner knows that `status = 'pending'` matches 0.1% of rows and `status = 'done'` matches 99%;
- a **generic plan**, planned once with no values at all, using average selectivity, and reused for free thereafter.

Custom plans are more accurate; generic plans skip the planning work. `plan_cache_mode = auto` — the default — tries to get both: it uses a custom plan for the **first five** executions, records their costs, then builds the generic plan and compares. If the generic plan is not estimated to be worse than the average custom plan, it switches, and **never reconsiders** for the lifetime of that prepared statement.

That comparison is made on estimates, so the failure mode is specific: when a predicate's selectivity varies wildly by value, the generic plan's "average" is a value that never actually occurs. It looks cheap on average and is catastrophic for the selective end of the distribution, which is usually the end the application queries most. Everything is fast for five executions and then permanently slow — the shape behind [[problems/the-same-query-is-fast-on-a-fresh-connection-and-slow-after-the-fifth-run|a query that is fast on a fresh connection and slow forever after]].

Three consequences worth remembering:

- **The switch is per prepared statement, per session.** A new connection starts the count again, which is why recycling connections "fixes" it and why the problem follows connection lifetime rather than load.
- **Nothing logs the transition.** The plan changes silently; only `EXPLAIN` on the sixth execution, or `auto_explain`, shows it.
- **Data drift alone can trigger it.** No deploy is required. A backfill that skews a column's distribution can flip a generic plan from acceptable to ruinous without a single line of code changing.

```sql
SET plan_cache_mode = force_generic_plan;   -- see the bad plan immediately
SET plan_cache_mode = force_custom_plan;    -- never switch; pay planning cost every time
SHOW plan_cache_mode;                       -- 'auto' by default
```

The five-execution threshold is a hard-coded constant, not a tunable. `plan_cache_mode` is the only lever, and it is all-or-nothing per session or per role.
