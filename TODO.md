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
      write `<name>.meta.json` sidecars, never touch the immutable source.
      (`catalogue.py apply-rename --execute`)

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
- [x] 🟢 Fix `SKIP_NAMES` (`.idea`/`.git`/`.DS_Store` exclusion) leaking IDE
      housekeeping files into the catalogue via symlinks: a review-mirror
      symlink under `00_RESEARCH_REVIEW/by_category/` named
      `.idea __ workspace.xml` isn't itself under a `.idea` path component,
      but resolves to the real (rightly-excluded) `.idea/workspace.xml` -
      found via `catalogue.py all --dry-run --limit 30`, which also caught
      the resulting `verify` failure before it could reach the real catalogue.
      `iter_source_files()` now also checks a symlink's resolved target
      against `SKIP_NAMES`, not just its own path.

- [ ] 🟢 Decide whether `researchboss` (the downstream catalogue database project
      mentioned by the user) should consume `catalogue_master.jsonl` directly or
      via an export step.
