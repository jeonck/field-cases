---
title: "Database CPU hits 100% for forty seconds, at a time that moves with each deploy"
date: 2026-08-22
tags:
  - target/redis
  - target/postgres
  - layer/capacity
  - symptom/latency
  - symptom/resource-exhaustion
status: solved
severity: P2
env: "Redis cache, 1h TTL / 40 application pods / PostgreSQL 15 / pricing aggregate query"
symptom: "pg_stat_statements: one query, calls +1,184 within a 40s window, mean_exec_time 8ms → 412ms"
root_cause: "All 40 pods cached one hot key with a fixed 1-hour TTL and were deployed together, so the entry expired for every process at the same instant and every in-flight request queried the origin simultaneously."
---

*Example note — the format applied to a well-documented failure mode, not a specific engagement.*

## Symptom

- **Since when:** six weeks earlier, immediately after the cache TTL was raised from 5 minutes to 1 hour. The change was made to reduce database load, and it did.
- **Reproduces when:** exactly once per hour, for about forty seconds, during which p99 across the whole API goes from 90 ms to 4 s.
- **Does not reproduce when:** off-peak. Below roughly half of peak traffic the same herd arrives and the database absorbs it in three seconds, which is invisible on any graph and had been happening for months.

The give-away is not the spike itself, it is the **clock it keeps**:

```text
week 1  spikes at 15:12, 16:12, 17:12, 18:12 …   (deploy that day: 14:12)
week 2  spikes at 10:40, 11:40, 12:40, 13:40 …   (deploy that day: 09:40)
```

Hourly, but never at the top of the hour, and the offset changes every time anything is deployed. That single observation eliminates every scheduled job, every cron, and every checkpoint interval at once — and it took two weeks to notice, because each week's spikes looked internally consistent and nobody compared weeks.

What the database saw, from `pg_stat_statements` sampled across a spike:

```text
query: SELECT product_id, min(price_cents), ... FROM price_rules WHERE ... GROUP BY product_id
  calls           +1,184 in 40s   (baseline: 40/hour)
  mean_exec_time  8 ms → 412 ms
```

One query. Twelve hundred identical executions of it, at once, for a single value.

## Environment

| | |
| --- | --- |
| Product / version | Redis 7 cache, PostgreSQL 15 origin, 40 application pods |
| Deployment | one hot key, `SET price_rules:v3 <json> EX 3600` |
| Scale | ~1.4k req/s reading that key at peak; ~30 requests in flight per pod |
| **Last change** | **the cache TTL was raised from 5 minutes to 1 hour six weeks earlier, to reduce load on the database. Average query volume against it fell by about 90%, exactly as intended.** |

The change did what it was meant to do. It made the miss twelve times rarer and left it exactly as large — and moved the herds far enough apart that nobody associated them with a cache at all.

## Investigation

| Hypothesis | How it was checked | Verdict |
| --- | --- | --- |
| A scheduled job at that time | No cron, no CronJob, no scheduled report matches. And the time **moves**, which nothing scheduled does | rejected — the movement is the fact that mattered |
| PostgreSQL checkpoints or autovacuum | `log_checkpoints` shows no correlation; the spikes continue with autovacuum disabled on that table | rejected |
| A traffic spike from clients | Ingress request rate flat within 2% across every spike window | rejected |
| A slow query regression | The query is 8 ms and correctly indexed. It is only slow **during** the spike, which is a consequence of the concurrency, not a cause | rejected — and the on-call runbook said "look for slow queries", which is why the first three occurrences found nothing |
| One cache key expiring for all pods at once | `TTL price_rules:v3` counted down to zero exactly at the spike, and the spike time equals the last deploy time plus one hour | **accepted** |

The confirmation is one Redis command run a minute before the predicted spike:

```bash
redis-cli TTL price_rules:v3      # 47  → the spike is 47 seconds away
```

Being able to predict the next occurrence to the second is what turned a two-week mystery into a five-minute one — see [[concepts/cache-stampede|cache stampede]].

## Root cause

All 40 pods read one key. They were deployed together, so they populated it within seconds of each other and it carries one TTL, so it expires for all of them at the same instant.

