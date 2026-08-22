---
title: "Publishing checklist — mask while writing, not later"
description: "Git history remembers what you delete."
tags:
  - meta
---

This site is public by default. Redacting something after the fact leaves it in the commit history, so the masking pass happens **while writing**, never before pushing.

## Never publish

- Customer or institution names, or any combination that identifies one
- Credentials of any kind — passwords, tokens, keys, connection strings
- Internal hostnames, private IPs, internal domains, account IDs
- Raw log dumps pasted wholesale
- Contracts, rates, headcount
- Reproduction steps for an unpatched vulnerability

## Substitute, do not delete

Keep the *shape*, change the value — a note where every identifier has been scrubbed to `[REDACTED]` is unreadable.

| Original | Published |
| --- | --- |
| `prod-db-seoul-03.internal` | `db-primary.example.internal` |
| `10.42.7.118` | `10.0.0.10` |
| `AKIA…EXAMPLE` (a real key) | `AKIA<REDACTED>` |
| "Acme Retail Co." | "a domestic e-commerce operator" |

Error messages stay verbatim — only the hostnames and IDs *inside* them get substituted.

## The judgement call

When unsure, ask: **would I be fine with the customer's own engineer reading this document?** If you hesitate, it is not done. A case that cannot be generalized does not get published at all.

## Before committing

```bash
grep -rEn '(AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|password\s*[:=]\s*\S+)' content/
```

The grep catches the obvious ones. It does not catch a customer name, and it never will — that part is on you. See also [[meta/capture-workflow|capture workflow]].
