---
title: "Unlinked open files"
description: "Deleting a file removes a name, not the data. Blocks are freed only when the last name and the last open descriptor are both gone."
tags:
  - meta
---

On a POSIX filesystem, `rm` does not delete a file. It calls `unlink()`, which removes a **name** from a directory and decrements the inode's link count. The inode — and every block it owns — is freed only when two counters reach zero:

1. the link count (how many names point at it), and
2. the open descriptor count (how many processes currently hold it open).

A file with no name and one open descriptor is a perfectly normal, fully functional file. The process holding it can keep reading and writing it forever. Nothing can open it again, because there is no longer a name to open — but it still occupies its blocks.

This is the entire explanation for `df` and `du` disagreeing:

| | asks | sees unlinked-but-open files |
| --- | --- | --- |
| `du` | walks directory entries — **names** | no |
| `df` | asks the filesystem for free **blocks** | yes |

So `du` reporting 2 GB while `df` reports 190 GB used is not a bug in either tool. They are answering different questions, and the gap between them *is* the size of what has been deleted but not closed — the shape behind [[problems/df-says-the-disk-is-full-du-says-it-is-half-empty|a disk that fills up with nothing on it]].

```bash
lsof +L1                       # every open file with a link count below 1
ls -l /proc/<pid>/fd | grep deleted
```

Two ways out, and they are not equivalent: closing or reopening the descriptor (a signal, a reload, a restart) frees the blocks properly, while truncating through the descriptor — `: > /proc/<pid>/fd/N` — reclaims the space instantly without touching the process, at the cost of discarding whatever it contained. The first is the fix; the second is what you do at 3am when the volume is at 100% and the process must not be restarted.

The same mechanic is why a temporary file created and immediately unlinked is the standard way to get private scratch space that cannot leak: it disappears the moment the process exits, however it exits.
