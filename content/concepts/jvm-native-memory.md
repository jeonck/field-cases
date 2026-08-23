---
title: "A container limit is not a heap limit"
description: "The kernel counts every byte the process holds. The JVM only reports the ones it knows about."
tags:
  - meta
---

A container's memory limit applies to the **resident set of the whole process**. A JVM's `-Xmx` applies to the Java heap. The gap between those two is where containers get killed.

Everything below the line is real memory the process holds and `-Xmx` says nothing about:

| Region | Sized by | Typical |
| --- | --- | --- |
| Java heap | `-Xmx` | the number everyone quotes |
| Metaspace | class count | 100–300 MB |
| Code cache (JIT) | code volume | 50–250 MB |
| Thread stacks | threads × `-Xss` | 1 MB per thread, default |
| Direct / mapped buffers | NIO, netty, compression | unbounded unless capped |
| GC structures | heap size and collector | 5–10% of heap |
| **glibc malloc arenas** | **CPU count and allocation pattern** | **hundreds of MB, invisible to the JVM** |

Setting `-Xmx` close to the container limit leaves nothing for the rest of that table, and the process is killed while the heap graph looks calm.

## Native Memory Tracking has a blind spot

`-XX:NativeMemoryTracking=summary` plus `jcmd <pid> VM.native_memory summary` accounts for everything the JVM allocated. It does **not** account for memory that the C library allocated on the JVM's behalf and never returned to the kernel, so a healthy audit can still be a gigabyte short of RSS:

```bash
jcmd 1 VM.native_memory summary | head -20      # what the JVM thinks it holds
grep VmRSS /proc/1/status                       # what the kernel knows it holds
```

**A gap between those two numbers is the allocator, not a leak.** glibc creates up to `8 × CPU count` malloc arenas, each able to retain memory it is not using — so a thread-heavy service on a 48-core node can hold hundreds of megabytes it will never hand back. Moving to larger nodes silently raises the arena count with no change to the application.

```bash
MALLOC_ARENA_MAX=2      # the standard mitigation, set as an environment variable
```

It trades allocator concurrency for footprint, which is usually the right trade in a container and is a real trade, not a free win.

## Why the kill leaves nothing behind

The cgroup OOM killer sends `SIGKILL`. There is no `OutOfMemoryError`, no heap dump — `-XX:+HeapDumpOnOutOfMemoryError` never fires, because the JVM never learns anything is wrong — no shutdown hook, and no final log line. The only evidence is outside the process:

```text
Last State:  Terminated
  Reason:    OOMKilled
  Exit Code: 137
```

An application that dies without explanation, in a container, with a healthy heap, is [[problems/pods-restart-every-few-hours-with-nothing-in-the-application-log|this failure until proven otherwise]]. Budget the whole process, not the heap: `-XX:MaxRAMPercentage` sizes the heap as a fraction of the container limit, which is the only sizing that stays correct when the limit changes.
