#!/usr/bin/env python3
"""Generate a color-coded, live-updating HTML ledger from TODO.md.

Usage: generate_todo_html.py <source.md> [output.html]

Project-local fork of the global ~/.claude/skills/todo-ledger generator,
scoped to this repo so its live-reload addition doesn't affect Pedro's
other projects. Parses top-level `## ` sections as phases and `- [ ]` /
`- [x]` lines as items. Recognizes an optional leading tag chain, e.g.:
    - [x] **Done** - **Deterministic** - 🔵 Fix the parser.
    - [ ] **API** - 🟣 Add the sync route.
and degrades gracefully for plain checklists with no tags:
    - [x] Set up the repo.
This repo's TODO.md uses that plain (untagged) style with items wrapped
across multiple indented lines rather than a tag chain, so parse() also
folds wrapped continuation lines back into the preceding item's text.

The generated page polls itself over HTTP every few seconds and reloads
when TODO.html's content changes on disk (e.g. after a TODO.md edit or a
commit re-runs this script) — see the poll() function near the end of
HTML_TEMPLATE's <script>. Polling fetch() is same-origin only, so this
works when TODO.html is served over http(s) but is silently a no-op when
opened directly as a file:// page (browsers block file:// fetches).

Self-contained, dependency-free (stdlib only) so it can run from a hook on any machine.
"""
import datetime
import json
import re
import sys
from pathlib import Path

ITEM_RE = re.compile(r"^\s*-\s\[( |x|X)\]\s+(.+?)\s*$")
TAG_RE = re.compile(r"^\*\*([^*]+)\*\*\s*-\s*")

# A small, fixed hue rotation so any category vocabulary (not just
# Deterministic/AI/API) gets a stable, distinct color per project run.
CATEGORY_PALETTE = [
    ("#3E5C76", "#DCE3E9", "#8FB0D3", "#24333D"),  # steel blue
    ("#7B4B9E", "#E9DEF0", "#C79FE3", "#2C2236"),  # violet
    ("#A2601F", "#F1E3D0", "#E0A468", "#362717"),  # amber/orange
    ("#2F7A66", "#DCEEE7", "#6FC2A9", "#1B3229"),  # teal
    ("#8A4A5E", "#F0DEE3", "#E0A0B4", "#332027"),  # rose
    ("#5A6B2E", "#E7EBD6", "#B4C97E", "#2A3018"),  # olive
]


def parse(md_text: str):
    phases = []
    current = None
    current_item = None
    for line in md_text.splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            current = {"title": heading.group(1).strip(), "items": []}
            phases.append(current)
            current_item = None
            continue
        m = ITEM_RE.match(line)
        if m:
            if current is None:
                current = {"title": "Tasks", "items": []}
                phases.append(current)
            done = m.group(1).lower() == "x"
            rest = m.group(2)
            tags = []
            while True:
                tm = TAG_RE.match(rest)
                if not tm:
                    break
                tags.append(tm.group(1).strip())
                rest = rest[tm.end():]
            if tags and tags[0].lower() == "done":
                tags = tags[1:]
            category = tags[-1] if tags else None
            current_item = {"done": done, "category": category, "text": rest.strip()}
            current["items"].append(current_item)
            continue
        # Wrapped continuation line (indented, belongs to the previous item) --
        # this project's TODO.md wraps long items across lines, unlike a
        # strictly one-line-per-item checklist, so fold it back in.
        if current_item is not None and line.strip() and line[:1].isspace():
            current_item["text"] += " " + line.strip()
            continue
        if line.strip():
            current_item = None
    return [p for p in phases if p["items"]]


def title_from(md_text: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+?)\s*$", md_text, re.MULTILINE)
    return m.group(1).strip() if m else fallback


