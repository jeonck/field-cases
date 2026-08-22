---
title: "Tag taxonomy — three axes, two tags per axis"
description: "target/, layer/, symptom/ — and nothing else except meta."
tags:
  - meta
---

Every problem note carries tags on three axes. **At most two per axis.** Three or more on one axis means the document is trying to be two documents.

## `target/` — what broke

The only axis allowed to grow. One tag per system actually involved.

`target/postgres`, `target/pgbouncer`, `target/kubernetes`, `target/nginx`, `target/kafka`, `target/redis`, `target/aws-rds`, `target/istio`, …

## `layer/` — where the cause finally was

Not where it hurt. Where it *was*. **Hard cap: 12 values.** Adding a thirteenth means one of these is wrong.

`layer/network`, `layer/storage`, `layer/database`, `layer/compute`, `layer/auth`, `layer/config`, `layer/dns`, `layer/observability`, `layer/capacity`

Leave the layer tag off while `root_cause` is still empty. Guessing here poisons the map.

## `symptom/` — what shape it had

`symptom/outage`, `symptom/intermittent-failure`, `symptom/latency`, `symptom/resource-exhaustion`, `symptom/data-inconsistency`, `symptom/silent-failure`, `symptom/deploy-failure`

## Off-axis

Exactly one: `meta`, for the operating documents of this site. Nothing else. Create a new tag only when no existing one covers the case — and for `layer/`, essentially never.

Related: [[meta/capture-workflow|capture workflow]], [[maps/incidents-moc|incident map]].
