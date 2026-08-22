---
title: "df says the disk is full, du says it is half empty"
date: 2026-08-22
tags:
  - target/nginx
  - layer/storage
  - symptom/resource-exhaustion
  - symptom/silent-failure
status: solved
severity: P1
env: "Ubuntu 22.04 / nginx 1.24 on a standalone VM / 200 GB ext4 data volume / logrotate daily"
symptom: "FATAL: could not write to file \"pg_wal/xlogtemp.4127\": No space left on device"
root_cause: "logrotate deleted the rotated access logs while nginx still held their descriptors, so the blocks were never freed. The postrotate reopen signal had been dropped when the logrotate configs were consolidated three weeks earlier."
---

*Example note — the format applied to a well-documented failure mode, not a specific engagement.*

## Symptom

- **Since when:** the volume crossed 90% about three weeks after a logrotate config cleanup, and hit 100% eleven days later. Nothing was deployed on either day.
- **Reproduces when:** every log rotation adds roughly 6 GB of unreclaimable space. Deterministic, and visible within a day on any host with the merged logrotate config.
- **Does not reproduce when:** nginx is restarted for any reason. A restart silently resets the whole thing, which is why two earlier "the disk alert cleared on its own" tickets exist and were closed without a cause.

The page came from the database on the same box, not from nginx:

```text
FATAL:  could not write to file "pg_wal/xlogtemp.4127": No space left on device
```

And then the part that stalled the investigation for an hour — the two tools disagreeing by 180 GB:

```text
$ df -h /var
Filesystem      Size  Used Avail Use% Mounted on
/dev/nvme1n1    200G  190G     0 100% /var

$ du -shx /var
9.4G    /var
```

nginx itself logged nothing and served traffic normally throughout. It was the only healthy process on a host where everything else was failing to write.

## Environment

| | |
| --- | --- |
| Product / version | nginx 1.24, PostgreSQL 15 co-located, Ubuntu 22.04, ext4 |
| Deployment | single VM, legacy — not containerised, logs written to local disk |
| Scale | ~6 GB of access logs per day, 200 GB volume, 14-day retention |
| **Last change** | **per-service logrotate configs consolidated into one shared template three weeks earlier. The nginx entry's `postrotate` block did not survive the merge.** |

## Investigation

| Hypothesis | How it was checked | Verdict |
| --- | --- | --- |
| Something enormous in a directory `du` was not walking | `find /var -xdev -size +1G` — nothing over 900 MB anywhere | rejected |
| Files hidden underneath a mountpoint (written before the mount) | Bind-mounted the root elsewhere and walked it: `mount --bind / /mnt && du -shx /mnt/var` — same 9.4 G | rejected — a real cause of this exact mismatch, just not this time |
| ext4 reserved blocks | `tune2fs -l` shows 5% reserved. Accounts for 10 GB, not 180 | rejected |
| Inode exhaustion rather than block exhaustion | `df -i` shows 3% inodes used | rejected — wrong resource entirely |
| Deleted files still held open by a process | `lsof +L1` shows nginx holding two descriptors totalling 178 GB, both with link count 0 | **accepted** |

The bind-mount check is worth keeping even though it was wrong here: files written to a directory before something is mounted over it produce exactly the same `df`/`du` gap, and it is indistinguishable from this one until you look. Rejecting it cost two minutes and removed the only other plausible story.

```text
$ lsof +L1 | head -3
COMMAND   PID  USER  FD  TYPE DEVICE       SIZE/OFF NLINK NODE NAME
nginx    1184  www  12w  REG  259,1    102008881152     0  394 /var/log/nginx/access.log.1 (deleted)
nginx    1184  www  15w  REG  259,1     89217304576     0  395 /var/log/nginx/access.log.2 (deleted)
```

`NLINK 0` with an open descriptor is the whole diagnosis — see [[concepts/unlinked-open-files|unlinked open files]] for why the blocks stay allocated.

## Root cause

logrotate renamed and then removed the rotated access logs, but nginx was never told to reopen its log files. A process does not notice that its log file was renamed or unlinked; it holds a descriptor to the inode, not to the path. nginx kept appending to a file with no name, the blocks were never released, and every rotation added another orphaned inode.

The `postrotate` block that used to send the reopen signal was lost when the per-service logrotate configs were merged into a shared template. The merge was reviewed, tested by running `logrotate -d` (which reports what it *would* rotate and says nothing about descriptors), and shipped clean.

## Fix

Immediate relief without restarting nginx — truncate through the descriptor, which frees the blocks at once:

```bash
: > /proc/1184/fd/12
: > /proc/1184/fd/15
```

Then make nginx let go of them properly:

```bash
nginx -s reopen        # or: kill -USR1 $(cat /var/run/nginx.pid)
```

And restore the missing directive so the next rotation does it on its own:

```
/var/log/nginx/*.log {
    daily
    rotate 14
    missingok
    notifempty
    compress
    delaycompress
    sharedscripts
    postrotate
        [ -f /var/run/nginx.pid ] && kill -USR1 "$(cat /var/run/nginx.pid)"
    endscript
}
```

`copytruncate` is the other way to do this and it needs a deliberate decision, not a default: it removes the need for any signal, at the cost of a window between the copy and the truncate in which written lines are lost. For access logs that is usually acceptable; for an audit log it is not. This host kept the signal.

## Prevention

- **Detection:** alert on the **gap** between `df` used and `du` walked, not on either alone — the gap is meaningless when small and is the entire diagnosis when large. Cheap version, one line in the node exporter textfile collector:
  ```bash
  lsof -nP +L1 2>/dev/null | awk '{s+=$8} END {print "unlinked_open_bytes " s+0}'
  ```
- **Prevention:** any logrotate config that touches a long-running process needs either a `postrotate` signal or an explicit `copytruncate`. Neither present is the bug. A `logrotate -d` dry run does not check this and should not be trusted to.
- **Remaining debt:** the two earlier tickets where "the disk alert cleared on its own" were nginx restarts masking this, and nobody linked them at the time. There is no mechanism that would connect three occurrences of the same silent failure today — the alert clears, the ticket closes, and the counter resets.

## Open questions

- Why 178 GB across exactly two descriptors, when rotation ran daily for three weeks? Either `delaycompress` kept only two generations open, or nginx closed the older ones at some point for a reason not established. The distinction matters for the detection threshold and was never chased.
- The database was the first thing to fail, not nginx, purely because nginx already had its blocks allocated. Whether a different write-heavy co-tenant would have failed earlier and made this obvious sooner is unknown — and it is an argument against co-locating them at all that nobody has made yet.
- Whether the same shared logrotate template silently dropped `postrotate` for other services on other hosts. It was checked on this host only.

## Related

Parent concept: [[concepts/unlinked-open-files|unlinked open files]]. Filed under Storage in the [[maps/incidents-moc|incident map]].

Structurally this is the same failure as [[problems/writes-keep-failing-with-read-only-transaction-after-failover|the JVM that kept writing to a demoted database]], in a completely different subsystem: a process pinning a reference that the rest of the system has already moved past. There, an address that was no longer the writer; here, an inode that no longer has a name. In both cases every layer below the cached reference behaved correctly and made things worse by doing so, and in both cases the fastest fix on the night was to restart the process — which is also what stopped anyone from finding the cause the first two times.