def build_html(phases, title: str, project: str, source_name: str) -> str:
    categories = []
    for p in phases:
        for i in p["items"]:
            if i["category"] and i["category"] not in categories:
                categories.append(i["category"])

    palette = {
        cat: CATEGORY_PALETTE[idx % len(CATEGORY_PALETTE)]
        for idx, cat in enumerate(categories)
    }

    def cat_class(cat):
        return "cat-" + re.sub(r"[^a-z0-9]+", "-", cat.lower()).strip("-") if cat else ""

    cat_css_light = "\n".join(
        f"  .{cat_class(c)} {{ --tag: {fg}; --tag-soft: {soft}; }}"
        for c, (fg, soft, _, _) in palette.items()
    )
    cat_css_dark_media = "\n".join(
        f"    .{cat_class(c)} {{ --tag: {dfg}; --tag-soft: {dsoft}; }}"
        for c, (_, _, dfg, dsoft) in palette.items()
    )
    cat_css_dark_scoped = "\n".join(
        f'  :root[data-theme="dark"] .{cat_class(c)} {{ --tag: {dfg}; --tag-soft: {dsoft}; }}'
        for c, (_, _, dfg, dsoft) in palette.items()
    )
    cat_css_light_scoped = "\n".join(
        f'  :root[data-theme="light"] .{cat_class(c)} {{ --tag: {fg}; --tag-soft: {soft}; }}'
        for c, (fg, soft, _, _) in palette.items()
    )

    data = json.dumps(phases).replace("</script", "<\\/script")
    cats_json = json.dumps({c: cat_class(c) for c in categories}).replace("</script", "<\\/script")
    generated = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    has_categories = "true" if categories else "false"

    template = HTML_TEMPLATE
    template = template.replace("__TITLE__", escape_html(title))
    template = template.replace("__PROJECT__", escape_html(project))
    template = template.replace("__SOURCE__", escape_html(source_name))
    template = template.replace("__GENERATED__", escape_html(generated))
    template = template.replace("__CAT_CSS_LIGHT__", cat_css_light)
    template = template.replace("__CAT_CSS_DARK_MEDIA__", cat_css_dark_media)
    template = template.replace("__CAT_CSS_DARK_SCOPED__", cat_css_dark_scoped)
    template = template.replace("__CAT_CSS_LIGHT_SCOPED__", cat_css_light_scoped)
    template = template.replace("__DATA__", data)
    template = template.replace("__CATS__", cats_json)
    template = template.replace("__HAS_CATS__", has_categories)
    return template


def escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