At that instant every request in flight — roughly 30 per pod — misses, and each one independently runs the aggregate query. Twelve hundred concurrent executions of a query that takes 8 ms alone takes 412 ms each under that concurrency, so the window stays open long enough to admit still more callers. The herd enlarges itself until the first result is finally cached.

Nothing was misconfigured and nothing failed. The cache was doing precisely what a cache with a fixed TTL does, forty times over, simultaneously.

## Fix

Jitter, which is one line and shipped within the hour:

```text
SET price_rules:v3 <json> EX (3600 + rand(0..600))
```

Peak concurrent misses fell from ~1,200 to ~40 as the herd spread over ten minutes. Database CPU during the former spike window became indistinguishable from baseline.

Then single-flight, so that only one caller per pod recomputes and the herd is removed rather than flattened:

```text
SET lock:price_rules:v3 1 NX EX 30    → winner queries the origin and caches
                                       losers wait briefly and read the new value
```

Both are now defaults in the shared cache wrapper for any TTL over sixty seconds, rather than something each call site has to remember.

Stale-while-revalidate — serve the expired value while one worker refreshes — would also remove the remaining latency blip and was **not** adopted, because nobody could say how stale pricing data is permitted to be. That is a policy question and it is still open.

## Prevention

- **Detection:** alert on cache **miss rate** spikes rather than on hit ratio. A ratio computed over a minute barely moves during a forty-second stampede, which is why the existing cache dashboard looked healthy throughout. A second useful alert is per-query calls-per-minute from `pg_stat_statements`, which spots a herd against any origin.
- **Prevention:** jitter and single-flight as wrapper defaults, so the decision is opt-out rather than opt-in. And a rule of thumb worth stating plainly: **raising a TTL to reduce load makes stampedes rarer and no smaller.** Any TTL increase on a hot key is a change to the tail, not just to the average.
- **Remaining debt:** the wrapper supports both mitigations and roughly thirty existing call sites have not been migrated. Two of them use 24-hour TTLs, which means bigger herds arriving once a day — currently absorbed, and on exactly the trajectory this key was on six weeks ago.

## Open questions

- Whether the 1-hour TTL is still needed at all. It was introduced to cut database load, the herd was the reason that load looked worth cutting, and nobody has re-measured with the stampede fixed. The TTL may now be solving a problem that no longer exists.
- The traffic level at which the herd became visible is inferred from when spikes appeared in the graph, not measured. Knowing it would tell us how close the two 24-hour keys are to the same cliff, and nothing else will.
- How stale pricing data may be is unanswered, which is what blocks stale-while-revalidate. It is the same gap as the unstated revocation SLA in [[problems/a-revoked-account-kept-working-for-four-hours|the revoked account note]]: a number that a technical decision depends on, that only a non-technical owner can set, and that nobody has been asked for.

## Related

Parent concept: [[concepts/cache-stampede|cache stampede]]. Filed under Capacity in the [[maps/incidents-moc|incident map]].

Sibling: [[problems/a-40-second-blip-became-a-25-minute-outage|the retry amplification]]. Both are synchronised herds and both are fixed in part by the same word — jitter — but they synchronise for different reasons, and the difference decides the rest of the fix. There the trigger is a failure, so the herd forms whenever anything goes wrong and the real control is a budget. Here the trigger is a clock, so the herd forms on schedule and the real control is single-flight. Seeing "add jitter" in both places is correct and is not the whole answer in either.

This also completes a diagnostic ladder with two earlier notes, worth holding together whenever someone says the database is slow. [[problems/the-same-query-is-fast-on-a-fresh-connection-and-slow-after-the-fifth-run|One query slow and the rest fine]] is a planning or indexing problem. [[problems/every-query-got-slower-during-business-hours-and-recovered-overnight|Every query slow by a similar factor]] is a resource ceiling underneath the database. **Every query slow in periodic bursts, with the rest of the time normal**, is a herd arriving from above it. Three shapes, three different places to look, and one glance at `pg_stat_statements` chooses between them.
