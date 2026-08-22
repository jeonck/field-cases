---
title: "Absent data is not zero"
description: "A threshold alert on a series that no longer exists is not a quiet alert. It is not an alert at all."
tags:
  - meta
---

Every threshold-based alert makes a hidden assumption: that the thing it measures is being measured. When the series disappears — renamed, relabelled, dropped by a broken exporter, or never emitted because the process that emits it is dead — the alert does not fire and does not fail. It evaluates to nothing.

In PromQL this is precise and unforgiving. A comparison filters an instant vector, so:

```promql
rate(http_requests_total{status=~"5.."}[5m]) > 0.05
```

returns the empty vector when `http_requests_total` has no samples. An alerting rule whose expression returns nothing has no alerts to create, so its state is neither firing nor pending. It is indistinguishable, in every UI and every API, from a rule that is evaluating perfectly and finding nothing wrong.

**That is the failure: healthy and unmeasured render identically.**

## What actually catches it

`absent()` and `absent_over_time()` invert the question — they return a value precisely when nothing is there:

```promql
absent_over_time(http_requests_total[10m])            # fires when the series vanishes
absent(up{job="orders-api"})                          # fires when the target disappears entirely
```

Pairing every critical threshold alert with an absence guard is the whole technique. It doubles the rule count, which is why it is usually skipped, and it is the only thing that distinguishes "no errors" from "no data".

Two weaker measures are worth having anyway: `promtool test rules` in CI pins an expression against sample data so a rename breaks the build, and recording rules give renames a single place to happen instead of thirty.

## Beyond Prometheus

The shape is not specific to one system. Any check that asks "is this number too high" inherits it:

- a log-based alert on a pattern that a format change stopped producing
- a synthetic probe passing because it was never actually reaching the thing it claims to test
- a dashboard panel showing a flat line, which reads as calm and means empty

The general form: **an alert can only tell you about data it receives, and nothing about data it stops receiving.** Monitoring that a signal exists is a separate job from monitoring its value, and it is the one that gets left out — see [[problems/a-40-minute-outage-passed-with-no-alert|a 40-minute outage that raised nothing at all]].
