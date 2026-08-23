---
title: "Burst credits"
description: "A burst budget is a loan against a baseline you have never measured. It runs out on the day the workload stops being smaller than you assumed."
tags:
  - meta
---

Several cloud resources are sold as a **baseline rate plus a credit bucket**: you earn credits continuously at the baseline rate, spend them to exceed it, and when the bucket empties you are clamped to the baseline with no warning and no error.

| Resource | Baseline | Burst |
| --- | --- | --- |
| EBS `gp2` | 3 IOPS per GiB, minimum 100 | up to 3,000 IOPS while credits last |
| EC2 `t` instances | a percentage of a vCPU | full vCPU while CPU credits last |
| RDS on `gp2` storage | the same 3 IOPS/GiB | the same cliff, underneath a database |

A 200 GiB `gp2` volume therefore has a **baseline of 600 IOPS** and can serve 3,000 for as long as the bucket holds out. Provisioned by size, it looks like a storage decision; in practice it is a throughput decision made by someone who was thinking about capacity in gigabytes.

## Why it is discovered late and all at once

Credits accumulate whenever demand is below baseline — overnight, at weekends — and drain whenever it is above. A workload that averages below baseline but peaks above it runs indefinitely on a bucket that refills every night, and looks completely healthy while doing so.

Then the average creeps up. The bucket no longer refills fully overnight, the balance trends down over days or weeks, and nothing changes about the daily experience until the day it reaches zero. At that point performance collapses to baseline during every busy period — and partially recovers each night, so the symptom **appears daily and appears to fix itself daily**, which reads as a load problem rather than a depletion.

The graph that explains it is not the latency graph. It is a slow, monotonic decline over weeks in a metric nobody has ever opened:

```text
BurstBalance (%)   100 … 92 … 78 … 61 … 40 … 18 … 0
```

## The signature

A clamped resource is **pinned at a round number**. Not fluctuating near a ceiling, not noisy — flat, at exactly the documented baseline, for the entire busy period:

```text
VolumeReadOps + VolumeWriteOps  ≈ 600/s   flat, for six hours
VolumeQueueLength               rising     — the queue is where the excess goes
```

The other tell is **uniform degradation**: every query, every request type, everything slows by a similar factor. A regression in one query path slows one path. A resource ceiling underneath the database slows all of them equally, which is the fastest way to know the problem is below the layer you are looking at — the shape behind [[problems/every-query-got-slower-during-business-hours-and-recovered-overnight|a database that got slower every morning and better every night]].

## Bounding it

- **Alert on the balance trend, not the floor.** At 5% you have hours. At 60% and falling for a week you have a capacity conversation.
- **Prefer a fixed baseline over a burst budget.** `gp3` provides 3,000 IOPS independent of size; `t` instances can be set to unlimited mode, which converts the cliff into a bill.
- **Size for the steady state, and treat burst as headroom you have already spent.** Anything that only works because credits exist is already over capacity; it has simply not been told yet.
