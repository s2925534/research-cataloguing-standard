# TODO

## Cataloguing pipeline (implemented)

- [x] Pass 1 — inventory scan of `_ResearchData`: walk `SOURCE_DATA_ROOTS`, record
      `original_filename`/`source_path`, compute hashes, detect exact duplicates,
      write `instance/catalogued_files/catalogue_master.jsonl` / `.csv` / `duplicate_report.csv`.
      (`catalogue.py scan`)
- [x] Pass 2 — content extraction: text extraction (PDF/DOCX/CSV/MD/EML/XLSX) plus
      OCR fallback for images/screenshots (`OCR_ENABLED=true`), populate
      `content_preview` (capped at `preview_max_words`), dates, organisation/system,
      domain identifiers, research taxonomy tags. (`catalogue.py extract` + `enrich`)
- [x] Pass 3 — rename proposal: generate `proposed_filename` per the naming
      convention, detect same-name collisions, flag likely duplicates for review
      rather than auto-deleting. (`catalogue.py rename-plan`)
- [x] Pass 4 — approved rename: copy files into `instance/catalogued_files/`,
      write `<name>.meta.json` sidecars, never touch the immutable source.
      (`catalogue.py apply-rename --execute`)

## Catalogue viewer

- [x] Build `catalog.html` — static page that renders `catalogue_master.jsonl`/
      a `catalog.json` export as a browsable/searchable table. (`templates/catalog.html`,
      scaffolded next to `catalogue_master.jsonl` by `setup.py`; needs `python3 -m
      http.server` in that folder since browsers block `fetch()` of `file://` URLs)
- [x] Add JS that polls/reloads the catalogue JSON so the page always reflects
      the latest data on disk without a manual rebuild. (re-fetches every 5s)

## Engine

- [x] Add a `human_review_queue.csv` triage view/report. (`catalogue.py review-queue`,
      ranks records by why they need a look, not just a dump of every record)
- [ ] Add lightweight tests for `setup.py` (config validation, schema merge).
- [ ] Consider validating generated catalogue records against
      `schema.generated.json` (e.g. via `jsonschema` package) as part of Pass 2.
- [x] Add a `summary` column (short one-line description of what the file is
      about, already AI-generated via `catalogue.py context`) to
      `rename_plan.csv`, positioned to the right of `catalogue_id`. No new AI
      calls needed - just expose the existing DB column, same pattern as
      `schema_reference`.

## Housekeeping

- [ ] Decide whether `researchboss` (the downstream catalogue database project
      mentioned by the user) should consume `catalogue_master.jsonl` directly or
      via an export step.
