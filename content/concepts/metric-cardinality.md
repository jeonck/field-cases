---
title: "Metric cardinality"
description: "One time series per unique label combination. Put an unbounded value in a label and you have asked for an unbounded number of them."
tags:
  - meta
---

A Prometheus time series is identified by its metric name **plus every label value**. `http_requests_total{method="GET", status="200"}` and `http_requests_total{method="GET", status="500"}` are two different series, stored and indexed separately, each with its own memory footprint in the head block.

Cardinality is therefore the product of every label's distinct values:

```text
methods (5) × statuses (8) × handlers (40) × pods (24)  =  38,400 series
```

Multiply in one label whose values are unbounded — a user ID, a session ID, an order ID, a full request path containing IDs, an error message string — and the product is no longer a number anyone chose:

```text
… × path (one per order ever seen)  =  unbounded
```

## What it costs

Active series live in memory. A few kilobytes each sounds negligible until the count moves by an order of magnitude, and the failure is not graceful:

- memory grows with the series count until the process is killed
- on restart, the write-ahead log must be replayed, and **replay time grows with the same number** — so a large head block turns each restart into minutes of unavailability
- during replay nothing is scraped, so every restart cuts a hole in every dashboard and every alert

That last point is what makes this worse than an ordinary capacity problem: the monitoring system's recovery is itself an outage of monitoring.

## The rule

**A label value must come from a set you can name.** Method, status class, route *template*, service, environment, region — all bounded, all enumerable in advance. Anything derived from user input or from an identifier is not a label; it belongs in a log or a trace, where the storage model expects high cardinality.

Route templates are the common trap: `/v1/orders/:id` is one series, `/v1/orders/1a77c0e4/items` is one series *per order* — see [[problems/all-dashboards-developed-gaps-and-prometheus-restarted-every-twenty-minutes|what that does to a monitoring stack]].

## Finding and bounding it

```promql
topk(5, count by (__name__)({__name__=~".+"}))     -- which metric
count(count by (path) (http_requests_total))       -- which label, and how bad
```

```bash
promtool tsdb analyze /prometheus     # highest-cardinality metric names and labels, offline
```

The structural control is a per-scrape-pool limit, and it matters more than any amount of discipline:

```yaml
scrape_configs:
  - job_name: kubernetes-pods
    sample_limit: 20000
    label_limit: 30
    label_value_length_limit: 200
```

A target that exceeds `sample_limit` fails **its own** scrape and is marked down. Without it, one badly-labelled service degrades the monitoring of everything else — the limit's real purpose is to make the blast radius the target rather than the cluster.
