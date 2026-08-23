---
title: "All dashboards developed gaps and Prometheus restarted every twenty minutes"
date: 2026-08-22
tags:
  - target/prometheus
  - layer/observability
  - symptom/outage
  - symptom/resource-exhaustion
status: solved
severity: P1
env: "Prometheus 2.53, 40 GiB limit / ~2.8M active series normally / one service added a raw request path as a label"
symptom: "prometheus_tsdb_head_series 19,412,880 against a 2.8M baseline"
root_cause: "A service began labelling http_requests_total with the raw request path, which contains order IDs, so every order created a new time series. Head memory crossed the container limit, and each restart replayed a WAL that had grown with it."
---

*Example note — the format applied to a well-documented failure mode, not a specific engagement.*

## Symptom

- **Since when:** gaps appeared four days after a deploy, and worsened daily. There was no sharp onset to correlate with anything — the rollout was gradual and the series count grew with it.
- **Reproduces when:** the affected service handles traffic. Every distinct order ID it serves adds a series permanently for the retention of the head block.
- **Does not reproduce when:** the service is scaled to zero, which was tried at 03:00 as a desperate measure and did stop the growth — without reclaiming anything already in the head.

Every dashboard in the organisation had holes in it, several minutes wide, several times an hour:

```text
prometheus_tsdb_head_series   19,412,880      (baseline 2.8M)
```

And the reason the holes were so wide — this is the line that turned "Prometheus is unhappy" into "Prometheus cannot recover":

```text
level=info ts=2026-08-22T04:12:09.881Z caller=head.go:773 component=tsdb
  msg="WAL replay completed" duration=8m41.223s
```

Eight minutes and forty-one seconds of replay after every kill, during which nothing was scraped and no alert could evaluate. Prometheus was being killed roughly every twenty minutes, so a third of the day had no monitoring at all — including, for a while, the monitoring of the incident.

## Environment

| | |
| --- | --- |
| Product / version | Prometheus 2.53, single instance, 15-day retention |
| Deployment | `limits.memory: 40Gi`, later 64Gi, ~600 scrape targets |
| Scale | ~2.8M active series normally, ~90k samples/s |
| **Last change** | **four days earlier, one service added a `path` label to its request counter, populated with the raw request path. Its paths contain order IDs. The change was three lines and reviewed by two people.** |

Nothing about the change looks dangerous in a diff. `path` is an obvious thing to want, the raw path is the obvious thing to put in it, and the cost is paid in a different system owned by a different team.

## Investigation

| Hypothesis | How it was checked | Verdict |
| --- | --- | --- |
| A scrape target is down or misbehaving | `up == 1` across all 600 targets; target count flat for weeks | rejected |
| Prometheus simply needs more memory | It was raised 40 → 64 GiB. It survived longer and still died — which is the signature of growth, not of a fixed shortfall. Two hours were spent here | rejected, and it is the most seductive wrong answer |
| Compaction or retention is broken | Block sizes and compaction durations normal; disk was never the constraint | rejected |
| More pods, therefore more series | Pod count and target count unchanged | rejected |
| One metric's cardinality exploded | `topk` by metric name shows a single metric holding 16.4M of the 19.4M series | **accepted** |

Two queries answer this, and neither takes longer than a scrape interval:

```promql
topk(5, count by (__name__)({__name__=~".+"}))
count(count by (path) (http_requests_total))
```

The second returned 16,380,442 — the number of distinct `path` values, which is approximately the number of orders the service had seen since the deploy. See [[concepts/metric-cardinality|metric cardinality]].

Offline, against a block, `promtool tsdb analyze /prometheus` reports the same thing under "highest cardinality labels" and does not require a working Prometheus to run — which mattered, because Prometheus was not reliably up.

## Root cause

Every unique combination of label values is a separate time series. The service labelled its request counter with paths like `/v1/orders/1a77c0e4-f2b9-4c1e-9a77-1a77c0e4f2b9/items`, so each order it had ever served held its own series, forever, at a few kilobytes of head memory each.

Memory crossed the container limit, the process was killed, and on restart the write-ahead log had to be replayed — and the WAL had grown along with the series count, so recovery took eight minutes and produced exactly the gaps that made the problem hard to see. The system was in a loop where its own failure destroyed the evidence of its failure.

## Fix

Immediate — drop the label at ingestion, which requires no deploy from the offending service:

```yaml
scrape_configs:
  - job_name: kubernetes-pods
    metric_relabel_configs:
      - regex: "path"
        action: labeldrop
```

Head series fell to 3.1M within one retention of the head block, and the restarts stopped.

Then the code, using the route template rather than the resolved path:

```text
/v1/orders/:id/items          ← one series
/v1/orders/1a77c0e4…/items    ← one series per order
```

And the structural control, which is the part worth taking from this note:

```yaml
    sample_limit: 20000
    label_limit: 30
    label_value_length_limit: 200
```

A target exceeding `sample_limit` fails its own scrape and shows as down. That is a small, loud, correctly-attributed failure belonging to one team, instead of a large silent one belonging to everybody. No limits were configured on any scrape pool before this.

## Prevention

- **Detection:** alert on `prometheus_tsdb_head_series` growth rate and on per-target `scrape_samples_post_metric_relabeling` crossing a threshold. Both would have fired on day one of four, while the series count was merely unusual rather than fatal.
- **Prevention:** `sample_limit` on every scrape pool, so the blast radius of a bad label is the service that shipped it. Combined with a stated rule — a label value must come from a set you can enumerate in advance — which is now in the service template's comments and nowhere more binding than that.
- **Remaining debt:** limits were added to the pods pool only; the four other scrape pools are still unbounded. `topk` also showed three more metrics above 100k series that nobody has looked at, which are not causing pain and are the same class of thing.

## Open questions

- Whether a smaller head block — shorter retention, or remote write with a leaner local head — would have kept restarts survivable. The eight-minute replay is what converted a memory problem into a monitoring outage, and nothing was changed about it.
- The four-day gradual rollout means the onset cannot be pinned to a moment. The correlation to the deploy was established by reading the diff after the metric was identified, not from the data, and if the label had been added in a less obvious commit it is not clear how it would have been found.
- Nobody has audited whether the same raw-path labelling exists in tracing or logging pipelines for this service, where high cardinality is expected and the cost is money rather than an outage. The assumption is that it is fine there. It has not been priced.

## Related

Parent concept: [[concepts/metric-cardinality|metric cardinality]]. Filed under Observability in the [[maps/incidents-moc|incident map]].

Sibling: [[problems/a-40-minute-outage-passed-with-no-alert|the alerts that had been inert for six weeks]] — the same system failing in the exact opposite direction. There, monitoring reported healthy because the data it judged had stopped arriving; here it collapsed because the data was unbounded. Too little and too much produce the same end state, which is that nobody can see anything, and both were caused by an ordinary change to a metric made by someone who had no reason to think about the monitoring system at all.

Contrast with [[problems/pods-restart-every-few-hours-with-nothing-in-the-application-log|the JVM pods killed for native memory]]: both are processes OOM-killed by the kernel for memory outside what anyone was watching, but that one died silently and left nothing, while this one left a very clear record and still cost more — because its recovery took eight minutes and its recovery was the thing everyone depended on. A failure that leaves evidence is usually the lucky one; a failure in the system that stores the evidence is the exception.
