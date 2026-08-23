---
title: "Consumer lag grows for hours while every consumer is running and logging normally"
date: 2026-08-22
tags:
  - target/kafka
  - layer/config
  - symptom/outage
  - symptom/data-inconsistency
status: solved
severity: P1
env: "Kafka 3.6 / 12 partitions, 6 consumer pods / Java client defaults: max.poll.records 500, max.poll.interval.ms 300000"
symptom: "consumer poll timeout has expired. This means the time between subsequent calls to poll() was longer than the configured max.poll.interval.ms"
root_cause: "A downstream HTTP dependency slowed from 8ms to ~900ms per call, so a 500-record batch could no longer be processed inside the 300s poll interval. Members were evicted mid-batch, offsets were never committed, and the reassigned partitions replayed the same batch indefinitely."
---

*Example note — the format applied to a well-documented failure mode, not a specific engagement.*

## Symptom

- **Since when:** lag started climbing at 09:40 and was still climbing four hours later. Nothing was deployed to the consumer service that day, that week, or that month.
- **Reproduces when:** per-record processing time exceeds roughly 600ms with the client defaults. Reproducible on demand by adding a sleep to the handler.
- **Does not reproduce when:** the topic is idle enough that batches come back smaller than the configured maximum. The failure needs full batches, so it disappears overnight and returns with morning traffic — which read as "it fixed itself" the first time.

Every pod was up, healthy, logging, and heartbeating. The only line that mattered appeared once per member every few minutes, and it explains the entire incident in its own words:

```text
[Consumer clientId=consumer-orders-1, groupId=orders-consumer] Member
consumer-orders-1-9f3c sending LeaveGroup request to coordinator
broker-2.example.internal:9092 due to consumer poll timeout has expired.
This means the time between subsequent calls to poll() was longer than the
configured max.poll.interval.ms, which typically implies that the poll loop
is spending too much time processing messages. You can address this either by
increasing max.poll.interval.ms or by reducing the maximum size of batches
returned in poll() with max.poll.records.
```

It was in the logs from the first minute. It was not read for three hours, because it is long, it looks like advice rather than an error, and it is logged at INFO.

## Environment

| | |
| --- | --- |
| Product / version | Kafka 3.6, Java client 3.6, 12 partitions on the topic |
| Deployment | 6 consumer pods, one process per pod, at-least-once with auto-commit off |
| Scale | ~4k records/s at peak, each record making one outbound HTTP call |
| **Last change** | **none in this service. The downstream pricing API deployed a change at 09:30 that moved its p99 from 8ms to about 900ms — a different team, a different repository, and an entirely successful deploy by their own metrics.** |

## Investigation

| Hypothesis | How it was checked | Verdict |
| --- | --- | --- |
| Broker-side problem | No under-replicated partitions, no ISR churn, other consumer groups on the same brokers healthy | rejected |
| Consumers are crash-looping | `kubectl get pods` shows zero restarts and hours of uptime. This is the trap — members were leaving the group constantly while no process ever died | rejected |
| Producers flooded the topic | Produce rate flat across the window; the lag is entirely a consumption problem | rejected |
| Not enough consumers | Scaled 6 → 12 pods. Lag got **worse**, because every joining member triggers another rebalance and the group spent more time reassigning than working | rejected, expensively |
| The poll loop exceeds `max.poll.interval.ms` | `kafka-consumer-groups.sh --describe` run twice a minute apart shows `CONSUMER-ID` values changing with no pod restarts | **accepted** |

The discriminator is worth extracting, because lag alone cannot tell slow from stuck:

```bash
kafka-consumer-groups.sh --bootstrap-server broker-1.example.internal:9092 \
  --describe --group orders-consumer
```

Members churning while processes stay up means eviction, not crashing — see [[concepts/consumer-group-rebalancing|consumer group rebalancing]]. Scaling up is the natural reaction and is precisely wrong, which is why that row is kept.

## Root cause

The handler makes one HTTP call per record. At 8ms a 500-record batch takes 4 seconds, comfortably inside the 300-second poll interval. At 900ms the same batch needs 450 seconds, and the coordinator evicts the member at 300.

Because eviction happens mid-batch, nothing is committed. The partitions are reassigned, the new owner reads from the last committed offset, and processes the same records at the same speed, and is evicted at the same point. The group made no forward progress for four hours while every component in it was healthy.

The records processed before each eviction were processed again after every reassignment, so the downstream received the same requests repeatedly for the duration. There was also **no timeout on the HTTP client**, which is why 900ms was able to be the number rather than something worse.

## Fix

Bound the work per poll — the batch size is the lever that is actually under this service's control:

```properties
max.poll.records=50
```

`50 × 900ms = 45s`, comfortably inside the unchanged 300s interval. Lag drained in about twenty minutes.

Give the outbound call a deadline, so per-record time has a ceiling instead of whatever the downstream happens to be doing:

```java
HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(2)).build();
// per-request: .timeout(Duration.ofMillis(1500))
```

Raising `max.poll.interval.ms` was proposed first and rejected. It extends the deadline the loop has to beat, which helps only if processing time is stable — and an unstable processing time is what caused this.

The durable change, done the following week: hand records to a bounded worker pool and keep the poll loop free, using `pause()`/`resume()` for backpressure so poll timing stops depending on downstream latency at all.

## Prevention

- **Detection:** alert on the group's rebalance rate, not only on lag. Lag was alerting correctly and said "slow", which is what everyone acted on for three hours. `kafka_consumer_coordinator_rebalance_total` rising steadily says "stuck", and the two need different responses — one of which is scaling up and the other of which is scaling up making it worse.
- **Prevention:** write the invariant down where the config lives: `max.poll.records × worst-case per-record time < max.poll.interval.ms`. With the defaults, anything slower than 600ms per record breaks it, and no per-record work should be unbounded — every network call in a poll loop needs a timeout.
- **Remaining debt:** four other consumer groups run the same defaults with the same unbounded outbound calls. None were changed, because none are currently near the ceiling — which is the same state this one was in at 09:29.

## Open questions

- How many duplicate downstream requests were made over the four hours, and whether the pricing API is idempotent for all of them. One endpoint was checked and is; the others were assumed to be, and the assumption has not been tested.
- Scaling 6 → 12 made things worse, and the explanation — added members triggering more rebalances — fits the timeline. It was observed once, under pressure, and never reproduced deliberately, so the magnitude of the effect is unknown.
- Why lag alerting existed and rebalance alerting did not is not really a technical question. Lag is the metric on every Kafka dashboard tutorial, and nobody chose to omit the other one.

## Related

Parent concept: [[concepts/consumer-group-rebalancing|consumer group rebalancing]]. Filed under Config in the [[maps/incidents-moc|incident map]] — the defaults were wrong for this workload, and nothing was misconfigured in the sense of a typo.

Sibling: [[problems/connect-fails-with-cannot-assign-requested-address-at-peak|the ephemeral port exhaustion at peak]]. Both are fixed budgets crossed because a *different team's* system changed behaviour — 429s there, latency here — and in both cases the arithmetic that predicts the failure could have been written down in advance and never was. The invariant nobody writes down is the one that gets crossed.

Contrast with [[problems/p99-latency-spikes-while-cpu-utilisation-sits-at-30-percent|the CPU throttling after node consolidation]]: that is also a deadline crossed by an external change, but there the system degraded proportionally — worse tail latency, still serving. Here it stopped completely while looking identical to healthy. A pipeline that makes no progress at all is a rarer and more confusing shape than one that gets slower, and it is worth recognising that "everything is up" and "everything is working" came apart.
