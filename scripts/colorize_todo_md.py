#!/usr/bin/env python3
"""Normalize color-coded emoji markers on TODO.md checklist items.

Inserts a colored circle emoji right after the checkbox of each checklist
item, colored by category. Category comes from an explicit bold tag chain
if the item has one (`- [x] **Done** - **API** - text`, ResearchBoss-style),
else falls back to the item's enclosing `## ` section heading -- this
project's TODO.md groups items by section rather than inline tags, so
section becomes the color dimension:
    - [ ] 🟢 Pass 3 — rename proposal: generate `proposed_filename`...
    - [x] 🔵 Pass 1 — inventory scan of `_ResearchData`...

The emoji is assigned by first-appearance order of each category, the same
rule scripts/generate_todo_html.py uses for its HTML palette, so the two
stay visually consistent. Idempotent: re-running strips any existing marker
and re-derives it, so it's safe to run on every edit/commit.
"""
import re
import sys
from pathlib import Path

ITEM_RE = re.compile(r"^(\s*-\s\[[ xX]\]\s)(.+?)(\s*)$")
HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
TAG_RE = re.compile(r"^\*\*([^*]+)\*\*\s*-\s*")
EMOJI_RE = re.compile(r"^(?:\U0001F534|\U0001F7E0|\U0001F7E1|\U0001F7E2|\U0001F535|\U0001F7E3|\U0001F7E4|⚫|⚪)\s+")

# Mirrors CATEGORY_PALETTE's hue rotation in generate_todo_html.py
# (steel blue, violet, amber, teal, rose, olive).
EMOJI_PALETTE = ["\U0001F535", "\U0001F7E3", "\U0001F7E0", "\U0001F7E2", "\U0001F534", "\U0001F7E4"]


def split_tags(rest: str):
    tags = []
    while True:
        m = TAG_RE.match(rest)
        if not m:
            break
        tags.append(m.group(1).strip())
        rest = rest[m.end():]
    return tags, rest


def category_of(tags, section):
    cat_tags = tags[1:] if tags and tags[0].lower() == "done" else tags
    if cat_tags:
        return cat_tags[-1]
    return section


def colorize(md_text: str) -> str:
    lines = md_text.splitlines()

    categories = []
    section = None
    for line in lines:
        h = HEADING_RE.match(line)
        if h:
            section = h.group(1).strip()
            continue
        m = ITEM_RE.match(line)
        if not m:
            continue
        tags, _ = split_tags(m.group(2))
        cat = category_of(tags, section)
        if cat and cat not in categories:
            categories.append(cat)
    emoji_for = {c: EMOJI_PALETTE[i % len(EMOJI_PALETTE)] for i, c in enumerate(categories)}

    out = []
    section = None
    for line in lines:
        h = HEADING_RE.match(line)
        if h:
            section = h.group(1).strip()
            out.append(line)
            continue
        m = ITEM_RE.match(line)
        if not m:
            out.append(line)
            continue
        prefix, body, trail = m.groups()
        tags, rest = split_tags(body)
        rest = EMOJI_RE.sub("", rest)
        emoji = emoji_for.get(category_of(tags, section))
        tag_chain = "".join(f"**{t}** - " for t in tags)
        new_body = tag_chain + (f"{emoji} " if emoji else "") + rest
        out.append(f"{prefix}{new_body}{trail}")

    text = "\n".join(out)
    return text + "\n" if md_text.endswith("\n") else text


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("TODO.md")
    if not path.exists():
        print(f"colorize-todo: source file not found: {path}", file=sys.stderr)
        return 0

    text = path.read_text(encoding="utf-8")
    new_text = colorize(text)
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
        print(f"colorize-todo: updated {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