HTML_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>__TITLE__</title>
<style>
  :root {
    --bg: #EEF0EA; --surface: #FFFFFF; --surface-alt: #E4E7DE;
    --text: #1C2420; --text-muted: #5B6660; --border: #D3D8CC;
    --accent: #3E5C76; --done: #2F7A52; --pending: #A9812F;
    --shadow: 0 1px 2px rgba(28,36,32,.06), 0 6px 20px -8px rgba(28,36,32,.18);
    color-scheme: light;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #12181A; --surface: #1B2326; --surface-alt: #212B2E;
      --text: #E7ECE7; --text-muted: #93A29B; --border: #2E3A3C;
      --accent: #8FB0D3; --done: #5FBE8A; --pending: #D8B168;
      --shadow: 0 1px 2px rgba(0,0,0,.3), 0 10px 30px -10px rgba(0,0,0,.5);
      color-scheme: dark;
    }
  }
  :root[data-theme="dark"] {
    --bg: #12181A; --surface: #1B2326; --surface-alt: #212B2E;
    --text: #E7ECE7; --text-muted: #93A29B; --border: #2E3A3C;
    --accent: #8FB0D3; --done: #5FBE8A; --pending: #D8B168;
    color-scheme: dark;
  }
  :root[data-theme="light"] {
    --bg: #EEF0EA; --surface: #FFFFFF; --surface-alt: #E4E7DE;
    --text: #1C2420; --text-muted: #5B6660; --border: #D3D8CC;
    --accent: #3E5C76; --done: #2F7A52; --pending: #A9812F;
    color-scheme: light;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    line-height: 1.5; -webkit-font-smoothing: antialiased;
  }
  .serif { font-family: "Iowan Old Style", "Palatino Linotype", Palatino, "URW Palladio L", Georgia, serif; }
  .mono, .eyebrow, .item-mark, .item-tag, .phase-progress, .toc-count, .ring-pct,
  .summary-headline, .cat-pill, .visible-count, .chip-filter, footer, .search-wrap input {
    font-family: ui-monospace, "SF Mono", "Cascadia Code", "Consolas", "Roboto Mono", monospace;
  }
  .page { max-width: 900px; margin: 0 auto; padding: 2.5rem 1.5rem 5rem; display: flex; flex-direction: column; gap: 2rem; }
  .masthead { display: flex; flex-direction: column; gap: .4rem; }
  .eyebrow { font-size: .72rem; letter-spacing: .14em; color: var(--accent); text-transform: uppercase; }
  .masthead h1 { font-size: clamp(1.8rem, 4vw, 2.5rem); margin: 0; text-wrap: balance; font-weight: 600; letter-spacing: -.01em; }
  .dek { margin: 0; color: var(--text-muted); max-width: 60ch; font-size: 1rem; }
  .summary {
    background: var(--surface); border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow);
    padding: 1.5rem; display: grid; grid-template-columns: auto 1fr; gap: 1.75rem; align-items: center;
  }
  .ring {
    --pct: 0; width: 108px; height: 108px; border-radius: 50%;
    background: conic-gradient(var(--done) calc(var(--pct) * 1%), var(--surface-alt) 0);
    display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  }
  .ring-inner { width: 80px; height: 80px; border-radius: 50%; background: var(--surface); display: flex; flex-direction: column; align-items: center; justify-content: center; }
  .ring-pct { font-size: 1.3rem; font-weight: 600; font-variant-numeric: tabular-nums; }
  .ring-pct-label { font-size: .62rem; color: var(--text-muted); letter-spacing: .06em; text-transform: uppercase; }
  .summary-stats { display: flex; flex-direction: column; gap: .9rem; min-width: 0; }
  .summary-headline { font-size: 1.05rem; font-variant-numeric: tabular-nums; }
  .summary-headline strong { color: var(--done); }
  .cat-breakdown { display: flex; flex-wrap: wrap; gap: .6rem; }
  .cat-pill { display: inline-flex; align-items: center; gap: .4rem; font-size: .74rem; padding: .28rem .6rem; border-radius: 999px; font-variant-numeric: tabular-nums; background: var(--tag-soft); color: var(--tag); }
__CAT_CSS_LIGHT__
  @media (prefers-color-scheme: dark) {
__CAT_CSS_DARK_MEDIA__
  }
