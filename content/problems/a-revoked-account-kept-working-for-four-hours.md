---
title: "A revoked account kept working for four hours, and nothing anywhere recorded a problem"
date: 2026-08-22
tags:
  - target/opa
  - layer/auth
  - symptom/silent-failure
status: solved
severity: P1
env: "Central authorization service with a 6h decision cache / IdP group sync every 15 min / 15-minute access tokens"
symptom: "{\"principal\":\"svc-contractor-04\",\"decision\":\"allow\",\"source\":\"cache\",\"age_s\":11840}"
root_cause: "Group membership was revoked and synced correctly, but the authorization decision cache was keyed on (principal, resource, action) with a 6-hour TTL and was never invalidated by a membership change, so already-warm allow decisions kept being served."
---

*Example note — the format applied to a well-documented failure mode, not a specific engagement.*

## Symptom

There isn't one. That is the entire point of this note and the reason it is written up rather than fixed quietly.

- **Since when:** the decision cache was introduced five months earlier. Every revocation since has had the same window; only one was ever examined.
- **Reproduces when:** the revoked principal had made the same request recently enough for a decision to be cached. Reproducible in a minute: call an endpoint, revoke the group, call it again.
- **Does not reproduce when:** the principal is new, or is calling something it has not called before. A cold cache key produces a correct denial immediately, which is why ad-hoc "is revocation working?" checks pass — they are usually run against a fresh path.

Found during a quarterly access review that joined HR termination timestamps against the authorization audit log. Eleven principals had `allow` decisions after their revocation timestamp. The record that made it visible at all:

```text
{"ts":"2026-08-22T09:14:02Z","principal":"svc-contractor-04","action":"read",
 "resource":"orders","decision":"allow","source":"cache","age_s":11840}
```

`source: cache` and `age_s: 11840` — a decision made three hours and seventeen minutes earlier, served after the account had been removed. No alert fired, no error was logged, no dashboard moved, and nothing would have. **The system did exactly what it was built to do, quickly.**

## Environment

| | |
| --- | --- |
| Product / version | central authorization service (OPA-based), decision cache TTL 6h |
| Deployment | policy sidecars in ~30 services, membership synced from the IdP every 15 min |
| Scale | ~9k authorization decisions/s, ~94% served from cache |
| **Last change** | **the decision cache was added five months earlier to bring authorization p99 down from 40 ms to 2 ms. Load tested, correctness tested against the policy suite, and shipped. Every test asserted that the cache returns the right answer.** |

Nobody tested how long it returns the *previous* answer after the right one changes, because that is not a property anyone thinks to write a test for.

## Investigation

This was investigated after the fact, from logs, with no incident to observe.

| Hypothesis | How it was checked | Verdict |
| --- | --- | --- |
| The IdP never processed the removal | IdP audit shows group removal at 05:56 and the membership sync consuming it at 06:05 | rejected |
| The principal held a second entitlement granting the same access | Entitlement diff before and after shows one group removed and nothing else granting `orders:read` | rejected |
| A long-lived token issued before revocation was still valid | Tokens are 15 minutes. The requests at 09:14 carried tokens **issued at 09:12**, after the revocation. Authentication was entirely correct | rejected — and this is the fork that matters |
| A policy bug allowing the action without the group | Replayed the same input against the policy engine with the cache bypassed: `deny` | rejected |
| The decision cache is not invalidated by membership changes | `age_s` values up to 21,600 on allow decisions for revoked principals; cache keys contain no membership version | **accepted** |

The third row is where this stops being an authentication problem. Fresh tokens, valid signatures, correct identity — and the wrong answer, because **authentication was re-evaluated every 15 minutes and authorization was not re-evaluated for six hours**. Those two clocks were never compared; see [[concepts/revocation-latency|revocation latency]].

## Root cause

The cache key is `(principal, resource, action)`. It contains nothing about the input that actually determined the decision — group membership — so a membership change produces no key change and no invalidation. The entry simply lives out its six hours.

