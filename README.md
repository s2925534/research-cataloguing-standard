# research-cataloguing-standard

A config-driven engine for creating auditable, deterministic catalogues of
research source files (literature, standards, operational evidence, data
exports) for any research project. It was originally drafted for the MPhil
Container Readiness Framework thesis and generalized so the same engine can
catalogue a different research project's files by swapping out one config
file.

## Layout

```
templates/    committed, project-agnostic engine + example config
instance/     gitignored, this checkout's real config, generated schema and cataloguing output
setup.py      entry point: reads instance/, writes instance/
```

### `templates/` (committed — do not edit per project)

- **`schema_core.json`** — JSON Schema for one catalogue record. Fields whose
  allowed values differ per project (organisation, system, artefact type,
  research taxonomy) are left open here and filled in by `setup.py` from
  `instance/project_config.json`.
- **`vocabularies_core.json`** — universal codes: file classes, statuses,
  access classifications, and a baseline artefact-type list common to any
  research project.
- **`cataloguing_instructions_core.txt`** — ready-to-use instructions for a
  local AI or automation agent. References `instance/project_config.json` /
  `instance/schema.generated.json` instead of hardcoding values.
- **`project_config.template.json`** — starting point for a new project's
  config. Copy to `instance/project_config.json` and fill in real values.
- **`.env.example`** — starting point for machine-specific settings. Copy to
  `instance/.env`.

### `instance/` (gitignored — your real, per-checkout data)

- **`project_config.json`** — this project's real profile: identity,
  repository layout, organisations, systems, extended artefact types, domain
  identifier fields, research taxonomies (RQ mapping, framework components).
- **`.env`** — this machine's real settings: source data root(s), output
  root, OCR toggle, hash algorithm, review confidence threshold.
- **`schema.generated.json`** — `templates/schema_core.json` with
  `project_config.json`'s values merged into its enums. Produced by
  `python3 setup.py`.
- **`catalogue.db`** — SQLite database, the primary queryable catalogue
  store. Produced/updated by `python3 catalogue.py`.
- **`catalogued_files/`** (`catalogue_master.jsonl`, `duplicate_report.csv`,
  `rename_plan.csv`, `unreadable_or_encrypted_report.csv`, etc.) — CSV/JSONL
  exports and reports derived from `catalogue.db`.

## First-time setup

```
cp templates/.env.example instance/.env                        # then edit with real paths
cp templates/project_config.template.json instance/project_config.json  # then edit with real values
python3 setup.py --check  # validate config only, writes nothing
python3 setup.py          # generate instance/schema.generated.json + scaffold instance/ output folders
```

`setup.py` never touches research files themselves. It only validates
`instance/project_config.json` / `instance/.env` and prepares the schema and
output-folder scaffolding that a later, separate cataloguing pass will use.

## To catalogue a different research project

Clone this repo, populate `instance/project_config.json` and `instance/.env`
with that project's values (starting from the `templates/` examples), and
run `setup.py`. Nothing in `templates/` needs to change.

## Running the cataloguer

`catalogue.py` implements Pass 1-3 of `templates/cataloguing_instructions_core.txt`
against `instance/catalogue.db` (SQLite, primary queryable store):

```
python3 catalogue.py scan          # Pass 1: walk SOURCE_DATA_ROOTS, hash + inventory every
                                    # non-zip file; known cloned spec/code repos are catalogued
                                    # as one rollup record each rather than per file
python3 catalogue.py extract       # Pass 2: text/OCR content preview + heuristic classification
python3 catalogue.py enrich        # Pass 2.5: embedded metadata + domain identifiers
python3 catalogue.py duplicates    # group by sha256, flag exact duplicates for later deletion
python3 catalogue.py near-duplicates # content-similarity match, flag near_duplicate
python3 catalogue.py group         # group repeat report exports/downloads by base filename
python3 catalogue.py rename-plan   # Pass 3: PROPOSE filenames -> instance/catalogued_files/rename_plan.csv
python3 catalogue.py review-queue  # write human_review_queue.csv, ranked by why each record needs a look
python3 catalogue.py export-jsonl  # refresh instance/catalogued_files/catalogue_master.jsonl from the DB
python3 catalogue.py validate-schema # optional (needs `pip install -r requirements.txt`): validate every
                                    # record against instance/schema.generated.json
python3 catalogue.py verify        # data-integrity regression check
python3 catalogue.py stats         # summary counts
python3 catalogue.py all           # scan..review-queue..export..verify..stats, in order
```

It never renames, moves, copies or deletes a source file. Pass 4 (approved,
human-triggered rename into `instance/catalogued_files/documents/`) is a
separate, explicit step:

```
python3 catalogue.py apply-rename                    # dry run: prints the plan, writes nothing
python3 catalogue.py apply-rename --execute           # copies sources -> instance/catalogued_files/documents/
                                                        # (kept out of catalogued_files/ itself so research
                                                        # files never mix with the pipeline's own tool/report
                                                        # output there - catalog.html, catalogue_master.*,
                                                        # *_report.csv. Per-file metadata lookup comes from
                                                        # catalogue_master.jsonl/catalog.html, not a sidecar
                                                        # next to each copy.)
```

`--skip-duplicates` omits files flagged `duplicate_status=exact_duplicate`;
`--nested` mirrors each file's original source subdirectory instead of the
default flat layout; `--group-literature` carves `LIT` records into their
own `documents/literature/` subfolder regardless of layout. Everything from the
automated passes is written with `human_review_required = 1` and low
`rename_confidence`; treat it as triage, not a finished catalogue.

Open `instance/catalogued_files/catalog.html` (scaffolded by `setup.py`) in
a browser for a searchable/sortable table view of the catalogue - it needs
to be served over http, not opened as a `file://` URL, e.g. `python3 -m
http.server` from that folder.

## Key principle (unchanged from the original standard)

Do not encode every detail in filenames. Use filenames for quick
recognition and the catalogue for provenance, research mapping, evidence
status, sensitivity, duplication, validation and audit history.

## Recommended operating model (unchanged)

Keep raw files immutable. Generate a rename plan first. Require human
approval. Rename only working copies. Retain a hash, original filename and
original path permanently.

## Author

Pedro Veloso — pedro@veloso.dev

## License

MIT — see [LICENSE](LICENSE). Free to use for this project.
