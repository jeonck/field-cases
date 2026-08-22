---
title: "Incident map"
description: "Every problem note, grouped by where the root cause turned out to be."
tags:
  - meta
---

Grouped by `layer/` — by where the cause **was**, not by what it looked like. A note that looked like a network problem and turned out to be a connection-pool limit belongs under Database.

Entries are added by hand when a note is published; see [[meta/note-linking-rules|linking rules]]. Nothing populates this automatically, which is the point — if adding the line feels like a chore, the note probably was not worth promoting.

## Network

- [[problems/dns-lookups-intermittently-take-exactly-5-seconds|DNS lookups inside pods intermittently take exactly 5 seconds]] — a conntrack insert race silently drops one of a parallel A/AAAA pair; the 5s is the resolver's own timeout, not the network's.

## Storage

- [[problems/df-says-the-disk-is-full-du-says-it-is-half-empty|df says the disk is full, du says it is half empty]] — logrotate unlinked the logs, nginx kept the descriptors, and the blocks were never freed.

## Database

- [[problems/the-same-query-is-fast-on-a-fresh-connection-and-slow-after-the-fifth-run|The same query is fast on a fresh connection and slow forever after the fifth run]] — a backfill skewed the data, and the planner's generic plan was built for a value the application never asks for.

## Compute

- [[problems/p99-latency-spikes-while-cpu-utilisation-sits-at-30-percent|p99 latency spikes to 400ms while CPU utilisation sits at 30%]] — bigger nodes multiplied thread count against an unchanged CPU quota; both numbers on the dashboard were correct.

## Auth

- [[problems/tls-fails-from-services-but-the-site-loads-fine-in-a-browser|TLS fails from every service, but the site loads fine in a browser]] — nginx served a leaf with no intermediate; only clients that chase AIA could paper over it.

## Config

- [[problems/prepared-statement-errors-appear-only-under-concurrency|"prepared statement does not exist" appears only under concurrency, never in staging]] — transaction pooling was configured correctly and its consequences were not read.

## DNS

- [[problems/writes-keep-failing-with-read-only-transaction-after-failover|Writes keep failing with "read-only transaction" long after the failover finished]] — every symptom pointed at the database; the cause was a JVM holding a resolved address for the process lifetime.

## Observability

- [[problems/a-40-minute-outage-passed-with-no-alert|A 40-minute outage passed with no alert, and the error-rate panel showed a flat line]] — a metric rename left ~40 critical rules evaluating to an empty vector, which looks exactly like healthy.

## Capacity

---

Unsure which section a note goes in? Its `root_cause` line is not written yet. Leave it out of the map until it is — see [[meta/capture-workflow|capture workflow]].
