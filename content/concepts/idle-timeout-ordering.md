---
title: "Idle timeout ordering"
description: "Two sides of a keep-alive connection each decide when it is stale. If the wrong one decides first, requests die in the gap."
tags:
  - meta
---

An HTTP keep-alive connection is held open by both ends, and **both ends have their own idle timeout**. Neither tells the other what it is. Whichever expires first closes the connection.

That is fine when the **client** closes: it knows it is done with the connection and simply opens a new one for the next request. It is not fine when the **server** closes, because closing is not instantaneous:

1. The connection has been idle near the server's timeout.
2. The server sends `FIN` and stops accepting requests on it.
3. In the same instant, the client — which believed the connection good for another few minutes — writes a request onto it.
4. The request is never answered. The client sees a reset, not a response.

The window is small, and it is entered every single time a connection reaches the server's idle timeout while the client still considers it live. Multiply by connection count and by hours, and it is a steady trickle of failures that no component reports as an error, because from each side nothing wrong happened.

## The rule

**The client's idle timeout must be strictly shorter than the server's**, with enough margin to cover the network. Then the client always closes first, and the race has no window.

Typical defaults, which are not ordered correctly out of the box:

| Hop | Setting | Default |
| --- | --- | --- |
| AWS ALB → backend | idle timeout | 60s |
| Envoy / Istio sidecar → upstream | `idleTimeout` | 1 hour |
| nginx → upstream | `keepalive_timeout` | 60s (server side) |
| Go `http.Server` | `IdleTimeout` | unset, falls back to `ReadTimeout`, else none |
| Node.js `http.Server` | `keepAliveTimeout` | 5s |

Node's 5s against an ALB's 60s is the well-known version of this — the load balancer holds a connection the server abandoned fifty-five seconds ago, and the result is intermittent 502s. Istio's one hour against a backend that closes at 60s is the same bug with the roles swapped, and is [[problems/intermittent-503s-only-on-the-quiet-endpoints|why the quiet endpoints fail and the busy ones do not]].

The rule applies **per hop**, not once. A request crossing load balancer → ingress → sidecar → application passes through three independent pairs of timeouts, and each pair has to be ordered on its own.

## Diagnosing it

The signature is inverted load-dependence: **busy paths are fine, idle paths fail**, because only an idle connection ever reaches the timeout. Anything that fails more at 4am than at peak should be suspected here.

In Envoy, the response flag names it outright — `UC` is upstream connection termination:

```text
[2026-08-22T04:11:52.884Z] "POST /v1/quote HTTP/1.1" 503 UC 0 95 0 - "-" ...
```

Retrying on reset hides it, and is a legitimate mitigation for idempotent requests. It is not a fix: the connection is still being closed underneath the client, and now every occurrence costs an extra round trip instead of an error.
