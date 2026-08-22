---
title: "\"prepared statement does not exist\" appears only under concurrency, never in staging"
date: 2026-08-22
tags:
  - target/pgbouncer
  - target/postgres
  - layer/config
  - symptom/intermittent-failure
status: solved
severity: P1
env: "PgBouncer 1.18 in transaction pooling / PostgreSQL 15 / pgjdbc 42.7 + HikariCP, ~40 service instances"
symptom: "ERROR: prepared statement \"S_3\" does not exist"
root_cause: "Transaction pooling hands each transaction a different server connection, so pgjdbc's server-side prepared statements were prepared on one connection and executed on another. PgBouncer 1.18 has no prepared-statement tracking."
---

*Example note — the format applied to a well-documented failure mode, not a specific engagement.*

## Symptom

- **Since when:** within two hours of cutting the services over to PgBouncer. Unlike most of what is written up here, the last change and the incident are the same day.
- **Reproduces when:** load. Roughly 0.4% of queries at production traffic, climbing with concurrency, and always on statements the service runs constantly — never on rare ones.
- **Does not reproduce when:** running in staging, at any concurrency staging can generate. Also never reproduces on the first few executions of a given statement, which is the tell.

```text
ERROR: prepared statement "S_3" does not exist
  SQLSTATE: 26000
```

The same deploy also produced its mirror image, less often, which is worth recording because searching for either error should find this note:

```text
ERROR: prepared statement "S_1" already exists
  SQLSTATE: 42P05
```

Retries succeeded. That made it look like a transient network problem for the first half hour, and it is why the initial mitigation attempt was to raise the retry count — which increased the error rate, because more retries meant more transactions, meant more reshuffling.

## Environment

| | |
| --- | --- |
| Product / version | PgBouncer 1.18, PostgreSQL 15.4, pgjdbc 42.7, HikariCP |
| Deployment | PgBouncer as a sidecar-less shared service, `pool_mode = transaction` |
| Scale | ~40 service instances, 800 client connections collapsed to 60 server connections |
| **Last change** | **PgBouncer introduced that morning, `pool_mode = transaction`, replacing direct HikariCP-to-Postgres connections. Nothing else changed — same driver, same queries, same schema.** |

## Investigation

| Hypothesis | How it was checked | Verdict |
| --- | --- | --- |
| An application or ORM statement-cache bug | The identical code had run on direct connections for over a year with zero occurrences of either SQLSTATE | rejected |
| PostgreSQL is restarting or dropping connections | `pg_postmaster_start_time()` unchanged, no server log entries, no connection-reset metrics | rejected |
| PgBouncer `server_lifetime` recycling connections underneath live clients | `server_lifetime` is 3600s; failures started within seconds of a fresh PgBouncer and continue steadily. Lowering it changed nothing | rejected — plausible enough that it cost twenty minutes |
| Transaction pooling is incompatible with server-side prepared statements | Reproduced deterministically: two clients, `prepareThreshold=5`, the same query executed six times each through PgBouncer. Fails on the sixth. Direct to Postgres, never fails | **accepted** |

The reproduction is the useful artefact here. Two clients and six executions is enough — it needs no load generator, and it is why the fix could be verified in a minute rather than by watching production error rates.

`prepareThreshold=5` is also the answer to "why not in staging": pgjdbc only promotes a query to a **named server-side** prepared statement after the fifth execution on a connection. Staging connections never lived long enough or ran hot enough to reach five. The environment was not a smaller production, it was a qualitatively different one.

## Root cause

Under `pool_mode = transaction`, a server connection returns to the pool at every `COMMIT`, so consecutive transactions from one client land on different server connections — see [[concepts/connection-pooling-modes|connection pooling modes]]. pgjdbc issues `PREPARE S_3` on whichever connection it happens to hold, and the later `EXECUTE S_3` arrives on a different one, which has never heard of it (`26000`). The mirror case is a server connection being handed to a second client whose own counter has already reached `S_1` (`42P05`).

Nothing was misconfigured in the sense of a wrong value. The configuration was correct and its consequences were not read.

## Fix

Immediate, driver-side — stop using server-side prepared statements:

```text
jdbc:postgresql://pgbouncer.example.internal:6432/app?prepareThreshold=0
```

That trades a little planning time per query for correctness, and it works on any PgBouncer version. Applied first, error rate to zero in one rolling restart.

The proper fix is to let PgBouncer track them, which needs **1.21 or newer**:

```ini
[pgbouncer]
pool_mode = transaction
max_prepared_statements = 200
```

With that set, PgBouncer re-prepares statements on whichever server connection a client lands on, and `prepareThreshold` can go back to its default. Done as a follow-up upgrade the same week; the driver parameter was then removed on one service and left in place on the rest, which is a loose end rather than a decision.

Verification, both times, was the two-client reproduction rather than production metrics:

```bash
# fails on the 6th execution against 1.18, passes against 1.21 + max_prepared_statements
psql "host=pgbouncer.example.internal port=6432 dbname=app" -c '\timing' -f repro.sql
```

## Prevention

- **Detection:** alert on SQLSTATE `26000` and `42P05` at any nonzero rate. Both are unambiguous and neither has a benign cause in a healthy system. The generic error-rate alert that actually fired said only "5xx up", and 0.4% did not trip it for twenty minutes.
- **Prevention:** changing `pool_mode` is a compatibility change, not a capacity tuning knob. The checklist before flipping any service to transaction pooling: server-side prepared statements, `SET` outside `SET LOCAL`, advisory locks, `LISTEN`/`NOTIFY`, `WITHOUT HOLD` cursors, temp tables. Every item is silent except the first.
- **Remaining debt:** only the item that raised an error was fixed. Nothing has audited the other services behind the same PgBouncer for the silent items on that list, and a `SET search_path` leaking between clients would not announce itself — it would look like a data bug next quarter.

## Open questions

- Does `max_prepared_statements` cover everything the ORM does, or only the named-prepare path? The reproduction covers named statements; the unnamed extended-protocol path was never exercised deliberately, so "it works now" rests on production being quiet.
- Why staging never reproduced it is attributed to `prepareThreshold`, and that explanation fits — but statement executions per connection were never actually measured in staging, so this is a consistent story rather than a verified one.
- Advisory locks are believed unused across these services on the strength of a `grep` for `pg_advisory`. A lock taken through a transaction pooler does not fail, it silently fails to lock, so a grep is thin evidence for something that would surface as corrupted data rather than an error.

## Related

Parent concept: [[concepts/connection-pooling-modes|connection pooling modes]]. Filed under Config in the [[maps/incidents-moc|incident map]] — the software behaved exactly as documented, the configuration expressed something nobody intended.

Sibling: [[problems/writes-keep-failing-with-read-only-transaction-after-failover|writes failing with "read-only transaction" after a failover]]. Both are the connection between application and database being something other than what the application believes it is — there, a cached address pointing at a demoted instance; here, a server connection that belongs to somebody else by the time the next statement arrives. In both, the database was healthy and answering correctly the entire time.

Contrast with [[problems/dns-lookups-intermittently-take-exactly-5-seconds|the 5-second DNS stall]]: that one also only appeared under load, but load made a **race** frequent enough to see. Here load did not cause anything — it merely got the driver past an execution counter that staging never reached. Both get described as "only happens in production", and the two need opposite investigations.
