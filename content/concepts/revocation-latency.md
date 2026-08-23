---
title: "Revocation latency"
description: "Every cache between a permission and its enforcement is time during which a removed permission still works. That time is a number, and somebody should have chosen it."
tags:
  - meta
---

Granting access is easy to verify: you try it, and it works. Removing access is not, because the observable result of a correct revocation is that **nothing happens** — and that is also the observable result of a revocation that silently did not take effect.

Between the moment a permission is removed and the moment requests actually stop being allowed, there is a chain, and every link contributes:

| Layer | Typical delay |
| --- | --- |
| HR or ticketing system → identity provider | minutes to hours |
| Identity provider group propagation | seconds to minutes |
| Directory or membership sync into the policy engine | one sync interval |
| **Decision cache in the enforcement path** | **its TTL, in full** |
| Access token already issued | the token's remaining lifetime |
| Long-lived session or API key | potentially forever |

The total is the **revocation latency**, and it is the sum of the chain, not the largest link. Most organisations can state their token TTL and none of the others.

## The cache that is reviewed for the wrong property

A decision cache in front of an authorization engine is added for latency, and it is reviewed the way caches are usually reviewed: does it return the right answer? On the allow path it does — that is what makes it a good cache.

The question that does not get asked is the opposite one: **when the correct answer changes, how long does the wrong one survive?** For a product catalogue that is a staleness annoyance. For an authorization decision it is the window in which a removed account keeps working, and a six-hour TTL chosen for performance is a six-hour revocation SLA chosen by accident.

## Two design rules

**Derive TTLs from a stated revocation SLA, not from a latency target.** Decide the number first — "a revoked principal loses access within 60 seconds" — and let every cache in the chain be sized by it. Without a stated number, each layer picks its own and nobody owns the sum.

**Never cache the deny path.** A short, uncached check against a revocation list on every request means the cache can only ever be wrong in the safe direction: it may deny something it should allow, which produces a complaint, an investigation, and a fix. Being wrong in the allow direction produces nothing at all.

## Making it visible

The only thing that makes this findable is recording *how* a decision was reached, not just what it was:

```text
{"ts":"…","principal":"svc-contractor-04","decision":"allow","source":"cache","age_s":11840}
```

`source` and `age_s` turn an audit log into something you can ask questions of. Without them, an allow decision for a revoked principal is indistinguishable from an allow decision for a valid one — which is why [[problems/a-revoked-account-kept-working-for-four-hours|this class of failure is normally found by an access review rather than by monitoring]].

The detector follows from that: join revocation timestamps against subsequent allow decisions, on a schedule. There is no live signal to alert on, because nothing fails.
