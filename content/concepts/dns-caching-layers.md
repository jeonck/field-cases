---
title: "DNS caching layers"
description: "A name is resolved by four or five independent caches, each with its own TTL, and only one of them respects the record's."
tags:
  - meta
---

"The DNS has updated" is never a single fact. Between an authoritative record changing and a process actually connecting to the new address there are several caches stacked on top of each other, and each one can hold the old answer for its own reasons:

| Layer | Honours record TTL? | Typical lifetime |
| --- | --- | --- |
| Authoritative / recursive resolver | yes | the TTL |
| Node resolver cache (`systemd-resolved`, `nscd`, NodeLocal DNSCache) | usually | the TTL, sometimes floored |
| **Runtime cache** (JVM `networkaddress.cache.ttl`, some HTTP clients) | **no** | 30s, or forever |
| Connection pool | not applicable | the pool's `maxLifetime` |
| Already-established TCP connections | not applicable | until the socket closes |

The bottom three do not resolve anything — they hold a socket or an address that was resolved once, and no TTL will ever expire it. This is why lowering a record's TTL before a planned migration is necessary but not sufficient: it fixes the layers that already behaved and does nothing to the ones that caused the incident.

The JVM is the usual offender because its cache is a **process-lifetime** cache when `networkaddress.cache.ttl=-1`, and a hardened `java.security` is an easy thing to inherit from a base image without noticing — see [[problems/writes-keep-failing-with-read-only-transaction-after-failover|writes that keep failing long after a failover finished]].

The test that settles it in one line, run inside the affected container:

```bash
getent hosts db-writer.example.internal   # fresh resolution, bypasses the runtime cache
```

If that disagrees with where the process is actually connecting (`ss -tnp | grep <pid>`), the problem is above the resolver, not in it.

The shape generalises well past DNS. [[concepts/image-tags-and-digests|A container image tag]] is also a mutable name with caches beneath it, and a node that pulled it earlier keeps serving the old bytes for as long as it holds them. In both cases the name was updated correctly and something that had already resolved it was never told, which is why the durable fix rhymes: refer to the content rather than to the name.
