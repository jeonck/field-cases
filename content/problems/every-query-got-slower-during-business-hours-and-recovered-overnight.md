---
title: "Every query got slower during business hours and recovered overnight, for three weeks"
date: 2026-08-22
tags:
  - target/aws-ebs
  - target/postgres
  - layer/storage
  - symptom/latency
status: solved
severity: P2
env: "PostgreSQL 15 on EC2 / 200 GiB gp2 volume, baseline 600 IOPS / write rate up ~20% after a feature launch"
symptom: "BurstBalance 0%, VolumeReadOps + VolumeWriteOps flat at 600/s for six hours"
root_cause: "A feature raised the write rate enough that the volume's I/O credits stopped fully replenishing overnight. Once the balance reached zero the volume was clamped to its 600 IOPS baseline during every busy period."
---

*Example note — the format applied to a well-documented failure mode, not a specific engagement.*

## Symptom

- **Since when:** query latency began degrading at around 09:15 each weekday and recovered by early evening. It got worse each week for three weeks before anyone escalated it, because each individual day looked survivable.
- **Reproduces when:** sustained load above roughly 600 IOPS. On a bad day that is any weekday morning.
- **Does not reproduce when:** overnight, at weekends, or immediately after a maintenance window. Recovering on its own every night is what kept this classified as "the database is busy in the mornings" for three weeks.

Everything slowed down together. This is the fact that mattered and it took two weeks to be treated as information rather than as vagueness:

```text
pg_stat_statements, mean_exec_time, week over week
  SELECT orders by id        1.2 ms  →  11.8 ms
  INSERT order_items         2.1 ms  →  19.4 ms
  SELECT customer summary   14.0 ms  → 131.0 ms
  UPDATE inventory           0.9 ms  →   9.1 ms
```

Roughly ten times slower, across every statement, regardless of what it touched or how it was planned. And underneath, in a console nobody had opened:

```text
BurstBalance                     0 %
VolumeReadOps + VolumeWriteOps   ≈ 600/s, flat for six hours
VolumeQueueLength                18.4 average
```

## Environment

| | |
| --- | --- |
| Product / version | PostgreSQL 15 on EC2, single instance, 200 GiB `gp2` root data volume |
| Deployment | provisioned three years ago by size; baseline 600 IOPS, burst 3,000 |
| Scale | ~1.5k queries/s peak, ~40 GB working set |
| **Last change** | **a feature launched five weeks earlier raised the write rate by roughly 20%. It shipped cleanly, its own metrics were good, and it was not suspected because the symptoms began two weeks after it went out.** |

The lag between the change and the symptom is the interesting part. Nothing broke when the write rate rose; the volume simply began spending slightly more credits per day than it earned, and it took two weeks of that to empty a bucket that had been full for three years.

## Investigation

| Hypothesis | How it was checked | Verdict |
| --- | --- | --- |
| A query regression or a plan flip | `pg_stat_statements` shows **every** statement degraded by a similar factor, including trivial primary-key lookups. A plan problem does not do that — it makes one thing slow | rejected, and this row is the whole investigation in hindsight |
| Autovacuum or a bloat problem | `n_dead_tup` normal, autovacuum keeping up, no long-running vacuums during the slow window | rejected |
| Connection or lock contention | No lock waits, connection count well under the limit, and the slowness is present at low concurrency too | rejected |
| Noisy neighbours on shared storage | Plausible for a shared tenancy, but the throughput is not noisy — it is **flat at exactly 600/s**, which is a limit rather than contention | rejected |
| The volume is clamped to its baseline after exhausting burst credits | `BurstBalance` at 0%, and a fifteen-month history showing a monotonic decline beginning two weeks after the feature launch | **accepted** |

Uniform degradation is the shortcut. When one query is slow, look at the query; when everything is slow by the same factor, the problem is underneath the database and no amount of `EXPLAIN` will find it — see [[concepts/burst-credits|burst credits]].

The second shortcut is the shape of the throughput graph. A saturated resource fluctuates; a **clamped** one is flat at a round number, and 600 is exactly 3 IOPS per GiB × 200 GiB.