Revocation propagated correctly through every layer that was designed for it: HR to IdP in minutes, IdP to policy engine in nine. Then it stopped at a component added afterwards for latency, which nobody had placed in the revocation chain because it was not thought of as part of that chain at all.

The total revocation latency was therefore up to six hours and fifteen minutes, and no document anywhere stated what it was supposed to be.

## Fix

Immediate — flush the cache, and add the flush to the deprovisioning runbook so the gap is bounded by human process until it is bounded by code:

```bash
curl -XPOST http://authz.example.internal:8181/v1/cache/purge
```

Then invalidate on the event rather than on a timer. The IdP publishes membership changes; the authorization service consumes them and drops every key for that principal:

```text
idp.membership.changed{principal}  →  authz: evict(principal, *)
```

And cap what any timer can cost, by deriving the TTL from a number that is now written down — a stated revocation SLA of 60 seconds:

```yaml
decision_cache:
  ttl: 30s          # half the stated 60s revocation SLA
  max_entries: 500000
```

p99 rose from 2 ms to 3 ms, which is the entire price of the whole fix.

The structural change is a revocation check that is **never** cached — a small denylist consulted on every request before the cache is read:

```rego
default allow = false
allow { not data.revoked[input.principal]; cached_decision }
```

With that in place the cache can only ever be wrong in the deny direction, which produces a complaint and a ticket. Being wrong in the allow direction produces nothing.

## Prevention

- **Detection:** a scheduled join of revocation timestamps against subsequent `allow` decisions. This is the only detector that works here, because there is no live failure signal to alert on — the reason this was found at all is that one engineer had added `source` and `age_s` to the decision log while debugging something unrelated.
- **Prevention:** state the revocation SLA as a number and derive every TTL in the chain from it. The failure was not that six hours is too long; it is that six hours was never a decision anyone made about revocation — it was a decision about latency that silently became one about security.
- **Remaining debt:** the same decision cache backs two other services with their own TTLs, unchanged. Token and session lifetimes were reviewed as part of this, but the API key path was not — API keys have no expiry at all and their revocation goes through a different code path that nobody has traced end to end.

## Open questions

- Whether any of the eleven principals did anything harmful during their windows is not established. The audit log records the decision, not the request body, so "they were read-only calls" is an inference from the `action` field and not from what was actually read.
- Would the access review have found this without `source` and `age_s` in the log? Almost certainly not — an `allow` for a revoked principal is indistinguishable from a normal `allow` without them, and they were added for unrelated debugging. That makes the detection an accident, and no process ensures similar fields exist elsewhere.
- What the acceptable revocation SLA actually is remains unanswered. 60 seconds was chosen because it sounded small and because it was comfortably implementable. Nobody with authority over the risk has said whether it is right, and the number is now load-bearing.

## Related

Parent concept: [[concepts/revocation-latency|revocation latency]]. Filed under Auth in the [[maps/incidents-moc|incident map]].

Sibling: [[problems/a-40-minute-outage-passed-with-no-alert|the alerts that had been inert for six weeks]]. Both are failures with no symptom, found only because somebody went and looked — there an outage that raised nothing, here a permission that outlived its removal. Neither has a live signal to alert on, and in both the detection had to be built as a periodic *comparison* rather than as a threshold, because the failing state and the healthy state are identical from the outside.

Worth contrasting with the stale-cache notes on this site — [[problems/writes-keep-failing-with-read-only-transaction-after-failover|the JVM holding a resolved address]] and [[problems/the-fix-is-deployed-and-a-third-of-requests-still-hit-the-old-bug|the nodes holding cached image layers]]. Those are the same mechanism and the opposite consequence: a stale cache made something stop working, loudly, and got fixed within hours. Here a stale cache made something **keep** working, and it survived five months. The mechanism does not determine how long a bug lives; the direction of its failure does.
