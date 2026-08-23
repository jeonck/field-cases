---
title: "Wall-clock schedules and DST"
description: "A local time is not a point on the timeline. Once a year one of them happens twice, and another never happens at all."
tags:
  - meta
---

A schedule expressed in local time — `30 1 * * *` in `America/New_York`, a systemd `OnCalendar=`, an application-level "run at 01:30" — assumes that every local time occurs exactly once per day. In any zone that observes daylight saving, twice a year that is false:

| Transition | What happens to the local clock | Effect on a 01:30 schedule |
| --- | --- | --- |
| **Fall back** | 01:00–02:00 occurs **twice**, once at UTC−4 and once at UTC−5 | fires **twice**, an hour apart |
| **Spring forward** | 02:00–03:00 **never occurs** | a 02:30 job **never fires** |

The two failures are opposites and they are the same bug. Whichever one the schedule happens to sit on decides which is discovered — and the skipped run is the quieter of the two, because nothing runs and nothing errors.

Different schedulers handle this differently and few document it precisely, so the honest position is that a wall-clock schedule inside the transition window has undefined behaviour in practice. Kubernetes CronJob gained `.spec.timeZone` precisely so schedules could follow business hours, which also means it inherited this.

## Two rules

**Schedule in UTC. Present in local time.** UTC has no repeated or missing hours, ever. A job that must align with a business day should be triggered on a UTC schedule and compute the business date itself — the calculation belongs in the job, where it can be tested, rather than in the scheduler, where it cannot.

**Make the job idempotent on its business date.** This is the fix that survives being wrong about everything else. A job keyed on the date it is processing does nothing the second time it runs, whatever caused the second run — DST, a manual re-run, a controller restart, an operator hitting the button twice. A job that can safely run twice does not need the schedule to be correct.

Note what does *not* help: a concurrency policy. Two runs an hour apart never overlap, so `concurrencyPolicy: Forbid` is satisfied by both of them.

```bash
# the tell — two runs of one schedule, one hour apart, at different UTC offsets
kubectl get jobs -l app=reconciliation \
  -o custom-columns=NAME:.metadata.name,START:.status.startTime
```

Timestamps with **different UTC offsets for the same local hour** are conclusive, and they are the only artefact that distinguishes this from a duplicate trigger — which is [[problems/the-nightly-reconciliation-ran-twice-and-both-runs-logged-success|what it looks like from the data]].
