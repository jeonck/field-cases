# sols

Public notebook of ICT incidents — [sols.metacog.co.kr](https://sols.metacog.co.kr)

Built with [Quartz v5](https://github.com/jackyzha0/quartz). Notes live in `content/`.

## Writing a note

1. Promotion test: will this be hit again in six months, by me or someone else? If no, don't write it.
2. Copy `content/templates/problem-template.md` to `content/problems/<english-kebab-slug>.md`. The slug is permanent.
3. Title by **symptom**, tag on three axes, link three ways, add a line to `content/maps/incidents-moc.md`.
4. Mask before committing — see `content/meta/publishing-checklist.md`:

```bash
grep -rEn '(AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY|password\s*[:=]\s*\S+)' content/
```

## Local

```bash
npm ci
npx quartz build --serve
```

Pushing to `main` deploys via GitHub Actions.
