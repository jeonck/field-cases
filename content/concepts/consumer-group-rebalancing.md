---
title: "Consumer group rebalancing"
description: "Kafka has two independent liveness deadlines, and the one everybody tunes is not the one that fires."
tags:
  - meta
---

A Kafka consumer group assigns each partition to exactly one member. Whenever membership changes, the group **rebalances**: assignments are revoked, recomputed, and handed back out, and no member processes anything while that happens.

Membership is judged by two separate deadlines, which is the part that causes trouble:

| Setting | Default | Measured by | Answers |
| --- | --- | --- | --- |
| `session.timeout.ms` | 45s | a **background heartbeat thread** | is the process alive? |
| `max.poll.interval.ms` | 300s | time between `poll()` calls | is the process making progress? |

Since the heartbeat moved to its own thread, a consumer stuck in processing keeps heartbeating perfectly. It looks alive because it *is* alive. Only `max.poll.interval.ms` notices that it has not asked for more work, and when it fires the coordinator removes the member and rebalances.

## The loop

That removal is where a slow consumer becomes a stopped pipeline:

1. A batch takes longer than `max.poll.interval.ms` to process.
2. The member is evicted; its offsets were never committed.
3. Partitions are reassigned; the new owner starts from the last committed offset — **the same batch**.
4. It takes just as long. Go to 2.

Nothing crashes, nothing errors except the coordinator's own log line, and lag grows in a straight line. Every record in flight is also processed repeatedly, so a non-idempotent downstream accumulates duplicates for as long as the loop runs.

Adding consumers makes it worse rather than better: each joining member triggers another rebalance, and the group spends more of its time reassigning than working.

## The invariant

The condition is arithmetic, and it can be checked before it is discovered:

```text
max.poll.records × worst-case per-record processing time  <  max.poll.interval.ms
```

With the defaults that is `500 × t < 300s`, so anything above 600ms per record breaks it. Per-record time is usually dominated by a network call to something owned by another team, which means **the left-hand side is not under your control and the right-hand side is** — see [[problems/consumer-lag-grows-for-hours-while-the-consumers-look-healthy|lag growing while consumers look healthy]].

Two levers, and they are not equivalent. `max.poll.records` bounds the work per poll and is the honest fix. Raising `max.poll.interval.ms` extends the deadline the loop must beat, which helps only if processing time is stable — and if it were stable, the deadline would not have been crossed.

```bash
kafka-consumer-groups.sh --bootstrap-server broker-1.example.internal:9092 \
  --describe --group orders-consumer
```

Run it twice a minute apart. Lag rising is ambiguous — it means slow *or* stuck. **`CONSUMER-ID` values changing while no process has restarted** is not ambiguous at all.
