---
title: "Pods restart every few hours with nothing in the application log and the heap at 40%"
date: 2026-08-22
tags:
  - target/jvm
  - target/kubernetes
  - layer/compute
  - symptom/silent-failure
  - symptom/intermittent-failure
status: solved
severity: P2
env: "JDK 17 / -Xmx3g against limits.memory 3.5Gi / 48-core nodes / HTTP client pool raised 50 → 400"
symptom: "Reason: OOMKilled / Exit Code: 137"
root_cause: "RSS exceeded the container limit while the heap was largely empty. Roughly a gigabyte was held by glibc malloc arenas — 8 per CPU on a 48-core node — inflated by a thread pool that had been raised from 50 to 400 three weeks earlier."
---

*Example note — the format applied to a well-documented failure mode, not a specific engagement.*

## Symptom

- **Since when:** restarts began three weeks after the HTTP client pool was raised from 50 to 400 threads. Roughly every three to five hours per pod, unsynchronised, so at any moment most of the fleet was fine.
- **Reproduces when:** the pod has been up long enough. It is not load-triggered in any sharp way — RSS climbs slowly and crosses the limit eventually, so a busy pod simply gets there sooner.
- **Does not reproduce when:** running the same image locally with no memory limit. It grows just as much and nothing kills it, which is why three weeks of local testing found nothing.

The application log ends mid-sentence. No exception, no `OutOfMemoryError`, no shutdown message, no heap dump despite `-XX:+HeapDumpOnOutOfMemoryError` being set. The only record of the death is outside the process:

```text
    Last State:     Terminated
      Reason:       OOMKilled
      Exit Code:    137
      Started:      Sat, 22 Aug 2026 04:12:09 +0000
      Finished:     Sat, 22 Aug 2026 07:48:31 +0000
```

Meanwhile the JVM's own dashboard, over the hour before each kill:

```text
heap used     1.19 GB / 3.00 GB      (40%)
GC pause p99  11 ms
old gen after full GC   0.82 GB, flat for hours
```

A memory problem where the memory graph is flat is not a memory problem anyone recognises, and that is why it went three weeks.

## Environment

| | |
| --- | --- |
| Product / version | JDK 17, Spring Boot, glibc (Debian base image) |
| Deployment | `-Xmx3g`, `limits.memory: 3.5Gi`, 24 pods on 48-core nodes |
| Scale | ~600 req/s, each request making outbound HTTP calls |
| **Last change** | **the HTTP client's connection and worker pool was raised from 50 to 400 threads three weeks earlier, to clear a queueing problem. Reviewed, measured against latency, shipped, and it worked.** |

The thread pool change did exactly what it was meant to do. Nobody costed it in memory, because thread count is not something anyone thinks of as a memory setting.

## Investigation

| Hypothesis | How it was checked | Verdict |
| --- | --- | --- |
| A Java heap leak | Old gen flat at 0.82 GB after every full GC for hours before a kill. A leak does not hold still | rejected |
| The liveness probe is killing the pod | A liveness kill is also `SIGKILL` and also exit 137 — this is the genuine ambiguity. Distinguished by events: `Reason: OOMKilled` rather than `Error`, and no `Liveness probe failed` events at all | rejected |
| Node memory pressure evicting pods | An eviction shows as `Evicted` with a node condition, not `OOMKilled`. The nodes had 30% free throughout | rejected |
| Metaspace or code cache growth | NMT shows metaspace 190 MB, code cache 140 MB, both flat | rejected |
| Native memory outside the JVM's accounting | NMT total committed 2.4 GB against an RSS of 3.45 GB — **a gigabyte the JVM does not know about** | **accepted** |

That last row is the whole thing, and it is two commands:

```bash
jcmd 1 VM.native_memory summary | head -20
grep VmRSS /proc/1/status
```

The JVM's accounting is complete for everything the JVM allocated. The gap belongs to the C library — see [[concepts/jvm-native-memory|a container limit is not a heap limit]].

## Root cause

glibc creates up to `8 × CPU count` malloc arenas — 384 on a 48-core node — and each retains memory it is not currently using. A workload with 400 threads allocating and freeing constantly spreads across many arenas and holds a large amount of memory that is free from the allocator's point of view and resident from the kernel's.

