---
title: "The fix is deployed, the rollout succeeded, and a third of requests still hit the old bug"
date: 2026-08-22
tags:
  - target/kubernetes
  - layer/config
  - symptom/deploy-failure
  - symptom/intermittent-failure
status: solved
severity: P1
env: "Kubernetes 1.28 / 24 pods across 9 nodes / versioned image tags, default imagePullPolicy"
symptom: "kubectl get pods -o jsonpath='{..imageID}' | sort -u  →  two distinct sha256 digests under one tag"
root_cause: "A CI retry re-pushed the same image tag with different content. Nodes that had already cached that tag kept serving the old layers under imagePullPolicy IfNotPresent, so the deployment ran two builds at once while every tool reported the tag as matching."
---

*Example note — the format applied to a well-documented failure mode, not a specific engagement.*

## Symptom

- **Since when:** immediately after a hotfix deploy that reported success. The bug it fixed kept being reported by customers for the next two days.
- **Reproduces when:** repeating the same request enough times. Roughly one in three hits the old behaviour, and which pods are wrong is stable — it is not random per request, it is per pod.
- **Does not reproduce when:** curling a single pod directly, if you happen to pick a good one. Two engineers verified the fix this way and both were right about what they saw.

Nothing failed. `kubectl rollout status` was clean, the Deployment reported 24/24 available, and the image tag in the manifest was the new one on every pod. The only artefact of the problem is what the pods say they are actually running:

```text
$ kubectl get pods -l app=orders \
    -o jsonpath='{range .items[*]}{.status.containerStatuses[0].imageID}{"\n"}{end}' | sort -u
registry.example.internal/orders@sha256:1a77c0e4f2b9…
registry.example.internal/orders@sha256:9f3cbb21d5ae…
```

Two digests. One tag. Everything that reports on a deployment compares tags, so every dashboard, every `describe`, and both engineers' spot checks agreed the rollout was complete and correct.

## Environment

| | |
| --- | --- |
| Product / version | Kubernetes 1.28, containerd, 24 pods across 9 nodes |
| Deployment | image referenced by version tag, `imagePullPolicy` unset |
| Scale | 24 replicas, node pool churns slowly — nodes live for weeks |
| **Last change** | **the CI publish step was made retry-safe six weeks earlier: a failed publish is retried against the same tag rather than failing the build. The hotfix build was retried once.** |

The CI change was small, sensible, and fixed a real annoyance. It also made re-pushing a tag a routine event rather than something nobody did.

## Investigation

| Hypothesis | How it was checked | Verdict |
| --- | --- | --- |
| A cache in front of the service is serving stale responses | Bypassed the CDN and the application cache entirely. Same one-in-three split | rejected |
| The rollout did not finish; old ReplicaSet still serving | `kubectl get rs` — old ReplicaSet at 0 replicas, no pods owned by it | rejected |
| Leftover canary or traffic split | No canary object, no service mesh routing rules on this path | rejected |
| Some pods have a stale ConfigMap or env | Config is identical across pods, confirmed by hashing the mounted files | rejected |
| Pods are running different images under the same tag | Two distinct `imageID` digests across the 24 pods, and the split matches which node each pod is on | **accepted** |

Correlating the digest with the node is what turned this from a curiosity into an explanation: every pod on the four oldest nodes had the old digest, every pod on the five newer ones had the new digest — see [[concepts/image-tags-and-digests|image tags and digests]].

```bash
kubectl get pods -l app=orders -o custom-columns=\
POD:.metadata.name,NODE:.spec.nodeName,IMAGE:.status.containerStatuses[0].imageID
```

## Root cause

The hotfix build's publish step failed once and was retried, so the same tag was pushed twice with different content — the second push moved the tag to new bytes.

A versioned image tag defaults to `imagePullPolicy: IfNotPresent`. The four nodes that had pulled that tag earlier already had layers under that name and did not consult the registry again, so every pod scheduled onto them ran the pre-retry build. The five nodes that pulled after the re-push got the fixed one. The cluster ran both builds simultaneously, under one tag, and reported perfect agreement — because agreement was being measured on the tag.

`kubectl rollout restart`, which was run twice during the incident in the hope of shaking it loose, created new pods that used the same cached layers.

## Fix

Immediate — deploy by digest, which cannot be ambiguous and does not depend on any pull policy:

```bash
DIGEST=$(crane digest registry.example.internal/orders:v2.4.1)
kubectl set image deployment/orders app="registry.example.internal/orders@${DIGEST}"
```

Then confirm on the thing that actually identifies the build, not on the tag:

```bash
kubectl get pods -l app=orders \
  -o jsonpath='{range .items[*]}{.status.containerStatuses[0].imageID}{"\n"}{end}' | sort -u | wc -l
# expect exactly 1
```

The durable change is at the registry, not in the manifests:

```bash
aws ecr put-image-tag-mutability --repository-name orders --image-tag-mutability IMMUTABLE
```

Tag immutability rejects the second push outright, which is the only measure here that also protects everything that resolved the tag before anyone knew to worry. The CI retry now generates a new tag rather than reusing one, and the deploy templates reference digests.

`imagePullPolicy: Always` was proposed and not adopted as the primary fix. It is a mitigation — it costs a registry round trip on every pod start and still trusts whatever the tag points at at that moment.

## Prevention

- **Detection:** a check that every pod of a Deployment reports one distinct `imageID`, run after each rollout. It is one command, it has no false positives, and it would have failed this deploy in the first thirty seconds.
- **Prevention:** registry tag immutability, and deploy manifests that reference digests. Between them the failure is not unlikely, it is unrepresentable.
- **Remaining debt:** the application image is now pinned by digest; the **base images** in every Dockerfile are still floating tags, so builds are still not reproducible and a base image can change underneath a rebuild without any record. That is the same class of problem one layer down and nothing is scheduled for it.

## Open questions

- How long the cluster ran two builds is unknown, and unknowable retroactively — node-level image cache state is not recorded anywhere, so the only evidence was the digests of pods that happened to be alive when someone looked. If the pods had been recreated first, this would have been invisible.
- Whether the retried publish actually changed anything meaningful, or merely produced a different build ID for identical source, was never determined. The build is not reproducible, so the two digests cannot be compared beyond "they differ".
- A one-off sweep found two other Deployments currently running mixed digests. Neither has visible symptoms, both were left alone, and nobody decided whether that is acceptable or merely convenient.

## Related

Parent concept: [[concepts/image-tags-and-digests|image tags and digests]]. Filed under Config in the [[maps/incidents-moc|incident map]].

Sibling: [[problems/writes-keep-failing-with-read-only-transaction-after-failover|the JVMs that kept writing to a demoted database]]. Both are caches holding a stale copy of something that was correctly updated elsewhere, and in both the cache is invisible to the layer that did the updating — DNS repointed and the JVM did not re-resolve; the tag moved and the node did not re-pull. The general shape is worth naming: **a name was updated, and something that had already resolved that name was never told.**

Contrast with [[problems/a-40-minute-outage-passed-with-no-alert|the alerts that had been inert for six weeks]]: there the tooling reported healthy because the data it judged had stopped arriving, here it reported success because it compared the wrong identifier. Both are green signals that were never measuring what anyone believed, but only one of them can be caught by asking a better question — and the better question here is one line long.
