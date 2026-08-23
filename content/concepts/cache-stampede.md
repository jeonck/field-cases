---
title: "Cache stampede"
description: "When one popular key expires, every caller misses at the same instant. Raising the TTL makes it rarer and worse."
tags:
  - meta
---

A cache entry does not expire for one caller. It expires for **all of them, simultaneously**, and every request in flight at that moment goes to the origin at once.

For a key served 1,000 times a second by 40 processes, the arithmetic at expiry is not "one extra query". It is however many requests are in flight during the time the origin takes to answer — hundreds or thousands of identical queries, arriving together, for a value that a single query could have produced.

The origin then slows down under that concurrency, which extends the window, which admits more callers into the same miss. A stampede is self-enlarging while it lasts.

## Why entries synchronise

They rarely start out synchronised; they get that way:

- **Fixed TTLs from a common start.** Processes deployed together warm their caches together, so every entry shares an expiry. The spike then recurs at exactly the TTL interval, at a **phase set by the deploy time** — so it moves whenever you deploy, which is a strong tell and rules out anything scheduled.
- **Precomputation jobs** that write many keys at once with one TTL.
- **A flush or restart**, after which everything is repopulated within seconds of everything else.

## Raising the TTL makes it worse

This is the counterintuitive part, and it is why the problem often appears right after a performance improvement. Doubling a TTL halves how often the origin is hit in steady state — and halves how often the herd occurs while leaving each herd exactly as large. Fewer, rarer, equally violent, and now spaced far enough apart that nobody connects them to the cache at all.

A cache tuned purely on hit ratio is being tuned on its average behaviour, and the stampede lives entirely in the tail.

## Three mitigations, in increasing order of what they fix

```text
1. Jitter      SET key val EX (3600 + rand(0..600))
   Spreads the herd over the jitter window. One line, no coordination,
   reduces the peak by roughly the ratio of window to origin latency.
   It does not remove the herd; it flattens it.

2. Single-flight  SET lock:key 1 NX EX 30   → winner recomputes, losers wait
   Exactly one caller goes to the origin per key per miss. Removes the
   herd entirely. Losers still wait, so the latency spike remains.

3. Stale-while-revalidate  serve the expired value; one worker refreshes
   No caller ever waits for a recompute. Requires that serving a slightly
   stale value is acceptable — which is a policy question, not a technical one.
```

Most systems need the first two. The third needs somebody to state how stale a value is allowed to be, and that number is usually as unwritten as everything else in this category — the shape behind [[problems/database-cpu-hits-100-percent-for-forty-seconds-at-a-time-that-moves-with-each-deploy|a database that spikes on a schedule nobody set]].
