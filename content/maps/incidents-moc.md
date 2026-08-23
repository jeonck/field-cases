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
- [[problems/intermittent-503s-only-on-the-quiet-endpoints|Intermittent 503s that hit the quiet endpoints and never the busy ones]] — the sidecar held idle connections for an hour against a server that closed them at 60 seconds.

## Storage

- [[problems/df-says-the-disk-is-full-du-says-it-is-half-empty|df says the disk is full, du says it is half empty]] — logrotate unlinked the logs, nginx kept the descriptors, and the blocks were never freed.

## Database

- [[problems/the-same-query-is-fast-on-a-fresh-connection-and-slow-after-the-fifth-run|The same query is fast on a fresh connection and slow forever after the fifth run]] — a backfill skewed the data, and the planner's generic plan was built for a value the application never asks for.
- [[problems/every-query-on-one-table-stopped-and-the-migration-had-not-started-yet|Every query on one table stopped, and the migration that caused it had not started yet]] — readers queued behind a lock that was never granted, while the database sat at 4% CPU.

## Compute

- [[problems/p99-latency-spikes-while-cpu-utilisation-sits-at-30-percent|p99 latency spikes to 400ms while CPU utilisation sits at 30%]] — bigger nodes multiplied thread count against an unchanged CPU quota; both numbers on the dashboard were correct.
- [[problems/pods-restart-every-few-hours-with-nothing-in-the-application-log|Pods restart every few hours with nothing in the application log and the heap at 40%]] — RSS crossed the container limit on memory the JVM does not account for; the kill leaves no evidence inside the process.

## Auth

- [[problems/tls-fails-from-services-but-the-site-loads-fine-in-a-browser|TLS fails from every service, but the site loads fine in a browser]] — nginx served a leaf with no intermediate; only clients that chase AIA could paper over it.

## Config

- [[problems/prepared-statement-errors-appear-only-under-concurrency|"prepared statement does not exist" appears only under concurrency, never in staging]] — transaction pooling was configured correctly and its consequences were not read.
- [[problems/consumer-lag-grows-for-hours-while-the-consumers-look-healthy|Consumer lag grows for hours while every consumer is running and logging normally]] — client defaults stopped fitting the workload when a downstream got slower; the group replayed the same batch for four hours.
- [[problems/the-fix-is-deployed-and-a-third-of-requests-still-hit-the-old-bug|The fix is deployed, the rollout succeeded, and a third of requests still hit the old bug]] — a re-pushed tag left cached layers on older nodes; every tool compared tags and agreed.
- [[problems/a-40-second-blip-became-a-25-minute-outage|A 40-second downstream blip became a 25-minute outage that continued after the downstream recovered]] — retries at two layers multiplied one request into nine; the load sustaining the outage was the outage's own output.
- [[problems/the-nightly-reconciliation-ran-twice-and-both-runs-logged-success|The nightly reconciliation ran twice and both runs logged success]] — a wall-clock schedule sat in the hour that occurs twice on the fall-back night.

## DNS

- [[problems/writes-keep-failing-with-read-only-transaction-after-failover|Writes keep failing with "read-only transaction" long after the failover finished]] — every symptom pointed at the database; the cause was a JVM holding a resolved address for the process lifetime.

## Observability

- [[problems/a-40-minute-outage-passed-with-no-alert|A 40-minute outage passed with no alert, and the error-rate panel showed a flat line]] — a metric rename left ~40 critical rules evaluating to an empty vector, which looks exactly like healthy.
- [[problems/all-dashboards-developed-gaps-and-prometheus-restarted-every-twenty-minutes|All dashboards developed gaps and Prometheus restarted every twenty minutes]] — a raw request path used as a label created one time series per order; the WAL replay after each kill was the actual outage.

## Capacity

- [[problems/connect-fails-with-cannot-assign-requested-address-at-peak|connect() fails with "cannot assign requested address" at peak, and the downstream is healthy]] — an undrained response body on an error path turned every rate-limited request into a burned TCP connection.

---

Unsure which section a note goes in? Its `root_cause` line is not written yet. Leave it out of the map until it is — see [[meta/capture-workflow|capture workflow]].
