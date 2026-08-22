---
title: "CFS quota throttling"
description: "A CPU limit is not a speed limit. It is a budget per 100ms window, and running out of it stops the process dead until the window rolls over."
tags:
  - meta
---

A Kubernetes `limits.cpu` is enforced by the Linux scheduler's **bandwidth control**, not by slowing anything down. The cgroup gets a quota of CPU-time per fixed period:

- **period** — 100ms by default (`cpu.cfs_period_us`)
- **quota** — `limits.cpu × period`. `limits.cpu: 1` means 100ms of CPU time per 100ms window; `limits.cpu: 500m` means 50ms.

When the quota is consumed, every thread in the cgroup is **stopped** until the next period begins. Not descheduled in favour of something else — stopped, whether or not the machine is idle.

## Why "CPU usage is low" is not an answer

Quota is consumed by all threads **in parallel**. A process with 24 runnable threads and a 1-CPU quota burns its entire 100ms budget in roughly 4ms of wall time, then sits frozen for the remaining ~96ms. Averaged over a 60-second scrape, that container reports modest CPU utilisation — it genuinely used little CPU, because it spent most of its life forbidden from using any.

Utilisation and throttling are different questions, and only one of them is on the usual dashboard:

```bash
cat /sys/fs/cgroup/cpu.stat        # cgroup v2
```
```text
nr_periods 184320
nr_throttled 57118
throttled_usec 1149203847
```

`nr_throttled / nr_periods` is the number that matters. Anything above a few percent means the container is being stopped regularly, at up to a full period per occurrence — which lands directly in tail latency and nowhere else.

## Thread count is the multiplier

The runtime decides how many threads compete for that budget, and it usually decides by asking the **machine**, not the cgroup:

- **Go** — `GOMAXPROCS` defaults to the host's core count. `go.uber.org/automaxprocs` reads the quota instead.
- **JVM** — container-aware since JDK 10; `-XX:ActiveProcessorCount` overrides when it guesses wrong.
- **Node.js** — libuv's threadpool is 4 by default regardless of anything.

So moving to bigger nodes multiplies parallelism while the quota stays fixed, and a container that was fine on a 4-core node is throttled on a 24-core one with no change to its own configuration — the mechanism behind [[problems/p99-latency-spikes-while-cpu-utilisation-sits-at-30-percent|tail latency spiking while CPU utilisation looks healthy]].

The kernel's old slack-expiry bug — over-throttling well below the quota — was fixed in Linux 5.4 and backported widely. On a modern kernel, throttling means the quota was genuinely consumed, and search results suggesting otherwise are describing a machine you are probably not running.
