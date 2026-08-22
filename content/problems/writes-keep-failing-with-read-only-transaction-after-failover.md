---
title: "Writes keep failing with \"read-only transaction\" long after the failover finished"
date: 2026-08-22
tags:
  - target/aws-rds
  - target/postgres
  - layer/dns
  - symptom/outage
status: solved
severity: P1
env: "Aurora PostgreSQL 15 / 1 writer + 2 readers / JDBC via HikariCP, ~30 JVM pods on Kubernetes"
symptom: "ERROR: cannot execute INSERT in a read-only transaction"
root_cause: "The JVMs cached the writer endpoint's address for the process lifetime (networkaddress.cache.ttl=-1, inherited from a hardened base image), so after failover they kept connecting to the demoted instance, which now serves reads only."
---

*Example note — the format applied to a well-documented failure mode, not a specific engagement.*

## Symptom

- **Since when:** the moment an unplanned Aurora failover completed. The failover itself took 32 seconds; write failures continued for 18 minutes after that, and stopped only when the pods were restarted.
- **Reproduces when:** any failover — planned or not — of a cluster whose clients are long-lived JVMs built from the current base image. Confirmed by triggering `failover-db-cluster` in staging.
- **Does not reproduce when:** the same failover is triggered against short-lived pods, against the psql CLI, or against the Node.js services on the same cluster. Those recover within seconds without intervention.

Every write path returned the same thing, from every pod, long after the database was healthy:

```text
ERROR: cannot execute INSERT in a read-only transaction
  SQLSTATE: 25006
```

Reads were perfectly fine throughout, which is what made this so confusing on the call — the dashboards were green, the database was up, and the application was talking to it successfully.

## Environment

| | |
| --- | --- |
| Product / version | Aurora PostgreSQL 15.4, 1 writer + 2 readers |
| Deployment | ~30 JVM pods (Temurin 17) on Kubernetes, HikariCP, pgjdbc 42.7 |
| Scale | ~1.5k writes/s at peak, pool of 20 connections per pod |
| **Last change** | **base image bumped two weeks earlier (Temurin 17.0.9 → 17.0.11, hardened variant). No application, driver, or database change since.** |

The base image bump is the entire root cause and it shipped two weeks before anything broke. It changed nothing observable until the first failover — the class of change that is invisible to every test that does not include a failure.

## Investigation

| Hypothesis | How it was checked | Verdict |
| --- | --- | --- |
| The failover never completed; the cluster has no writer | RDS events show promotion completed in 32s. `SELECT pg_is_in_recovery()` against the writer endpoint from a psql pod returns `f` | rejected |
| The new writer came up stuck in read-only | Same check, plus a manual `INSERT` from psql through the writer endpoint — succeeds immediately | rejected — the database was healthy the whole time |
| HikariCP is holding pre-failover connections | Forced pool eviction via the JMX `softEvictConnections` bean. The pool rebuilt, and **the new connections failed the same way** | rejected as the cause — but it explains why nothing self-healed |
| The application is routing to a reader endpoint | JDBC URL inspected on a running pod: it is the writer endpoint, unchanged for a year | rejected |
| The client resolved the endpoint before the failover and never re-resolved | In one affected container: `getent hosts` returns the **new** writer's address, while `ss -tnp` shows the JVM connected to the **old** one | **accepted** |

That last row is the whole investigation in one line, and it took far too long to run. Fresh resolution and actual socket disagreeing inside the same container means the stale answer is held above the resolver — see [[concepts/dns-caching-layers|DNS caching layers]].

```text
$ getent hosts db-writer.example.internal
10.0.0.21       db-writer.example.internal

$ ss -tnp | grep java | awk '{print $5}' | sort -u
10.0.0.14:5432
```

`10.0.0.14` is the demoted instance. It accepts connections, authenticates, serves reads, and rejects every write — a perfectly healthy reader that nobody asked for.

## Root cause

Aurora's writer endpoint is a CNAME with a 5-second TTL; failover repoints it. The JVMs never saw the change, because the hardened base image ships a `java.security` containing:

```properties
networkaddress.cache.ttl=-1
```

`-1` means cache successful lookups for the lifetime of the process. The address was resolved once at startup and pinned until the pod died. Every recovery mechanism below that layer — pool eviction, connection validation, driver retries — dutifully rebuilt connections to the same wrong address.

## Fix

Immediate, during the incident:

```bash
kubectl rollout restart deployment -l tier=api   # new processes, new resolution
```

The actual fix, so a restart is not the recovery plan:

```bash
# JVM: cap the address cache. 30s is the modern default; anything finite works.
JAVA_TOOL_OPTIONS="-Dnetworkaddress.cache.ttl=30 -Dnetworkaddress.cache.negative.ttl=0"
```

Belt and braces at the driver, so a read-only server is refused rather than used:

```text
jdbc:postgresql://db-writer.example.internal:5432/app?targetServerType=primary
```

The durable fix is to stop relying on DNS for failover entirely — the [AWS Advanced JDBC Wrapper](https://github.com/aws/aws-advanced-jdbc-wrapper) tracks cluster topology and fails over without waiting on a name to change:

```text
jdbc:aws-wrapper:postgresql://db-writer.example.internal:5432/app?wrapperPlugins=failover
```

Staging failover after the JVM flag: writes resumed 6 seconds after promotion, with no restart.

## Prevention

- **Detection:** alert on SQLSTATE `25006` at any nonzero rate. It is never normal for a service that expects a writer, it is unambiguous, and it fires within seconds — unlike the error-rate alert that did page us, which said only that writes were failing.
- **Prevention:** quarterly failover drills in staging. This was latent for two weeks and would have been latent indefinitely; only an actual failover can find it. Pin `networkaddress.cache.ttl` explicitly in the application's own config rather than inheriting whatever the base image decides.
- **Remaining debt:** nothing in CI inspects `java.security`. The next hardened base image can reintroduce this, or something adjacent, and the only signal will be the next failover. A one-line check in the image build (`grep networkaddress.cache.ttl $JAVA_HOME/conf/security/java.security`) was proposed and not implemented.

## Open questions

- Would the pods have recovered on their own eventually? HikariCP `maxLifetime` was 30 minutes and they were restarted at 18, so this was never observed. The answer is almost certainly no — a new connection re-uses the cached address, and nothing in the JVM ever expires it — but "almost certainly" is not "measured", and it changes whether the drill should wait or restart.
- Where did `-1` actually come from? It was attributed to the hardened base image, but the upstream layer that introduced it was never identified, so it is not known whether it is deliberate hardening policy or an artefact carried from an older template.
- The Node.js and Python services on the same cluster recovered without intervention, which is consistent with neither runtime caching resolutions — but nobody verified that their HTTP clients and pools behave the same way under a failover. They were assumed fine because they did not page.

## Related

Parent concept: [[concepts/dns-caching-layers|DNS caching layers]]. Filed under DNS in the [[maps/incidents-moc|incident map]] — not under Database, even though every symptom pointed there, because the cause was a cached name.

The sibling worth reading next is [[problems/dns-lookups-intermittently-take-exactly-5-seconds|the 5-second DNS stall inside pods]]. The two were briefly conflated during this incident ("we have a DNS problem again"), and they have nothing in common: that one is a kernel-level packet drop that resolves itself on retry, this one is a userspace cache that never retries at all. Same word, opposite layer, opposite fix.