Heap 1.2 GB in use, everything else in the JVM's own accounting about 500 MB, roughly a gigabyte in arenas, and a 3.5 GiB limit. RSS crosses it after a few hours, the cgroup OOM killer sends `SIGKILL`, and the JVM never learns anything happened — hence no exception, no dump, no final log line.

The thread pool increase supplied the threads. The 48-core nodes supplied the arena count. `-Xmx3g` against a 3.5 GiB limit left 500 MB for everything that is not heap, which was never enough and had been fine only because the old configuration used less of it.

## Fix

Cap the arenas, which is one environment variable and needs no code change:

```yaml
env:
  - name: MALLOC_ARENA_MAX
    value: "2"
```

RSS fell from 3.45 GB to 2.61 GB on the first restarted pod and stopped climbing. It trades allocator concurrency for footprint; p99 moved by under a millisecond here, which will not be true of every workload.

Size the heap from the limit rather than independently, so it stays correct the next time the limit changes:

```yaml
env:
  - name: JAVA_TOOL_OPTIONS
    value: "-XX:MaxRAMPercentage=60 -XX:NativeMemoryTracking=summary"
```

`60%` of 3.5 GiB is about 2.1 GB of heap, well above the 1.2 GB actually in use, and leaves room for the rest of the process. Native Memory Tracking stays on permanently — it costs a few percent and it is the difference between this investigation taking three weeks and taking ten minutes.

Replacing glibc's allocator with jemalloc was raised and not done. It is the more thorough answer and it changes the runtime for every service on the base image, which is not a change to make during an incident.

## Prevention

- **Detection:** alert on `container_memory_working_set_bytes / limit > 0.85`, and separately on `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}`. The JVM heap dashboard cannot see this and adding panels to it would not have helped — the number that mattered was never a JVM number.
- **Prevention:** heap is a fraction of the container limit, never an independent constant. The budget worth writing next to the manifest: heap + metaspace + code cache + (threads × stack) + allocator overhead **< limit**, with the last term real and hard to predict. Any change to thread count is a change to that budget.
- **Remaining debt:** `MALLOC_ARENA_MAX` was set on this deployment only. It is not in the shared base image, and eleven other JVM services set `-Xmx` as a constant with no relationship to their limits. None are currently restarting, which is the same thing this service could have said a month ago.

## Open questions

- The ~1 GB is attributed to malloc arenas by elimination and by the fact that capping them recovered 840 MB. Nothing measured arena residency directly, so the attribution is strong evidence rather than a measurement.
- Is the 400-thread pool still needed? It was introduced to clear a queueing problem that was itself later addressed elsewhere, and nobody has retested with a smaller pool. The memory fix removed the pressure to ask.
- Whether jemalloc would be better than `MALLOC_ARENA_MAX=2` for this workload is untested. Arena limiting can cost throughput under allocation contention, and "p99 did not move" was measured over one afternoon at ordinary traffic.

## Related

Parent concept: [[concepts/jvm-native-memory|a container limit is not a heap limit]]. Filed under Compute in the [[maps/incidents-moc|incident map]].

Sibling: [[problems/p99-latency-spikes-while-cpu-utilisation-sits-at-30-percent|the CPU throttling that hid behind healthy utilisation]]. Both are container limits enforced by the kernel against a runtime that sized itself by asking the machine instead of the cgroup, and both got worse on larger nodes — there `GOMAXPROCS` followed the core count, here the malloc arena count did. The difference is what the kernel does when you cross the line: CPU throttling **stops** the process and leaves a counter to find, memory **kills** it and leaves nothing. The second is far more confusing for exactly that reason, and the failure that leaves evidence is the lucky one.

Contrast with [[problems/df-says-the-disk-is-full-du-says-it-is-half-empty|the disk that filled with nothing on it]]: both are "the tool everyone trusts reports a healthy number while the real one is elsewhere" — `du` against `df`, heap against RSS. In both cases the two numbers are individually correct and the gap between them is the entire diagnosis, and in both cases nobody thinks to subtract until someone suggests it.
