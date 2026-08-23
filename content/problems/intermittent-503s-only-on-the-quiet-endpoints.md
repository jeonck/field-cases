---
title: "Intermittent 503s that hit the quiet endpoints and never the busy ones"
date: 2026-08-22
tags:
  - target/istio
  - target/golang
  - layer/network
  - symptom/intermittent-failure
status: solved
severity: P2
env: "Istio 1.21 sidecars / Go 1.22 upstream with IdleTimeout 60s / sidecar connection pool at defaults"
symptom: "\"POST /v1/quote HTTP/1.1\" 503 UC 0 95 0 - \"-\" upstream_reset_before_response_started{connection_termination}"
root_cause: "The sidecar's connection pool holds idle upstream connections for an hour while the Go server closes them at 60 seconds, so any request written onto a connection the server was closing at that instant failed."
---

*Example note — the format applied to a well-documented failure mode, not a specific engagement.*

## Symptom

- **Since when:** three weeks after the service was onboarded to the mesh. The rate never changed and never escalated — a flat 0.05% of requests, indefinitely.
- **Reproduces when:** a connection has been idle for around 60 seconds and is then reused. Reproducible with a two-request script and a `sleep 61` between them.
- **Does not reproduce when:** the endpoint is busy. This is the diagnostic fact and it was treated as noise for two weeks — **the high-traffic paths never fail, and the quiet internal endpoints fail constantly.** Failure rate is highest at 04:00 and lowest at peak.

```text
[2026-08-22T04:11:52.884Z] "POST /v1/quote HTTP/1.1" 503 UC 0 95 0 - "-"
"-" "9f3cbb21-d5ae-4c1e-9a77-1a77c0e4f2b9" "pricing.example.internal:8080"
"10.0.0.30:8080" outbound|8080||pricing.default.svc.cluster.local -
10.0.0.30:8080 10.0.0.12:48122 - - upstream_reset_before_response_started{connection_termination}
```

`UC` is Envoy's flag for upstream connection termination, and `0 95 0` says zero bytes received, 95 milliseconds elapsed, zero response. The request left, the connection died, nothing came back. The upstream logged nothing at all for these requests, because from its side they never arrived.

## Environment

| | |
| --- | --- |
| Product / version | Istio 1.21, Envoy sidecars, Go 1.22 upstream |
| Deployment | sidecar connection pool at defaults (`idleTimeout` 1 hour) |
| Scale | ~900 req/s in aggregate; the affected endpoints run at 2–15 req/s |
| **Last change** | **the service was onboarded to the service mesh three weeks earlier. The upstream's `IdleTimeout: 60 * time.Second` had been in its server config for two years and is correct.** |

Neither side is misconfigured on its own. The Go server setting an explicit idle timeout is good practice, and Istio's default connection pool is the documented default. They were simply never ordered against each other, because before the mesh there was no second timeout to order against.

## Investigation

| Hypothesis | How it was checked | Verdict |
| --- | --- | --- |
| The upstream pods are restarting or being OOMKilled | Zero restarts across three weeks; `kubectl describe` clean | rejected |
| The upstream is overloaded and shedding | The failure rate is **inversely** correlated with traffic — worst at 04:00, best at peak. Overload does not do that | rejected, and the inversion is the entire clue |
| Sidecar CPU throttling delaying responses | `cpu.stat` throttle ratio under 1%, after [[problems/p99-latency-spikes-while-cpu-utilisation-sits-at-30-percent|a previous incident]] made this the reflex check on this cluster | rejected |
| mTLS handshake failures between sidecars | Would show as `UF` with a TLS error, not `UC` on an established connection; handshake metrics flat | rejected |
| Requests are being written onto connections the upstream is closing | Every failing request is on a connection idle for ~60s. Reproduced deliberately: request, `sleep 61`, request — the second fails | **accepted** |

