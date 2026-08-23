---
title: "The nightly reconciliation ran twice and both runs logged success"
date: 2026-08-22
tags:
  - target/kubernetes
  - layer/config
  - symptom/data-inconsistency
status: solved
severity: P1
env: "Kubernetes 1.28 CronJob / schedule \"30 1 * * *\" with spec.timeZone America/New_York / non-idempotent batch"
symptom: "two Job objects for one schedule, startTime 2026-11-01T01:30:00-04:00 and 2026-11-01T01:30:00-05:00"
root_cause: "The schedule is a wall-clock time in a zone that observes daylight saving. On the fall-back night 01:30 local occurred twice, an hour apart, and the job had no idempotency key, so every ledger row was written twice."
---

*Example note — the format applied to a well-documented failure mode, not a specific engagement.*

## Symptom

- **Since when:** one night. The duplicates were found by a finance reconciliation two days later, not by anything technical.
- **Reproduces when:** the local clock repeats an hour and the schedule sits inside it. Once a year, on one night, for schedules between 01:00 and 02:00 local.
- **Does not reproduce when:** any other night of the year. Three hundred and sixty-four nights of evidence say the configuration is correct.

Both runs succeeded. Both logged normally. Neither produced a warning, a conflict, or a constraint violation, because nothing in the pipeline had any reason to think a second run was unusual:

```text
NAME                        START
reconciliation-29418570     2026-11-01T01:30:00-04:00
reconciliation-29418630     2026-11-01T01:30:00-05:00
```

Same local time. **Different UTC offsets.** That is the entire diagnosis and it is visible in the output of a single `kubectl get jobs`, provided anyone thinks to look at the offset rather than the time.

## Environment

| | |
| --- | --- |
| Product / version | Kubernetes 1.28 CronJob, `concurrencyPolicy: Allow`, `backoffLimit: 2` |
| Deployment | one CronJob, one container, writes ledger rows to PostgreSQL |
| Scale | ~40k rows per nightly run |
| **Last change** | **`spec.timeZone: America/New_York` was added four months earlier so that the run would line up with the business day rather than drifting against it twice a year. Reviewed, sensible, and the direct cause.** |

The change was made *because* someone was thinking about daylight saving. It fixed the drift it was meant to fix and introduced the repeated hour, which is a more specific failure and a much rarer one.

## Investigation

| Hypothesis | How it was checked | Verdict |
| --- | --- | --- |
| Someone re-ran the job manually | Kubernetes audit log has no `create` on Jobs from a user account that night | rejected |
| A pod retry inside one Job (`backoffLimit`) | Retries produce additional **pods** under one Job. These are two distinct Job objects with different names, both owned by the CronJob | rejected — and this is the fork worth being careful at |
| A duplicate CronJob object, or two controllers | One CronJob in one namespace; control plane is managed and single | rejected |
| The downstream duplicated on ingest | Two distinct batch IDs in the ledger, each internally consistent. The duplication is upstream of the database | rejected |
| The local hour repeated and the schedule fired in both | Two `startTime` values, identical local time, offsets `-04:00` and `-05:00` | **accepted** |

Everything above the last row is what you check when a job runs twice, and all of it is about *who triggered it*. Nobody triggered it twice — the local time occurred twice, which is not a category anyone has a hypothesis for until they have seen it once. See [[concepts/wall-clock-schedules|wall-clock schedules and DST]].

## Root cause

`30 1 * * *` in `America/New_York` means "when the local clock reads 01:30". On the fall-back night the local clock reads 01:30 twice — once at UTC−4 and again an hour later at UTC−5 — and the schedule was satisfied both times.

The job had no idempotency key. It selected the previous day's transactions and wrote ledger rows, so the second run selected the same transactions and wrote the same rows again. Nothing rejected them because nothing in the schema said a business date could only be reconciled once.

`concurrencyPolicy: Forbid` would not have prevented this, and it was the first thing proposed. The two runs are an hour apart and never overlap, so a concurrency policy is satisfied by both of them.

## Fix

The data repair came first and was the expensive part — the duplicate batch was identified by its batch ID and reversed, with finance verifying the totals afterwards.

The schedule moves to UTC, and the job computes its own business date:

```yaml
spec:
  schedule: "30 5 * * *"        # 01:30 America/New_York in winter, expressed in UTC
  # spec.timeZone removed — UTC has no repeated or missing hours
```

The change that actually matters is the idempotency key, because it holds regardless of anything above:

```sql
ALTER TABLE ledger_batch
  ADD CONSTRAINT ledger_batch_business_date_key UNIQUE (business_date);
```

The job now claims its business date before doing any work, and a second invocation exits zero having done nothing. That covers DST, manual re-runs, controller restarts, and an operator pressing the button twice — none of which the schedule fix addresses.

## Prevention

- **Detection:** alert when the count of completed runs for a business date is not exactly one. This catches the double run **and** the spring-forward case where a run is silently skipped, which nothing else here would notice at all.
- **Prevention:** UTC in the scheduler, timezone logic in the job, where it can be unit tested. A wall-clock schedule falling between midnight and 04:00 in a DST-observing zone should be treated as a defect on sight.
- **Remaining debt:** the mirror case has not happened yet. In spring, a schedule in the skipped hour simply does not run, produces no Job, no logs, and no error. Three other CronJobs in the cluster carry `spec.timeZone`, two of them at 03:15 — inside the window. They were found by grep and have not been changed.

## Open questions

- The scheduler's exact behaviour here is inferred from two timestamps, not established from the controller's logic. Whether it will always fire twice, or did so for a reason particular to this controller version, was never confirmed — and it matters, because "it fires twice" and "it might fire twice" are different things to defend against.
- The reversal was verified by finance at the level of daily totals, not row by row. The totals match. Whether any individual row was reversed against the wrong original has not been checked and probably will not be.
- Whether the original drift complaint — the reason `timeZone` was added — is still a real problem once the job computes its own business date. Nobody asked, and the setting was removed on the assumption that it is not.

## Related

Parent concept: [[concepts/wall-clock-schedules|wall-clock schedules and DST]]. Filed under Config in the [[maps/incidents-moc|incident map]] — the schedule expressed something that is not well defined twice a year.

Sibling: [[problems/tls-fails-from-services-but-the-site-loads-fine-in-a-browser|the certificate chain that broke at renewal]]. Both are latent changes detonated by a **scheduled event** rather than by a deploy — a certificate renewal there, the calendar itself here — so both defeat the reflex of asking what shipped recently. The calendar is the worse of the two, because a renewal at least appears in a job log somewhere, while a clock going backwards appears nowhere and belongs to nobody.

Contrast with [[problems/the-same-query-is-fast-on-a-fresh-connection-and-slow-after-the-fifth-run|the query plan that flipped after a backfill]]: both are incidents with no deploy, where the thing that changed is absent from every change log — data there, the calendar here. The pattern worth carrying away is that "what changed" has more answers than the deploy pipeline knows about, and the two most common are the shape of the data and the date.
