# TODO

## Cataloguing pipeline (implemented)

- [x] 🔵 Pass 1 — inventory scan of `_ResearchData`: walk `SOURCE_DATA_ROOTS`, record
      `original_filename`/`source_path`, compute hashes, detect exact duplicates,
      write `instance/catalogued_files/catalogue_master.jsonl` / `.csv` / `duplicate_report.csv`.
      (`catalogue.py scan`)
- [x] 🔵 Pass 2 — content extraction: text extraction (PDF/DOCX/CSV/MD/EML/XLSX) plus
      OCR fallback for images/screenshots (`OCR_ENABLED=true`), populate
      `content_preview` (capped at `preview_max_words`), dates, organisation/system,
      domain identifiers, research taxonomy tags. (`catalogue.py extract` + `enrich`)
- [x] 🔵 Pass 3 — rename proposal: generate `proposed_filename` per the naming
      convention, detect same-name collisions, flag likely duplicates for review
      rather than auto-deleting. (`catalogue.py rename-plan`)
- [x] 🔵 Pass 4 — approved rename: copy files into `instance/catalogued_files/`,
      never touch the immutable source. (`catalogue.py apply-rename --execute`)
      No per-file `.meta.json` sidecar - that doubled the file count in every
      folder; `catalogue_master.jsonl`/`catalog.html` at the `catalogued_files/`
      root already give per-file metadata lookup, sidecars were pure
      redundancy (removed 2026-07-16).

## Catalogue viewer

- [x] 🟣 Build `catalog.html` — static page that renders `catalogue_master.jsonl`/
      a `catalog.json` export as a browsable/searchable table. (`templates/catalog.html`,
      scaffolded next to `catalogue_master.jsonl` by `setup.py`; needs `python3 -m
      http.server` in that folder since browsers block `fetch()` of `file://` URLs)
- [x] 🟣 Add JS that polls/reloads the catalogue JSON so the page always reflects
      the latest data on disk without a manual rebuild. (re-fetches every 5s)

## Engine

- [x] 🟠 Add a `human_review_queue.csv` triage view/report. (`catalogue.py review-queue`,
      ranks records by why they need a look, not just a dump of every record)
- [x] 🟠 Add lightweight tests for `setup.py` (config validation, schema merge).
      (`tests/test_setup.py`, stdlib `unittest`, run via `python3 -m unittest
      discover tests`)
- [x] 🟠 Consider validating generated catalogue records against
      `schema.generated.json` (e.g. via `jsonschema` package) as part of Pass 2.
      (`catalogue.py validate-schema`, optional dep in `requirements.txt`, not
      part of `all`. First run found and fixed real schema/data drift: null
      `primary_entity_type`/`evidence_role`/`metadata_confidence` weren't
      permitted by the schema even though the pipeline leaves them null until
      classified; `source_organisation`/`source_system` enums didn't include
      null; `content_preview` had no hard character cap backing its
      `maxLength`, so dense/binary content could exceed it - now enforced in
      `cap_words()`.)
- [x] 🟠 Add a `summary` column (short one-line description of what the file is
      about, already AI-generated via `catalogue.py context`) to
      `rename_plan.csv`, positioned to the right of `catalogue_id`. No new AI
      calls needed - just expose the existing DB column, same pattern as
      `schema_reference`.

## Housekeeping

- [x] 🟢 Add a color-coded, live-updating `TODO.html` view (`scripts/generate_todo_html.py`
      + `scripts/colorize_todo_md.py`, same pattern as the ResearchBoss project). Items
      are colored by section since this file has no inline tag chain; the page polls
      itself over HTTP and reloads when `TODO.html` changes on disk. A project-local
      `PostToolUse` hook in `.claude/settings.local.json` reruns both scripts whenever
      Claude edits `TODO.md`, so the page and the color dots stay in sync automatically.
- [x] 🟢 Fix `iter_source_files()` scanning symlinks at all. First found as a
      narrower bug via `catalogue.py all --dry-run --limit 30`: a review-mirror
      symlink under `00_RESEARCH_REVIEW/by_category/` named
      `.idea __ workspace.xml` isn't itself under a `.idea` path component, so
      it dodged `SKIP_NAMES`, but resolved to the real (rightly-excluded)
      `.idea/workspace.xml`. Patched then to also check a symlink's resolved
      target against `SKIP_NAMES`.
      That patch turned out to be too narrow: a full from-scratch `all` run
      (2026-07-16) hit the same 00_RESEARCH_REVIEW mirror (~600 symlinks,
      each named after its target's full relative path with " __ "
      separators) at scale - `rglob()`'s traversal order isn't guaranteed, so
      for ~466 records the mirror's flattened-name symlink got scanned
      *before* the real file, permanently recording the mirror's alias as
      `original_filename` instead of the real name (caught by `verify`; also
      silently broke the deterministic embedded-title slug heuristic for
      every affected record, forcing 906/906 rename-plan slugs through the
      paid AI fallback instead of the expected ~60/40 split). Confirmed all
      623 symlinks in the source tree live under 00_RESEARCH_REVIEW and every
      target is independently reachable via its own real path, so
      `iter_source_files()` now skips symlinks outright rather than
      special-casing what they resolve to.

- [ ] 🟢 Decide whether `researchboss` (the downstream catalogue database project
      mentioned by the user) should consume `catalogue_master.jsonl` directly or
      via an export step.