Inverted load-dependence is the fingerprint. Almost everything else gets worse under load; only an idle-connection problem gets *better*, because busy connections never sit long enough to reach anyone's timeout — see [[concepts/idle-timeout-ordering|idle timeout ordering]].

## Root cause

Both ends of a keep-alive connection hold their own idle timeout and neither announces it. The Go server closes at 60 seconds. The sidecar's pool holds connections for an hour, so it believes every connection is good long after the server has stopped agreeing.

When the server's timer fires it sends `FIN` and stops serving that connection. If the sidecar writes a request into that same instant, the request is lost — not refused, not timed out, simply written into a connection that is being torn down. Envoy reports `UC`, the upstream logs nothing, and neither side did anything wrong.

The quiet endpoints fail because they are quiet. A connection serving 200 req/s never idles for 60 seconds; a connection serving 2 req/s does so constantly.

## Fix

Make the client close first, by a margin:

```yaml
apiVersion: networking.istio.io/v1
kind: DestinationRule
metadata:
  name: pricing
spec:
  host: pricing.default.svc.cluster.local
  trafficPolicy:
    connectionPool:
      http:
        idleTimeout: 30s        # strictly below the upstream's 60s
```

The equivalent from the other direction — raising the server above the client — is also valid and was not chosen here, because the sidecar default of one hour would have meant an unreasonably long server timeout. Between the two, lowering the client is almost always the smaller number to defend.

Retries on reset were added as a safety net, with the constraint stated rather than assumed:

```yaml
retries:
  attempts: 2
  retryOn: reset
```

`retryOn: reset` re-sends the request. That is safe here because these endpoints are idempotent; it would not be safe for the payment endpoints two services over, and applying this mesh-wide would have been the wrong instinct.

Failure rate went from 0.05% to roughly 0.002% within minutes of the DestinationRule applying.

## Prevention

- **Detection:** alert on the `UC` response flag rate specifically, not on the 503 rate. The flag names the cause, and a 503 rate alert would have to be set so low to catch 0.05% that it would fire on everything else.
- **Prevention:** write the ordering down per hop. A request here crosses ALB → ingress gateway → sidecar → application, which is three independent pairs of timeouts, and the rule — client strictly below server — has to hold at each one. Only the sidecar-to-application hop was examined during this incident.
- **Remaining debt:** the ALB in front has a 60-second idle timeout and the ingress gateway's setting was never established, so one of the three hops is fixed and two are unknown. Every other service onboarded to the mesh inherits the same one-hour pool default against whatever its own server does, and no sweep has been run.

## Open questions

- The residual 0.002% was never explained. It may be genuine upstream restarts, a second race elsewhere in the path, or the same race at a different hop. It stopped being urgent the moment it stopped being visible, which is the honest reason it was not chased.
- `maxRequestsPerConnection` was left at its default. Whether it interacts with this — by recycling connections before either timeout matters — was not investigated, and it might make the timeout ordering moot or might just hide it.
- Whether retries have been quietly masking this class of failure on other services all along. Several have `retryOn` configured from a template nobody remembers writing, and a masked failure produces no signal to count.

## Related

Parent concept: [[concepts/idle-timeout-ordering|idle timeout ordering]]. Filed under Network in the [[maps/incidents-moc|incident map]].

Sibling: [[problems/connect-fails-with-cannot-assign-requested-address-at-peak|the ephemeral port exhaustion at peak]], which is this problem's mirror image. There, connections were not being reused at all and the cost was exhausting a finite local resource; here, connections are reused for far too long and the cost is using one the other end has abandoned. Keep-alive has a failure mode on both sides of correct, and the two notes together are more useful than either alone: if connection handling is suspect, the question is which direction it is wrong in.

Contrast with [[problems/consumer-lag-grows-for-hours-while-the-consumers-look-healthy|the Kafka rebalance loop]]: both are two independently-reasonable timeouts that were never compared, but there the two settings lived in one config file owned by one team, and here they live in two systems owned by two teams with no place that shows them side by side. The second kind does not get fixed by reading your own configuration more carefully.
