---
title: "p99 latency spikes to 400ms while CPU utilisation sits at 30%"
date: 2026-08-22
tags:
  - target/kubernetes
  - target/golang
  - layer/compute
  - symptom/latency
status: solved
severity: P2
env: "Kubernetes 1.28 / cgroup v2 / Go 1.22 service, limits.cpu 1 / node pool moved 4-core → 24-core"
symptom: "nr_periods 184320 / nr_throttled 57118 / throttled_usec 1149203847"
root_cause: "GOMAXPROCS follows the node's core count, not the cgroup quota. On 24-core nodes the service ran 24 threads against a 1-CPU quota, exhausted its 100ms budget in a few milliseconds, and was frozen for the rest of every period it burst in."
---

*Example note — the format applied to a well-documented failure mode, not a specific engagement.*

## Symptom

- **Since when:** two weeks after the node pool was consolidated onto larger instances. p50 improved slightly, p99 roughly tripled, and nobody connected the two because the change was a capacity improvement.
- **Reproduces when:** any burst of concurrent requests. Deterministic under a load generator at 40 concurrent connections; the spikes appear in multiples of roughly 100ms.
- **Does not reproduce when:** requests arrive serially, or when the pod is scheduled onto one of the few remaining 4-core nodes. Same image, same config, same traffic — the node's size is the variable.

No errors, no restarts, no OOM kills. The only artefact is the cgroup's own accounting, which nothing was looking at:

```text
$ cat /sys/fs/cgroup/cpu.stat
usage_usec 42188410293
user_usec 31002884120
system_usec 11185526173
nr_periods 184320
nr_throttled 57118
throttled_usec 1149203847
```

31% of periods throttled, and 19 minutes of wall time spent stopped. Meanwhile the dashboard everyone was looking at:

```text
container_cpu_usage_seconds_total rate over 1m ≈ 0.31 cores  (limit: 1)
```

Both numbers are correct. The container used a third of its CPU allowance and spent a third of its life forbidden from using any, and those are the same fact seen from two sides.

## Environment

| | |
| --- | --- |
| Product / version | Go 1.22 service, Kubernetes 1.28, cgroup v2, kernel 5.15 |
| Deployment | `requests.cpu: 200m`, `limits.cpu: 1`, 18 replicas |
| Scale | ~3k req/s across the deployment, bursty — traffic arrives in bunches |
| **Last change** | **node pool consolidated from 4-core to 24-core instances two weeks earlier, to improve bin-packing. Pod requests, limits, image and configuration all unchanged.** |

This is the row that mattered and the one hardest to take seriously. Nothing about the workload changed; the machine underneath it got six times wider, and that alone multiplied the service's thread count by six against a quota that stayed at one CPU.

## Investigation

| Hypothesis | How it was checked | Verdict |
| --- | --- | --- |
| Go GC pauses | `GODEBUG=gctrace=1` — STW phases under 0.5ms, and the p99 spikes do not line up with GC cycles | rejected |
| A downstream dependency is slow | Downstream p99 flat throughout. Spikes occur on endpoints that make no outbound calls at all | rejected |
| Noisy neighbours or CPU steal on the new instance type | `%steal` at 0.0 across the pool. The spikes follow the pod when it is rescheduled, rather than staying with a node | rejected |
| The CFS slack-expiry kernel bug (over-throttling below quota) | Kernel 5.15 carries the 5.4 fix. Also inapplicable — `cpu.stat` shows the quota being genuinely consumed, not phantom throttling | rejected, and worth recording: it is the first thing every search result proposes |
| CFS quota throttling, with thread count mismatched to the quota | `nr_throttled/nr_periods` at 0.31. Setting `GOMAXPROCS=1` on one pod dropped its p99 from 412ms to 34ms within a minute, with no other change | **accepted** |

The one-pod experiment is the whole confirmation and takes about thirty seconds. It is worth reaching for before any of the above, because a positive result costs one pod and a negative result eliminates the entire class:

```bash
kubectl set env deployment/orders-api GOMAXPROCS=1 --containers=app   # canary one replica first
```

## Root cause

