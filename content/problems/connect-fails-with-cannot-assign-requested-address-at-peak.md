---
title: "connect() fails with \"cannot assign requested address\" at peak, and the downstream is healthy"
date: 2026-08-22
tags:
  - target/golang
  - target/linux
  - layer/capacity
  - symptom/intermittent-failure
status: solved
severity: P1
env: "Go 1.22 service on Kubernetes / one downstream service IP:port / default ephemeral port range / kernel 5.15"
symptom: "dial tcp 10.0.0.30:8080: connect: cannot assign requested address"
root_cause: "An error-path early return stopped draining response bodies, so every rate-limited response burned a TCP connection instead of reusing it. At peak the 28k ephemeral ports to that one destination were consumed by TIME_WAIT within a minute."
---

*Example note — the format applied to a well-documented failure mode, not a specific engagement.*

## Symptom

- **Since when:** the first occurrence was at the evening peak, three weeks after a retry-handling refactor. It recurred at every peak after that, worsening as the downstream's rate limiter got busier.
- **Reproduces when:** sustained load against **one** downstream, combined with that downstream returning a meaningful share of non-200 responses. Both conditions are required, which is why it took three weeks and a downstream change to appear.
- **Does not reproduce when:** the same request rate is spread across two downstream addresses, or when the downstream returns only 200s. Neither of those was obvious until after the cause was known.

```text
Post "http://pricing.example.internal:8080/v1/quote": dial tcp 10.0.0.30:8080:
connect: cannot assign requested address
```

The failure is on `connect()`, before any bytes are sent, and the errno is `EADDRNOTAVAIL`. It is not a refusal, not a timeout, and not a DNS problem — the kernel is saying it has no local address left to use. That distinction was made about forty minutes in, and everything before it was spent investigating a downstream that was fine.

```text
$ ss -tan state time-wait | wc -l
27994
$ sysctl net.ipv4.ip_local_port_range
net.ipv4.ip_local_port_range = 32768	60999
```

27,994 sockets parked in `TIME_WAIT` out of a range of 28,232.

## Environment

| | |
| --- | --- |
| Product / version | Go 1.22, `net/http` default transport, kernel 5.15 |
| Deployment | Kubernetes, pod networking, no NAT gateway on this path |
| Scale | ~1.4k requests/s to a single downstream service address at peak |
| **Last change** | **retry and error handling refactored three weeks earlier — an early `return` was added for non-200 responses. The downstream enabled a rate limiter the week of the first incident, which is when non-200s stopped being rare.** |

Two changes, three weeks and two teams apart, neither of which is a problem alone. The refactor made every non-200 expensive; the rate limiter made non-200s common.

## Investigation

| Hypothesis | How it was checked | Verdict |
| --- | --- | --- |
| The downstream is down or saturated | Its p99 and error rate were normal apart from deliberate 429s. It never dropped a connection | rejected |
| DNS resolution failure for the downstream | The error contains a resolved IP — resolution had already succeeded | rejected in one second, once the message was actually read |
| Conntrack table full on the node | `conntrack -S` and `nf_conntrack_count` well under the maximum. Plausible enough to check first, given [[problems/dns-lookups-intermittently-take-exactly-5-seconds|a previous incident on this cluster]] | rejected |
| File descriptor limit | `ulimit -n` is 65536, process holds ~9k open | rejected |
| Ephemeral port exhaustion against one 4-tuple | `ss -tan state time-wait \| wc -l` at 27,994 against a 28,232-port range | **accepted** |

The whole diagnosis is one command, and the reason it was not the first one is that `EADDRNOTAVAIL` was read as "cannot reach the address" rather than "cannot allocate an address" — see [[concepts/ephemeral-ports-and-time-wait|ephemeral ports and TIME_WAIT]]. Everything about the sentence invites the wrong reading.

## Root cause

Go's HTTP transport can only reuse a connection whose response body has been **read to completion**. Closing it is not sufficient. The refactor added this:

```go
resp, err := client.Do(req)
if err != nil {
    return err
}
defer resp.Body.Close()
if resp.StatusCode != http.StatusOK {
    return fmt.Errorf("pricing: unexpected status %d", resp.StatusCode)   // body never drained
}
```