__CAT_CSS_DARK_SCOPED__
__CAT_CSS_LIGHT_SCOPED__
  .toc { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow); padding: .5rem; }
  .toc-row { display: grid; grid-template-columns: 1fr auto auto; gap: .75rem; align-items: center; padding: .55rem .85rem; border-radius: 8px; text-decoration: none; color: var(--text); }
  .toc-row:hover { background: var(--surface-alt); }
  .toc-title { font-family: inherit; font-size: .88rem; }
  .toc-bar-track { width: 90px; height: 6px; border-radius: 999px; background: var(--surface-alt); overflow: hidden; }
  .toc-bar-fill { height: 100%; background: var(--done); border-radius: 999px; }
  .toc-count { font-size: .78rem; color: var(--text-muted); font-variant-numeric: tabular-nums; min-width: 3.6em; text-align: right; }
  .controls { position: sticky; top: .75rem; z-index: 5; background: var(--surface); border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow); padding: .85rem 1rem; display: flex; flex-wrap: wrap; gap: .75rem; align-items: center; }
  .segmented { display: inline-flex; background: var(--surface-alt); border-radius: 10px; padding: 3px; gap: 2px; }
  .segmented button { border: none; background: transparent; color: var(--text-muted); font: inherit; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; font-size: .82rem; padding: .4rem .75rem; border-radius: 8px; cursor: pointer; }
  .segmented button.active { background: var(--surface); color: var(--text); box-shadow: 0 1px 3px rgba(0,0,0,.12); font-weight: 600; }
  .chip-filter { display: inline-flex; align-items: center; gap: .35rem; font-size: .76rem; padding: .38rem .7rem; border-radius: 999px; border: 1px solid var(--border); background: var(--surface); color: var(--text-muted); cursor: pointer; }
  .chip-filter.on { background: var(--tag-soft); color: var(--tag); border-color: transparent; }
  .search-wrap { flex: 1 1 180px; min-width: 140px; }
  .search-wrap input { width: 100%; font-size: .85rem; padding: .5rem .7rem; border-radius: 8px; border: 1px solid var(--border); background: var(--bg); color: var(--text); }
  .search-wrap input:focus-visible, .segmented button:focus-visible, .chip-filter:focus-visible, .toc-row:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .visible-count { font-size: .76rem; color: var(--text-muted); margin-left: auto; white-space: nowrap; }
  #phases { display: flex; flex-direction: column; gap: 1.5rem; }
  .phase { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow); padding: 1.35rem 1.35rem .5rem; scroll-margin-top: 5.5rem; }
  .phase-head { display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; margin-bottom: .9rem; }
  .phase-head h2 { font-size: 1.2rem; margin: 0; font-weight: 600; text-wrap: balance; }
  .phase-progress { font-size: .8rem; color: var(--text-muted); white-space: nowrap; font-variant-numeric: tabular-nums; }
  .phase-bar-track { height: 5px; border-radius: 999px; background: var(--surface-alt); overflow: hidden; margin-bottom: .9rem; }
  .phase-bar-fill { height: 100%; background: var(--done); }
  .item { display: grid; grid-template-columns: 1.4rem 1fr auto; gap: .7rem; align-items: start; padding: .6rem .4rem; border-top: 1px solid var(--border); border-left: 3px solid transparent; }
  .item:first-of-type { border-top: none; }
  .item.done { border-left-color: var(--done); }
  .item.pending { border-left-color: var(--pending); }
  .item.hidden { display: none; }
  .item-mark { font-size: .95rem; line-height: 1.6; text-align: center; }
  .item.done .item-mark { color: var(--done); }
  .item.pending .item-mark { color: var(--pending); }
  .item-text { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; font-size: .92rem; }
  .item.done .item-text { color: var(--text-muted); }
  .item-tag { font-size: .68rem; letter-spacing: .03em; padding: .2rem .5rem; border-radius: 999px; white-space: nowrap; text-transform: uppercase; align-self: center; background: var(--tag-soft); color: var(--tag); }
  .phase.all-hidden { display: none; }
  footer { text-align: center; color: var(--text-muted); font-size: .78rem; }
  @media (max-width: 560px) {
    .summary { grid-template-columns: 1fr; text-align: left; justify-items: start; }
    .item { grid-template-columns: 1.2rem 1fr; }
    .item-tag { grid-column: 2; justify-self: start; margin-top: .2rem; }
    .toc-row { grid-template-columns: 1fr auto; }
    .toc-bar-track { display: none; }
  }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