`limits.cpu: 1` is not a speed limit. It is 100ms of CPU time per 100ms period, and it is consumed by every thread in parallel — see [[concepts/cfs-quota-throttling|CFS quota throttling]].

Go sets `GOMAXPROCS` from the number of CPUs the **machine** reports, not from the cgroup quota. On the old 4-core nodes the service ran 4 worker threads and could overrun its budget only modestly. On 24-core nodes it runs 24, so a burst consumes the full 100ms budget in about 4ms of wall time and every thread is then stopped for the remaining ~96ms. A request unlucky enough to arrive at that moment waits out the period, and a request that needs CPU across several periods waits out several.

The tripled p99 and the improved p50 are the same mechanism: when not throttled, the service had more parallelism than before and was faster.

## Fix

The runtime should size itself from the quota, not the node:

```go
import _ "go.uber.org/automaxprocs"   // reads the cgroup quota at startup
```

For services that cannot take the dependency immediately, the downward API does the same job without a code change:

```yaml
env:
  - name: GOMAXPROCS
    valueFrom:
      resourceFieldRef:
        resource: limits.cpu     # rounds up; 500m becomes 1
```

Then confirm it actually took effect, because both mechanisms are silent when they fail:

```bash
kubectl exec deploy/orders-api -- sh -c 'cat /sys/fs/cgroup/cpu.stat | grep nr_throttled'
```

Throttled periods went from 31% to 0.2% and p99 settled at 38ms. The limit itself was also raised to 2 on this service, which is discussed below and muddies the result.

Removing CPU limits entirely — keeping requests, dropping limits — is the other common recommendation, and it is a real trade rather than a strictly better option: it eliminates throttling and hands the risk to whatever else shares the node. It was not adopted here, and it was also not seriously evaluated.

## Prevention

- **Detection:** alert on `rate(container_cpu_cfs_throttled_periods_total) / rate(container_cpu_cfs_periods_total) > 0.05`. This existed as a Grafana panel nobody had opened and as no alert at all. CPU utilisation is not a proxy for it and never was — the whole failure is that both numbers were healthy-looking simultaneously.
- **Prevention:** treat an instance-type change as a change to every container's effective parallelism, and put it in the same change log as a deploy. A node pool migration is reviewed by the platform team against node-level metrics, all of which improved.
- **Remaining debt:** `automaxprocs` is not in the shared service module, so it is opt-in and roughly forty Go services have not opted in. Only the one that paged was fixed. The same class of mismatch exists for Node.js services through the libuv threadpool, which nobody has looked at, and would present identically.

## Open questions

- p99 improved after two changes shipped in the same rollout on this service — `GOMAXPROCS` and raising `limits.cpu` to 2. The single-pod canary attributes the effect to `GOMAXPROCS` and the throttle ratio supports it, but the final numbers cannot cleanly separate the two, and the limit bump was never reverted to check.
- Why 31% throttled corresponds to 30% utilisation is treated as coincidence here. Whether there is a real relationship, or how scrape interval hides throttling generally, was never worked out — and it matters for setting the alert threshold on services with different burst shapes.
- Nothing establishes what the right `limits.cpu` for this service actually is. It was 1 because it has been 1 since the deployment was written, and raising it to 2 was a reaction, not a measurement.

## Related

Parent concept: [[concepts/cfs-quota-throttling|CFS quota throttling]]. Filed under Compute in the [[maps/incidents-moc|incident map]].

Sibling: [[problems/dns-lookups-intermittently-take-exactly-5-seconds|the 5-second DNS stalls inside pods]]. Both are Kubernetes tail-latency problems with no errors anywhere, both were invisible to every application-level metric, and both have a characteristic duration that identifies them on sight — 5.0s there is a resolver's own timeout, ~100ms here is one scheduler period. When a latency histogram has a spike at a suspiciously round number, the number is usually a timeout or a period somewhere, and naming it is most of the diagnosis.

Contrast with [[problems/the-same-query-is-fast-on-a-fresh-connection-and-slow-after-the-fifth-run|the query that goes slow after five executions]]: both are latency regressions where the responsible dashboard looked fine, but there the metric was simply not being read, while here the metric was read and was genuinely healthy. A correct number can still be the wrong question.