Every non-200 therefore abandoned its connection, and the connection was closed by this side, so it entered `TIME_WAIT` and held its source port for 60 seconds. With one destination IP and port, the arithmetic is fixed: 28,232 ports over a 60-second hold is about 470 new connections per second, sustained. At peak the 429 rate alone was over 600/s.

A second factor was underneath it the whole time: `http.Transport`'s `MaxIdleConnsPerHost` defaults to **2**. Even the successful requests were reusing far fewer connections than anyone assumed, which is why the margin was thin enough for an error path to consume it.

## Fix

Drain before closing, so the connection goes back to the pool:

```go
defer func() {
    io.Copy(io.Discard, resp.Body)
    resp.Body.Close()
}()
```

Raise the per-host idle pool, which the default sizes for a browser rather than for a service calling one dependency 1.4k times a second:

```go
tr := http.DefaultTransport.(*http.Transport).Clone()
tr.MaxIdleConns = 512
tr.MaxIdleConnsPerHost = 256
tr.IdleConnTimeout = 90 * time.Second
```

Node-level headroom, applied at the same time and worth separating in the mind from the fix above — it buys a multiple, not a change of behaviour:

```bash
sysctl -w net.ipv4.ip_local_port_range="1024 65535"
sysctl -w net.ipv4.tcp_tw_reuse=1            # default is 2 = loopback only
```

`tcp_tw_recycle` does not appear here on purpose: it was never safe behind NAT and was removed from Linux in 4.12. It is still the top suggestion in older guides.

`TIME_WAIT` sockets dropped from ~28k to under 400 after the drain fix reached production, and the peak passed without a single `EADDRNOTAVAIL`.

## Prevention

- **Detection:** alert on ephemeral port range utilisation — `ss -tan state time-wait | wc -l` against `ip_local_port_range`, as a node exporter textfile metric. Nothing was watching this, and it is a resource with a hard ceiling and a computable limit, which makes it one of the easier things to alert on well.
- **Prevention:** enable the `bodyclose` linter in CI, which finds undrained and unclosed response bodies statically. This bug is visible in a five-line diff and passed review twice.
- **Remaining debt:** the fix was applied to one client in one service. The shared HTTP wrapper that would make draining and pool sizing the default exists but is used by roughly half the services, and nobody has counted which half. The sysctl changes were applied to the node pool by hand and are not in the node configuration, so they will disappear at the next image roll.

## Open questions

- The drain fix, the transport tuning, and both sysctls shipped within twenty minutes of each other. The `TIME_WAIT` count makes the drain fix the obvious primary, but nothing was rolled back to confirm it, and if the sysctls alone were sufficient the code fix would still be right and unproven.
- How many other services have the same undrained-body pattern is unknown. `bodyclose` was enabled only on this repository, and running it fleet-wide was raised and not scheduled.
- Whether the downstream's rate limiter was the trigger or merely the first trigger. Any source of non-200s at that volume produces the same outcome, so the incident may have been three weeks of luck rather than three weeks of latency, and the distinction was never examined.

## Related

Parent concept: [[concepts/ephemeral-ports-and-time-wait|ephemeral ports and TIME_WAIT]]. Filed under Capacity in the [[maps/incidents-moc|incident map]].

Sibling: [[problems/dns-lookups-intermittently-take-exactly-5-seconds|the 5-second DNS stalls]], which is where the conntrack hypothesis above came from. Both are finite kernel-side tables that are invisible until a workload crosses them, both live on the node rather than in the application, and neither appears in any application-level metric. Checking conntrack first here was reasonable and wrong, and it is recorded rather than quietly dropped for that reason.

Contrast with [[problems/p99-latency-spikes-while-cpu-utilisation-sits-at-30-percent|the CPU throttling after node consolidation]]: both are resource limits that a change elsewhere pushed the workload into, but that one was pushed by infrastructure getting *larger* and this one by an error path getting *busier*. In both cases the service's own request rate — the number everyone watches — did not move at all.
