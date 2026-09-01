---
title: "Field Cases"
description: "Field notes on ICT incidents — symptom first, root cause last, nothing sanitized except the customer's name."
socialImage: field-cases-og.png
---

![Turn your wounds into wisdom. — Field Cases, ICT problem notes](/static/field-cases-og.png)

A public notebook of ICT failures. One document per problem, titled by the **symptom**, because that is what you will search for at 3am six months from now.

Nothing here is a tutorial. Every note is something that actually broke, what it looked like, which hypotheses were wrong, and what finally fixed it.

## Where to start

- [[maps/incidents-moc|Incident map]] — every problem note, grouped by where the root cause turned out to be.
- [[meta/capture-workflow|Capture workflow]] — how a scribbled log line becomes a document here, and when it should not.
- [[meta/tag-taxonomy|Tag taxonomy]] — three axes, two tags per axis, no exceptions.
- [[meta/note-linking-rules|Linking rules]] — why every note owes three links.
- [[meta/publishing-checklist|Publishing checklist]] — the masking pass that runs before anything is committed.

## The rules, in one paragraph

Title by symptom, not by conclusion. Paste error messages **verbatim** — no summaries, no translation. Never leave the "last change" row of the environment table empty; half of all root causes are sitting in it. Keep the hypotheses you rejected. An empty "open questions" section usually means avoidance, not understanding. Mask customer names, credentials, internal hostnames and private IPs **while writing** — git history remembers what you delete later.
