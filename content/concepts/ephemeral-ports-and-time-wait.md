---
title: "Ephemeral ports and TIME_WAIT"
description: "The number of connections a host can open to one destination is fixed arithmetic, and it is smaller than most people expect."
tags:
  - meta
---

An outbound TCP connection is identified by a 4-tuple: source IP, source port, destination IP, destination port. Three of those are fixed when a service talks to one destination, so the only thing that varies is the **source port**, drawn from the ephemeral range:

```bash
sysctl net.ipv4.ip_local_port_range     # 32768 60999 by default — 28,232 ports
```

That is not a limit on connections. It is a limit on connections **to a single destination IP and port**, from a single source IP. Talking to two destinations doubles it; talking to one does not.

## The arithmetic that actually bites

A closed connection does not return its port immediately. The side that closes first enters `TIME_WAIT` and stays there for 60 seconds — `TCP_TIMEWAIT_LEN` is compiled in, and there is no sysctl for it. So the sustainable rate of **new** connections to one destination is:

```text
28,232 ports / 60 s  ≈  470 connections per second
```

Beyond that, ports are consumed faster than they are released, and `connect()` starts failing with `EADDRNOTAVAIL`:

```text
dial tcp 10.0.0.30:8080: connect: cannot assign requested address
```

Read that error carefully — it is about a **local** resource. It is not a refusal, not a timeout, and says nothing about the destination being healthy. Reading it as a downstream problem is the standard wrong turn.

```bash
ss -tan state time-wait | wc -l           # how much of the range is parked
ss -s                                     # totals by state
```

## Why a healthy service suddenly starts churning connections

470/s is generous if connections are reused, and unreachable if they are not. Keep-alive turns thousands of requests per second into a handful of connections — and it is easy to disable by accident:

- **Go** — a response body that is not read to completion cannot be reused, no matter that it is closed. An early `return` on an error path is enough. `http.Transport`'s `MaxIdleConnsPerHost` also defaults to **2**, so even correct code keeps only two idle connections per destination unless told otherwise.
- Any client where a connection-per-request wrapper is introduced beneath an unchanged API.

The pattern is that nothing about the traffic volume changes — only whether each request costs a connection — so the failure arrives without any corresponding rise in requests, which is [[problems/connect-fails-with-cannot-assign-requested-address-at-peak|how it presents]].

## Mitigations, in order of honesty

1. **Reuse connections.** Everything else buys a multiple; this changes the exponent.
2. `net.ipv4.ip_local_port_range = 1024 65535` — roughly doubles the range. Real, and finite.
3. `net.ipv4.tcp_tw_reuse = 1` — lets outgoing connections reuse a `TIME_WAIT` socket when timestamps make it safe. The default is `2`, meaning loopback only, so this is a genuine change. It applies to outbound connections only.
4. Not `tcp_tw_recycle`. It was never safe behind NAT and was removed from Linux in 4.12; guides recommending it are describing a kernel from another decade.

The same exhaustion happens one layer out, at a NAT gateway, where every host behind it shares one source IP and therefore one port range. The arithmetic is identical and the ceiling is shared — the connection-tracking table that gateway maintains has its own limits, described in [[concepts/conntrack|conntrack]].
