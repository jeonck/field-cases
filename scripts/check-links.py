#!/usr/bin/env python3
"""Check that wikilinks in content/ survive into the built site's backlinks.

Run AFTER `npx quartz build`. Exits 1 if:
  - a wikilink target does not exist                      (dangling)
  - a page has an inbound wikilink the built site omits   (missing backlink)
  - raw `[[...]]` syntax leaked into rendered HTML        (broken render)

Typical cause of the last two: a `[[a|b]]` wikilink inside a markdown table
cell. The `|` is read as a column separator. Write it as `[[a\\|b]]`.
"""
import html
import os
import re
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTENT = os.path.join(ROOT, "content")
PUBLIC = os.path.join(ROOT, "public")
IGNORED = {"templates", "private", ".obsidian"}  # mirrors ignorePatterns

WIKILINK = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]*)?(?:\|[^\]]*)?\]\]")
BACKLINKS = re.compile(r'<div class="backlinks"><h3>Backlinks</h3>(.*?)</ul></div>', re.S)
HREF = re.compile(r'<li><a href="([^"]+)" class="internal')


def strip_code(text):
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    return re.sub(r"`[^`\n]*`", "", text)


def slugs():
    out = []
    for dirpath, dirnames, files in os.walk(CONTENT):
        dirnames[:] = [d for d in dirnames if d not in IGNORED]
        for fn in files:
            if fn.endswith(".md"):
                out.append(os.path.relpath(os.path.join(dirpath, fn), CONTENT)[:-3])
    return sorted(out)


def resolve(target, known, by_name):
    """Full path, or a unique bare filename (Quartz's `shortest` resolution)."""
    target = target.strip().rstrip("\\")  # trailing \ is the table-cell escape
    if target in known:
        return target
    hits = by_name.get(target.split("/")[-1], [])
    return hits[0] if len(hits) == 1 else None


def rendered_inbound(slug):
    path = os.path.join(PUBLIC, slug + ".html")
    if not os.path.exists(path):
        return None
    block = BACKLINKS.search(open(path, encoding="utf-8").read())
    if not block:
        return set()
    found = set()
    for href in HREF.findall(block.group(1)):
        href = html.unescape(href).split("#")[0]
        href = re.sub(r"^(\.\./|\./)+", "", href).strip("/")
        found.add(href or "index")
    return found


def main():
    if not os.path.isdir(PUBLIC):
        print("public/ not found — run `npx quartz build` first", file=sys.stderr)
        return 2

    known = slugs()
    kset = set(known)
    by_name = defaultdict(list)
    for s in known:
        by_name[s.split("/")[-1]].append(s)

    expected, dangling = defaultdict(set), []
    for src in known:
        body = strip_code(open(os.path.join(CONTENT, src + ".md"), encoding="utf-8").read())
        for raw in {m.group(1) for m in WIKILINK.finditer(body)}:
            dst = resolve(raw, kset, by_name)
            if dst is None:
                dangling.append((src, raw.strip()))
            elif dst != src:
                expected[dst].add(src)

    missing, extra, no_html = [], [], []
    for slug in known:
        got = rendered_inbound(slug)
        if got is None:
            no_html.append(slug)
            continue
        for src in sorted(expected.get(slug, set()) - got):
            missing.append((slug, src))
        for src in sorted(got - expected.get(slug, set())):
            extra.append((slug, src))

    leaked = []
    for slug in known:
        p = os.path.join(PUBLIC, slug + ".html")
        if os.path.exists(p):
            page = open(p, encoding="utf-8").read()
            # `[[x|y]]` inside <code>/<pre> is documentation of the syntax, not a failure
            page = re.sub(r"<(code|pre)\b.*?</\1>", "", page, flags=re.S)
            if "[[" in page:
                leaked.append(slug)

    failed = False
    if dangling:
        failed = True
        print(f"DANGLING wikilinks ({len(dangling)}): target does not exist")
        for src, t in dangling:
            print(f"  {src}  ->  [[{t}]]")
    if missing:
        failed = True
        print(f"MISSING backlinks ({len(missing)}): linked in source, absent from built site")
        for slug, src in missing:
            print(f"  {slug}  should list  {src}")
    if leaked:
        failed = True
        print(f"RAW [[ in rendered HTML ({len(leaked)}): wikilink did not render")
        for slug in leaked:
            print(f"  {slug}")
        print("  hint: a [[a|b]] inside a markdown table cell needs the pipe escaped: [[a\\|b]]")
    if no_html:
        failed = True
        print(f"NO HTML emitted for {len(no_html)} page(s): {', '.join(no_html)}")
    if extra:  # informational: e.g. markdown links, which this check does not parse
        print(f"note: {len(extra)} backlink(s) rendered that no wikilink explains (not a failure)")

    n = sum(len(v) for v in expected.values())
    print(f"{'FAIL' if failed else 'ok'}: {len(known)} pages, {n} inbound wikilinks checked")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
