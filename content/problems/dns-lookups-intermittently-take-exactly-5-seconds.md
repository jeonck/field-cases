---
title: "DNS lookups inside pods intermittently take exactly 5 seconds"
date: 2026-08-22
tags:
  - target/kubernetes
  - target/coredns
  - layer/network
  - symptom/intermittent-failure
  - symptom/latency
status: solved
severity: P2
env: "Kubernetes 1.28 / kube-proxy iptables mode / CoreDNS 1.11 / ~40 nodes, ~600 pods"
symptom: "Error: getaddrinfo EAI_AGAIN api-internal.example.internal"
root_cause: "Parallel A/AAAA queries leave a pod on one socket at the same instant; both race to insert the same conntrack entry, one loses and is dropped, and the resolver waits out its 5s timeout before retrying."
---

*Example note — the format applied to a well-documented failure mode, not a specific engagement.*

## Symptom

- **Since when:** roughly a week after the node pool was scaled out; nothing was deployed on the day it started.
- **Reproduces when:** a pod issues many short-lived outbound requests. Roughly 1 in 300 name lookups stalls, always for 5.0s, never 4 or 6.
- **Does not reproduce when:** querying CoreDNS directly by service IP with `dig`, or from a pod with `hostNetwork: true`. Both resolve in single-digit milliseconds every time.

Application side:

```text
Error: getaddrinfo EAI_AGAIN api-internal.example.internal
    at GetAddrInfoReqWrap.onlookup [as oncomplete] (node:dns:107)
```

From inside the same pod, the shape is unmistakable — the answer is correct, it just arrives late:

```text
$ time getent hosts api-internal.example.internal
10.0.0.10       api-internal.example.internal

real    0m5.012s
user    0m0.001s
sys     0m0.004s
```

Five seconds is not a network delay. It is `resolv.conf`'s default `timeout:5` — the resolver was not waiting for a slow answer, it was waiting for an answer that never came.

## Environment

| | |
| --- | --- |
| Product / version | Kubernetes 1.28, CoreDNS 1.11, kube-proxy in iptables mode |
| Deployment | managed control plane, Ubuntu 22.04 nodes, kernel 5.15 |
| Scale | ~40 nodes, ~600 pods, ~2k DNS queries/s cluster-wide |
| **Last change** | **node pool scaled 12 → 40 the previous week; per-node pod density roughly tripled. No application or DNS config change.** |

The last-change row is the whole story: nothing broke, load made a latent race frequent enough to notice.

## Investigation

| Hypothesis | How it was checked | Verdict |
| --- | --- | --- |
| CoreDNS is overloaded or CPU-throttled | `coredns_dns_request_duration_seconds` p99 = 3ms; no throttling on the pods; scaling replicas changed nothing | rejected |
| Upstream resolver is timing out | Stall occurs on cluster-internal names that never leave CoreDNS | rejected |
| `ndots:5` search-domain amplification | Real, and it multiplies query volume, but it makes lookups *slower on average*, not exactly 5.0s intermittently | rejected — separate issue, fixed later |
| Packet loss on the pod network | `tcpdump` on the pod veth: the query goes out, no response ever comes back for the stalled lookup. Loss is one-directional and only for one of the two queries in a pair | accepted — but not the cause, the effect |
| Conntrack insert race on parallel A/AAAA queries | `conntrack -S` on the node shows `insert_failed` climbing in step with the stall rate; every stalled lookup is a glibc resolver sending A and AAAA from the **same source port** back-to-back | **accepted** |

The tcpdump was the turn. A dropped UDP packet with no ICMP and no counter anywhere in the CNI narrows the field to the kernel, and `conntrack -S` is the first place to look — see [[concepts/conntrack|conntrack]] for why the insert path is racy at all.

```text
$ conntrack -S | awk '{print $1, $6}' | head -4
cpu=0 insert_failed=41822
cpu=1 insert_failed=39217
cpu=2 insert_failed=40901
cpu=3 insert_failed=38455
```

## Root cause

glibc's resolver sends the A and AAAA queries for one name in parallel, over a **single** UDP socket, so both packets carry the same 5-tuple. kube-proxy DNATs both to a CoreDNS pod IP. Two packets of the same new "flow" therefore hit the conntrack insert path on two CPUs at the same instant; the kernel builds an entry for each, one insert loses and **its packet is silently dropped**. The resolver has one of its two answers and waits out `timeout:5` before retrying, at which point the entry exists and the retry succeeds instantly.

Nothing logs an error because from the kernel's point of view nothing failed — it dropped a duplicate. The application sees a five-second name lookup, or, once its own timeout is shorter than five seconds, `EAI_AGAIN`.

## Fix

Stop the two queries from sharing a socket. `single-request-reopen` makes glibc open a new socket (and thus a new source port, and thus a different 5-tuple) for the AAAA query:

```yaml
# pod spec
dnsConfig:
  options:
    - name: single-request-reopen
    - name: ndots
      value: "2"
    - name: timeout
      value: "2"
    - name: attempts
      value: "3"
```

Cluster-wide, so it does not depend on every team remembering, via the default injected into all pods:

```bash
kubectl patch deployment "$APP" --type=json -p='[{
  "op": "add",
  "path": "/spec/template/spec/dnsConfig",
  "value": {"options": [{"name": "single-request-reopen"}]}
}]'
```

The durable fix is NodeLocal DNSCache — a per-node caching resolver that pods reach on a link-local address, which takes DNS off the DNAT path entirely and upgrades cache misses to TCP upstream:

```bash
kubectl apply -f https://raw.githubusercontent.com/kubernetes/kubernetes/master/cluster/addons/dns/nodelocaldns/nodelocaldns.yaml
# then set kubelet --cluster-dns to the link-local address and roll the nodes
```

`insert_failed` stopped climbing within a minute of the first rollout; the 5s tail disappeared from the latency histogram on the same pods.

## Prevention

- **Detection:** scrape `conntrack -S` per node and alert on `insert_failed` **rate**, not absolute value — the counter is never zero and the absolute number is meaningless. A histogram of DNS resolution time with a bucket boundary at 5s makes this failure self-announcing; a p99 alone hides it, because 1-in-300 does not move p99.
- **Prevention:** NodeLocal DNSCache as the default for any cluster past a couple of dozen nodes. `ndots:2` as a cluster default cuts query volume roughly fivefold, which reduces the exposure even where the race is still possible.
- **Remaining debt:** `single-request-reopen` is a **glibc** option. Alpine/musl images ignore it silently — no error, no warning, the flag is simply not implemented. About a third of the images here are Alpine-based, so they were carried entirely by NodeLocal DNSCache. Nothing currently checks whether a new image is musl-based, so a future cluster without NodeLocal would regress for those pods only, and nobody would connect it to this note.

## Open questions

- Why exactly 5.0s and never a partial? If both A and AAAA can lose the race independently, some fraction of stalls should look different. Either the losing packet is always the second one for a reason not established, or the other case exists and was too rare to catch in the capture window.
- Whether IPv6 being disabled cluster-wide would have avoided this entirely by removing the AAAA query. Plausible, never tested, and the answer matters for the next cluster build.
- The kernel here is 5.15. The insert-race fix landed upstream in some form; which kernel version actually makes `single-request-reopen` unnecessary was never pinned down, so the workaround is still carried on a kernel that may not need it.

## Related

Parent concept: [[concepts/conntrack|conntrack]]. Listed under Network in the [[maps/incidents-moc|incident map]]. No sibling problem note yet — the linking quota in [[meta/note-linking-rules|linking rules]] is short by one until a second note lands.
