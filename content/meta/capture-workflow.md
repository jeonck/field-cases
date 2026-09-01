---
title: "Capture workflow — from a log line to a published note"
description: "When a scrap becomes a document, and when it should stay in the inbox."
tags:
  - meta
---

## The promotion test

Not every scrap becomes a document. One question decides it:

> **Is there a real chance that in six months, or that someone else, will hit this again?**

- **No** → do not write it. Say why in one line and move on.
- **Unclear** → leave it in the inbox. Unclear is not a reason to write; it is a reason to wait.
- **Yes** → promote it to `content/problems/`.

## File and title

- Path: `content/problems/<english-kebab-slug>.md`
- The slug is **permanent**. Renaming it breaks wikilinks and URLs. Pick it once, in English, kebab-case.
- The title states the **symptom**, not the conclusion. Future searches start from what was seen, not from what was learned.
  - Good: `Connections hang for 30s after failover, no error logged`
  - Bad: `pgbouncer server_lifetime misconfiguration`

## Front matter

```yaml
---
title: "The symptom in one line — the words you would search for"
date: YYYY-MM-DD
tags:
  - target/...
  - layer/...
  - symptom/...
status: investigating | solved | wontfix
severity: P1 | P2 | P3
env: "product version / deployment shape / scale"
symptom: "the error message, verbatim, one line"
root_cause: "one line. leave empty while it is still unknown"
---
```

Adding a new front matter key means also adding it to `note-properties` → `includedProperties` in `quartz.config.yaml`, or it will not render.

## Sections

Start from [the template](https://github.com/jeonck/field-cases/blob/main/content/templates/problem-template.md): Symptom / Environment / Investigation / Root cause / Fix / Prevention / Open questions / Related.

- **Error messages verbatim, in a code block.** No summarizing, no translating. This is the single biggest factor in whether the note is findable later.
- Symptom carries three lines: *since when*, *how to reproduce*, *what does not reproduce it*.
- The environment table's **"last change" row is never empty**.
- Investigation keeps the **rejected hypotheses** too — hypothesis → how it was checked → rejected/accepted.
- Fix is copy-pasteable commands or config, not prose.
- Prevention splits into detection / prevention / remaining debt.
- **Open questions empty usually means avoidance, not understanding.** Write down what you still do not know.

Then: [[meta/tag-taxonomy|tag it]], [[meta/note-linking-rules|link it]], [[meta/publishing-checklist|mask it]].
