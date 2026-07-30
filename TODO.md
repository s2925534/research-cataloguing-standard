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
- [x] 🔵 Pass 4 — approved rename: copy files into
      `instance/catalogued_files/documents/`, never touch the immutable
      source. (`catalogue.py apply-rename --execute`)
      No per-file `.meta.json` sidecar - that doubled the file count in every
      folder; `catalogue_master.jsonl`/`catalog.html` at the `catalogued_files/`
      root already give per-file metadata lookup, sidecars were pure
      redundancy (removed 2026-07-16).
      Copies land in `documents/`, not directly in `catalogued_files/` - that
      mixed the actual research files in with the pipeline's own tool/report
      output (`catalog.html`, `catalogue_master.*`, `*_report.csv`) sitting in
      the same listing. Now `catalogued_files/` is "the tools" and
      `catalogued_files/documents/` is "the research files", cleanly separated
      (fixed 2026-07-17).

## Document review tool

- [x] 🟣 Build `review.py` — standalone, project-agnostic sentence/block-level
      diff-review tool for any two text/Markdown files, so proposed edits (e.g.
      AI-suggested changes to a working document) can be accepted,
      rejected, or manually rewritten per change instead of blindly applied.
      Shows original-vs-suggested per change (not a unified +/- diff); changed
      paragraphs are split sentence-by-sentence, tables/lists/code/headings
      stay atomic. Undecided changes default to keeping the original. stdlib-only
      `http.server` subclass serves `review_ui/` and writes a timestamped
      `*.review-backup-*` copy of the target file before every save.
      (`python3 review.py <original.md> <proposed.md>`; tests in
      `tests/test_review.py`)
      Every block also gets a hover-revealed "Edit block" toggle, not just
      the ones the diff flagged - a manual free-text override wins over
      whatever the diff/decision logic would otherwise produce, so review
      isn't limited to only the proposed changes (added 2026-07-20, after
      trying the tool against a real working document surfaced the gap:
      untouched text was read-only).
      Context blocks now render as real HTML (headings/tables/lists/
      figures/captions, color-coded Added/Removed/Changed badges) instead
      of a raw Markdown/HTML text dump, since trying it against a real
      document made that dump unreadable at scale. Fixed a table-header
      contrast bug caused by `position: sticky` inside an `overflow-x:
      auto` container producing an overlapping dark box over the header
      text. Existing `<img>` paths are now actually served (via
      `--asset-root`, previously nothing served them so every image
      404'd) and images/tables/paragraphs can be inserted anywhere via a
      per-block "+ Insert" control (uploads go to `--media-dir`), plus
      dedicated "Replace image"/"Edit caption" controls on figure blocks
      (added 2026-07-20).
      Added a clearer accepted/rejected indicator per change: a status
      pill (Pending / Kept original / Accepted / Custom edit), the
      non-chosen pane dims once decided, and the inline underline on a
      changed sentence within its paragraph recolors to match (dashed
      accent while pending, red/green/blue once decided) - previously
      every card looked the same regardless of whether it had been
      decided, only the button highlight differed. Verified via a raw
      stdlib WebSocket/CDP client driving headless Chrome (no
      puppeteer/playwright available) to actually click through
      Keep/Accept/Edit-manually and screenshot each resulting state
      (added 2026-07-20).
      Added a light/dark/system theme toggle to the toolbar: a
      localStorage-backed preference (`review-theme`), a synchronous
      `<head>` init script so a stored preference never flashes the wrong
      theme, and `:root[data-theme]` CSS blocks that override the
      `prefers-color-scheme` media query in both directions. Verified via
      the same headless-Chrome/CDP approach: clicked through System ->
      Light -> Dark -> System, confirmed colors/icon actually change and
      the preference survives a full page reload (added 2026-07-20).
      Optional `--citation-register PATH` accepts any
      `citation_evidence_register.md`-style file (CE-#### entries) to power
      hover/click tooltips on `[CE-####]`/`[CLAIM_NEEDS_REVISION]`-style
      inline markers in the review UI — harmless no-op for documents that
      don't use that marker convention.
      Added a "Download" toolbar menu — exports the document as currently
      reviewed (nothing needs saving to disk first) as PDF, Word (`.docx`),
      or Markdown, each in "as shown" or "clean" (strips `[CE-####]`,
      `[INTERNAL EVIDENCE - ...]`, `[NEW - added ...]`, and the standard
      uncertainty markers via the new `strip_catalogue_markers()`) — for
      previewing a submission-ready read without touching the live document.
      PDF/Word go through `pandoc` (PDF additionally needs
      `wkhtmltopdf`/`weasyprint`/a LaTeX engine on PATH — `find_pdf_engine()`
      picks whichever's available); missing tooling reports a clear error
      to the browser instead of failing silently. Markdown export needs no
      external tool. (`/api/export`, tests in
      `TestStripCatalogueMarkers`/`TestFindPdfEngine` in
      `tests/test_review.py`, added 2026-07-30)

## Legacy-to-DSR catalogue migration

- [x] 🟠 Build `catalogues/legacy_dsr_migration.py` — one-off migration
      utility from the legacy engine's `instance/catalogue.db` to the DSR
      catalogue's `instance/catalogue_dsr.db`, plus reference-marker
      rewriting for files that cite legacy catalogue IDs. Three independent,
      inspectable stages: (1) `crosswalk` — reads the legacy DB read-only
      (sqlite URI `mode=ro`, can never write to it by construction),
      classifies each record's file through the real DSR classifier, and
      mints DSR entries via the same counters a normal `--dsr` scan uses
      (so IDs never collide), writing a legacy-id -> dsr-stable-id
      crosswalk CSV; (2) `apply-references` / `apply-register` — rewrites
      legacy-ID tokens inside `[INTERNAL EVIDENCE - ...]` markers or a
      citation register's trailing `(LEGACY-ID)` parenthetical to the new
      DSR stable_id, always to a copy, never overwriting the target
      in-place, and never touching academic `[CE-####]` citation markers;
      (3) `validate` / `promote` — validate strips reference-marker spans
      from both old and new text and asserts the rest is byte-identical;
      promote is the only stage that writes to a "live" file, and only
      after backing it up to a timestamped, tracked archive path first.
      Dry-run by default everywhere; `--apply` required to write for real.
      Engine-generic: no thesis-specific (or otherwise caller-specific)
      path is hardcoded — target files, crosswalk CSV, and archive
      directory are all caller-supplied. (tests in
      `tests/test_legacy_dsr_migration.py`)
- [x] 🟠 Build `migrate_legacy_to_dsr.py` — CLI front-end for the module
      above: `crosswalk`, `apply-references`, `apply-register`,
      `rename-files` (renames each active, non-excluded, non-rollup
      catalogued file in place, swapping just the legacy-id token already
      embedded in its filename for the DSR stable_id, and updating
      `file_name`/`relative_path`/`source_path` in `catalogue_dsr.db` to
      match), `validate`, `promote` (`--label` overrides the default
      `pre-dsr-migration` backup-filename tag for non-DSR promotions, e.g.
      `--label "pre-gap-audit-fixes"`), and `report` (crosswalk coverage
      stats).
- [x] 🟠 Build `review_dsr.py` — browser-based, one-record-at-a-time review
      tool for DSR catalogue records flagged `confidence_status='Requires
      Review'` in `instance/catalogue_dsr.db`. Standard-library only,
      matching the project's other local review tools. Each decision
      (approve/edit/skip) writes straight to the DB as you go — no separate
      save step, so closing the browser or server at any point loses
      nothing, and re-running later just resumes on whatever's left.
      Serves its UI from `dsr_review_ui/`.

## Catalogue viewer

- [x] 🟢 Build `catalog.html` — static page that renders `catalogue_master.jsonl`/
      a `catalog.json` export as a browsable/searchable table. (`templates/catalog.html`,
      scaffolded next to `catalogue_master.jsonl` by `setup.py`; needs `python3 -m
      http.server` in that folder since browsers block `fetch()` of `file://` URLs)
- [x] 🟢 Add JS that polls/reloads the catalogue JSON so the page always reflects
      the latest data on disk without a manual rebuild. (re-fetches every 5s)

## Engine

- [x] 🔴 Add a `human_review_queue.csv` triage view/report. (`catalogue.py review-queue`,
      ranks records by why they need a look, not just a dump of every record)
- [x] 🔴 Add lightweight tests for `setup.py` (config validation, schema merge).
      (`tests/test_setup.py`, stdlib `unittest`, run via `python3 -m unittest
      discover tests`)
- [x] 🔴 Consider validating generated catalogue records against
      `schema.generated.json` (e.g. via `jsonschema` package) as part of Pass 2.
      (`catalogue.py validate-schema`, optional dep in `requirements.txt`, not
      part of `all`. First run found and fixed real schema/data drift: null
      `primary_entity_type`/`evidence_role`/`metadata_confidence` weren't
      permitted by the schema even though the pipeline leaves them null until
      classified; `source_organisation`/`source_system` enums didn't include
      null; `content_preview` had no hard character cap backing its
      `maxLength`, so dense/binary content could exceed it - now enforced in
      `cap_words()`.)
- [x] 🔴 Add a `summary` column (short one-line description of what the file is
      about, already AI-generated via `catalogue.py context`) to
      `rename_plan.csv`, positioned to the right of `catalogue_id`. No new AI
      calls needed - just expose the existing DB column, same pattern as
      `schema_reference`.

## Housekeeping

- [x] 🟤 Add a color-coded, live-updating `TODO.html` view (`scripts/generate_todo_html.py`
      + `scripts/colorize_todo_md.py`, same pattern as the ResearchBoss project). Items
      are colored by section since this file has no inline tag chain; the page polls
      itself over HTTP and reloads when `TODO.html` changes on disk. A project-local
      `PostToolUse` hook in `.claude/settings.local.json` reruns both scripts whenever
      Claude edits `TODO.md`, so the page and the color dots stay in sync automatically.
- [x] 🟤 Fix `iter_source_files()` scanning symlinks at all. First found as a
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

- [ ] 🟤 Downstream integration note (from Ledgerly/ResearchBoss's own TODO.md
      Phase 33, added 2026-07-17 — resolves the earlier stub item above about
      a "downstream catalogue database project"): Ledgerly, a separate sibling
      project (`~/Documents/_Projects/ResearchBoss`), plans to drive this
      project non-interactively per its own hosted user — staging that user's
      connected cloud folder (OneDrive/Google Drive/Dropbox) to a local
      directory, then invoking `catalogue.py` (`scan` → `extract` → `enrich`
      → `duplicates`/`near-duplicates` → `rename-plan` → `review-queue` →
      `export-jsonl`) as a subprocess against a per-caller `instance/`-style
      directory, then reading the resulting `catalogue_master.jsonl` directly
      (decided: no separate export step needed, it's already the canonical
      machine-readable output) to import catalogued files as citable sources.
      Nothing to build here yet and no behavior change — this project keeps
      running exactly as it does today (single shared `instance/`, run by
      hand, no accounts, no cloud). Flagging only so a future need for
      per-caller `instance/` directories (today assumes one shared instance)
      and a stable/documented subprocess exit-code contract isn't a surprise
      if/when Ledgerly's side of that integration gets built.