## Root cause

A `gp2` volume earns I/O credits at its baseline rate — 3 IOPS per GiB, so 600/s here — and spends them to serve up to 3,000. For three years the workload peaked above baseline during the day and sat well below it overnight, so the bucket refilled every night and the volume behaved as a 3,000 IOPS device.

The feature raised the daily average enough that overnight replenishment no longer covered daytime spend. The balance declined a few percent a day for two weeks, reached zero, and from then on every busy period ran at 600 IOPS with the excess sitting in the queue. Recovery each night is not the system healing; it is the bucket partially refilling and being emptied again by lunchtime.

Nothing was misconfigured. A 200 GiB volume was sized for 200 GiB of data, which it still holds comfortably. The throughput consequence of that size was decided three years ago by someone thinking about gigabytes.

## Fix

Immediate, and available without downtime — move to `gp3`, whose baseline is independent of size:

```bash
aws ec2 modify-volume --volume-id vol-0a1b2c3d --volume-type gp3 --iops 6000 --throughput 250
```

`gp3` includes 3,000 IOPS at any size and can be provisioned higher. Latency returned to its previous baseline within the modification window; there is no burst concept to run out of.

The equivalent `gp2` fix is to grow the volume, because baseline scales with size — 1,000 GiB would give 3,000 IOPS — and it means paying for five times the storage to buy throughput. It was the fallback and was not needed.

Separately, the write amplification the feature introduced was reduced by batching per-request rows into one insert per request rather than one per line item, which cut write IOPS by about 35%. That is the change that makes the new ceiling last.

## Prevention

- **Detection:** alert on `BurstBalance` **trend**, not on a floor — below 60% and declining over a week is a capacity conversation; below 5% is a few hours' notice and no time to act. A second alert on volume IOPS sitting flat at the documented baseline catches the clamp directly, and would have fired on day one of three weeks.
- **Prevention:** `gp3` for anything with a database on it. More generally, treat burst as headroom already spent — a workload that only fits because credits exist is over capacity and has not been informed. Any change that moves write rate is a storage capacity change.
- **Remaining debt:** six other `gp2` volumes remain in the account. Two are below 50% balance and falling, and were found while writing this up. Nobody has decided whether to migrate them all now or wait for each to become an incident, and the second option is currently winning by default.

## Open questions

- The write increase is attributed to one feature, but three shipped that month and the write-rate metric is not broken down by source. The attribution rests on timing and on reading the code, not on measurement, and if it is wrong the batching fix is aimed at the wrong place.
- How long the nightly partial recovery had been masking a decline is unknown. The metric retains fifteen months and nobody looked further back than the incident, so it is not established whether this began five weeks ago or has been narrowing for a year.
- Whether `gp3` at 6,000 IOPS is genuine headroom or a larger cliff at a later date. Nobody modelled growth; the number was chosen as "about ten times the baseline we had" and that is not a model.

## Related

Parent concept: [[concepts/burst-credits|burst credits]]. Filed under Storage in the [[maps/incidents-moc|incident map]].

Sibling: [[problems/p99-latency-spikes-while-cpu-utilisation-sits-at-30-percent|the CPU throttling behind healthy utilisation]]. Both are rate limits enforced by a budget that does not appear on any utilisation graph — a CFS quota per 100ms period there, an I/O credit bucket here — and in both the ordinary monitoring was correct and useless. The difference is the timescale: a throttled container is stopped and restarted a hundred times a second, so the failure is instant and stationary, while a credit bucket empties over weeks and the failure arrives on a day when nothing happened.

Contrast with [[problems/the-same-query-is-fast-on-a-fresh-connection-and-slow-after-the-fifth-run|the query plan that flipped after five executions]]. Both present as "the database got slow", and telling them apart takes one glance at `pg_stat_statements`: a plan problem slows **one** statement dramatically, a resource ceiling slows **all** of them by a similar factor. That single distinction would have saved two weeks here, and it costs nothing to check first.
