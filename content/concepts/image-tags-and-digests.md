---
title: "Image tags and digests"
description: "A tag is a mutable pointer that anyone can move. A digest is the image. Only one of them identifies what is running."
tags:
  - meta
---

A container image is identified two ways, and they are not equivalent:

| | Example | Property |
| --- | --- | --- |
| **Tag** | `registry.example.internal/orders:v2.4.1` | a mutable label in the registry — repointable at any time |
| **Digest** | `registry.example.internal/orders@sha256:9f3c…` | the content hash — a different digest is a different image, always |

A tag is a name for whatever the registry currently says it is. Re-pushing a tag is an ordinary, silent, permitted operation: the tag now points at different bytes, and nothing that already resolved it is notified.

## Where the stale copy hides

Nodes cache image layers, and the default pull policy decides whether they are consulted:

| Tag in the manifest | Default `imagePullPolicy` |
| --- | --- |
| `:latest`, or no tag | `Always` |
| anything else, e.g. `:v2.4.1` | **`IfNotPresent`** |

So a versioned tag — the disciplined-looking choice — is the one that gets cached. A node that pulled `v2.4.1` before the tag moved keeps serving the old layers to every pod scheduled onto it afterwards, indefinitely. Nodes that pull it later get the new content. The cluster then runs two different builds under one tag, split by which node happened to pull when, and every surface that reports on the deployment compares **tags** and reports agreement.

`kubectl rollout restart` does not fix it. New pods are created, the pull policy is still `IfNotPresent`, and the cached layers are still present.

The question worth asking is never "is the tag right" but "do all the pods agree on a digest":

```bash
kubectl get pods -l app=orders \
  -o jsonpath='{range .items[*]}{.status.containerStatuses[0].imageID}{"\n"}{end}' | sort -u
```

More than one line means more than one build is running — the failure behind [[problems/the-fix-is-deployed-and-a-third-of-requests-still-hit-the-old-bug|a fix that is deployed and still not in effect]].

## Making it impossible rather than unlikely

- **Deploy by digest.** `image: repo/orders@sha256:…` cannot be ambiguous, cannot be cached wrongly, and does not depend on any pull policy.
- **Registry tag immutability** (`IMMUTABLE` on ECR, equivalents elsewhere) rejects the re-push at the source. This is the only control that also protects everything that resolved the tag before you started caring.
- `imagePullPolicy: Always` is a mitigation, not a fix: it costs a registry round trip per pod start, and it still trusts whatever the tag points at right now.

Tags remain useful as human-facing labels. They are not identifiers, and treating them as identifiers is the whole bug.
