---
title: "conntrack"
description: "The kernel's connection tracking table — what makes stateless packets into stateful flows, and what NAT is built on top of."
tags:
  - meta
---

Netfilter's connection tracking table. Every flow the kernel NATs or filters statefully gets an entry keyed by the 5-tuple (protocol, source IP/port, destination IP/port). NAT rules — including everything `kube-proxy` writes in iptables mode — are applied **once**, when the entry is created; subsequent packets of the flow are rewritten by looking up the existing entry rather than re-evaluating rules.

Three consequences that keep showing up in incidents:

- **It is finite.** `nf_conntrack_max` caps it. Full table → `nf_conntrack: table full, dropping packet` and connections fail with no application-level error.
- **UDP entries are guesses.** UDP has no handshake, so the kernel invents a flow from the first packet and expires it on a timer (`nf_conntrack_udp_timeout`, 30s by default). DNS lives here.
- **Insertion is racy.** Two packets of the same "flow" NAT'd at the same instant can both build an entry; one loses the insert and its packet is dropped silently. Visible only as `conntrack -S | grep insert_failed` climbing.

The third one is why [[problems/dns-lookups-intermittently-take-exactly-5-seconds|DNS lookups inside pods stall for exactly five seconds]] — a dropped packet is not an error, it is a timeout.

```bash
conntrack -S                        # per-CPU counters: insert_failed, drop, early_drop
sysctl net.netfilter.nf_conntrack_count net.netfilter.nf_conntrack_max
```