</head>
<body>
<div class="page">
  <header class="masthead">
    <div class="eyebrow">__PROJECT__ &middot; __SOURCE__</div>
    <h1 class="serif">__TITLE__</h1>
    <p class="dek">Every task from the checklist, sorted by phase, with what's shipped and what's still open at a glance.</p>
  </header>

  <section class="summary" id="summary-section">
    <div class="ring" id="ring">
      <div class="ring-inner"><span class="ring-pct" id="ring-pct">0%</span><span class="ring-pct-label">done</span></div>
    </div>
    <div class="summary-stats">
      <div class="summary-headline" id="summary-headline"></div>
      <div class="cat-breakdown" id="cat-breakdown"></div>
    </div>
  </section>

  <nav class="toc" id="toc"></nav>

  <section class="controls">
    <div class="segmented" id="status-filter" role="group" aria-label="Filter by status">
      <button data-status="all" class="active">All</button>
      <button data-status="pending">Remaining</button>
      <button data-status="done">Done</button>
    </div>
    <div id="cat-filters"></div>
    <div class="search-wrap"><input type="text" id="search" placeholder="Filter tasks..." aria-label="Filter tasks by keyword" /></div>
    <span class="visible-count" id="visible-count"></span>
  </section>

  <main id="phases"></main>

  <footer>generated __GENERATED__ from __SOURCE__ &middot; <span id="live-status">auto-refreshes when __SOURCE__ changes (needs http, not file://)</span></footer>
</div>

<script>
  const DATA = __DATA__;
  const CATS = __CATS__;
  const HAS_CATS = __HAS_CATS__;

  const phasesEl = document.getElementById('phases');
  const tocEl = document.getElementById('toc');
  const catBreakdownEl = document.getElementById('cat-breakdown');
  const catFiltersEl = document.getElementById('cat-filters');
  const summaryHeadlineEl = document.getElementById('summary-headline');
  const ringEl = document.getElementById('ring');
  const ringPctEl = document.getElementById('ring-pct');
  const visibleCountEl = document.getElementById('visible-count');
  const searchInput = document.getElementById('search');
  const statusFilterEl = document.getElementById('status-filter');

  const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');
  const catClass = (c) => CATS[c] || '';

  let state = { status: 'all', cats: new Set(), q: '' };

  function computeTotals() {
    let total = 0, done = 0;
    const byCat = {};
    Object.keys(CATS).forEach(c => byCat[c] = { total: 0, done: 0 });
    DATA.forEach(p => p.items.forEach(i => {
      total++;
      if (i.done) done++;
      if (i.category) {
        byCat[i.category].total++;
        if (i.done) byCat[i.category].done++;
      }
    }));
    return { total, done, byCat };
  }

  function renderSummary() {
    const { total, done, byCat } = computeTotals();
    const pct = total ? Math.round((done / total) * 100) : 0;
    ringEl.style.setProperty('--pct', pct);
    ringPctEl.textContent = pct + '%';
    summaryHeadlineEl.innerHTML = `<strong>${done}</strong> / ${total} tasks complete`;
    if (HAS_CATS) {
      catBreakdownEl.innerHTML = Object.entries(byCat).map(([cat, v]) =>
        `<span class="cat-pill ${catClass(cat)}">${cat} ${v.done}/${v.total}</span>`
      ).join('');
      catFiltersEl.innerHTML = Object.keys(CATS).map(cat =>
        `<button class="chip-filter ${catClass(cat)}" data-cat="${cat}" type="button">${cat}</button>`
      ).join('');
      catFiltersEl.querySelectorAll('.chip-filter').forEach(chip => {
        chip.addEventListener('click', () => {
          const cat = chip.dataset.cat;
          if (state.cats.has(cat)) { state.cats.delete(cat); chip.classList.remove('on'); }
          else { state.cats.add(cat); chip.classList.add('on'); }
          applyFilters();
        });
      });
    }
  }

  function renderTOC() {
    tocEl.innerHTML = DATA.map(p => {
      const done = p.items.filter(i => i.done).length;
      const total = p.items.length;
      const pct = total ? Math.round((done / total) * 100) : 0;
      return `<a class="toc-row" href="#${slug(p.title)}">
        <span class="toc-title">${p.title}</span>
        <span class="toc-bar-track"><span class="toc-bar-fill" style="width:${pct}%"></span></span>
        <span class="toc-count">${done}/${total}</span>
      </a>`;
    }).join('');
  }

  function renderPhases() {
    phasesEl.innerHTML = DATA.map((p, pi) => {
      const done = p.items.filter(i => i.done).length;
      const total = p.items.length;
      const pct = total ? Math.round((done / total) * 100) : 0;
      const rows = p.items.map((item, ii) => {
        const statusClass = item.done ? 'done' : 'pending';
        const mark = item.done ? '✓' : '○';
        const tag = item.category ? `<span class="item-tag ${catClass(item.category)}">${item.category}</span>` : '<span></span>';
        return `<div class="item ${statusClass}" data-phase="${pi}" data-item="${ii}" data-cat="${item.category || ''}" data-text="${item.text.toLowerCase().replace(/"/g, '&quot;')}">
          <span class="item-mark" aria-hidden="true">${mark}</span>
          <span class="item-text">${item.text}</span>
          ${tag}
        </div>`;
      }).join('');
      return `<section class="phase" id="${slug(p.title)}" data-phase="${pi}">
        <div class="phase-head"><h2 class="serif">${p.title}</h2><span class="phase-progress">${done}/${total}</span></div>
        <div class="phase-bar-track"><span class="phase-bar-fill" style="width:${pct}%"></span></div>
        ${rows}
      </section>`;
    }).join('');
  }

  function applyFilters() {
    const items = Array.from(document.querySelectorAll('.item'));
    let visible = 0;
    items.forEach(el => {
      const isDone = el.classList.contains('done');
      const cat = el.dataset.cat;
      const text = el.dataset.text;
      let show = true;
      if (state.status === 'done' && !isDone) show = false;
      if (state.status === 'pending' && isDone) show = false;
      if (state.cats.size > 0 && !state.cats.has(cat)) show = false;
      if (state.q && !text.includes(state.q)) show = false;
      el.classList.toggle('hidden', !show);
      if (show) visible++;
    });
    document.querySelectorAll('.phase').forEach(section => {
      const anyVisible = section.querySelectorAll('.item:not(.hidden)').length > 0;
      section.classList.toggle('all-hidden', !anyVisible);
    });
    const { total } = computeTotals();
    visibleCountEl.textContent = `showing ${visible} / ${total}`;
  }

  statusFilterEl.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-status]');
    if (!btn) return;
    state.status = btn.dataset.status;
    statusFilterEl.querySelectorAll('button').forEach(b => b.classList.toggle('active', b === btn));
    applyFilters();
  });

  searchInput.addEventListener('input', () => {
    state.q = searchInput.value.trim().toLowerCase();
    applyFilters();
  });

  renderSummary();
  renderTOC();
  renderPhases();
  applyFilters();

  // Live reload: poll this same page over HTTP and reload when its content
  // changes on disk (e.g. TODO.md was edited and this file got regenerated).
  // No-ops quietly if fetch is blocked, which happens when opened as file://.
  (function () {
    const liveStatusEl = document.getElementById('live-status');
    const selfUrl = location.href.split('#')[0];
    let lastText = null;
    let broken = false;

    async function poll() {
      if (broken) return;
      try {
        const sep = selfUrl.includes('?') ? '&' : '?';
        const res = await fetch(selfUrl + sep + '_ts=' + Date.now(), { cache: 'no-store' });
        if (!res.ok) return;
        const text = await res.text();
        if (lastText === null) {
          lastText = text;
          if (liveStatusEl) liveStatusEl.textContent = 'watching ' + '__SOURCE__' + ' for changes';
          return;
        }
        if (text !== lastText) location.reload();
      } catch (e) {
        broken = true;
        if (liveStatusEl) liveStatusEl.textContent = 'open over http(s) to enable auto-refresh';
      }
    }

    poll();
    setInterval(poll, 3000);
  })();
</script>
</body>
</html>
"""


def main():
    if len(sys.argv) < 2:
        src = Path("TODO.md")
    else:
        src = Path(sys.argv[1])

    if not src.exists():
        print(f"todo-ledger: source file not found: {src}", file=sys.stderr)
        return 0  # non-fatal: hooks shouldn't block on a missing/renamed file

    md_text = src.read_text(encoding="utf-8")
    phases = parse(md_text)
    if not phases:
        print(f"todo-ledger: no checklist items found in {src}, skipping", file=sys.stderr)
        return 0

    title = title_from(md_text, src.stem)
    project = src.resolve().parent.name
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".html")

    html = build_html(phases, title, project, src.name)
    out.write_text(html, encoding="utf-8")
    print(f"todo-ledger: wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
