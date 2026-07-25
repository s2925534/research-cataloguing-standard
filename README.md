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

## Standard-specific catalogue modes

Alongside the primary engine above, `catalogue.py` can run one of several
external metadata/cataloguing standards as an alternate catalogue profile,
one flag per standard (see `STANDARD_CATALOGUE_MODULES` in `catalogue.py`).
Every one of these is a fully separate, additive pipeline: its own database
under `instance/`, its own output directory under
`instance/catalogued_files/<name>/`, and its own `scan`/`migrate`/`validate`/
`export` commands. None of them ever opens `instance/catalogue.db` or another
standard's database/output directory - the primary engine and every other
standard's catalogue are unaffected whichever of these flags you use.

```
python3 catalogue.py scan <flag> --dry-run|--apply
python3 catalogue.py migrate <flag> --dry-run|--apply   # re-classify already-scanned
                                                         # records, no filesystem walk
python3 catalogue.py validate <flag>
python3 catalogue.py export <flag>
```

### DSR catalogue mode (`--dsr`)

`dsr_catalogue.py` implements an alternate, Design Science Research-specific
cataloguing standard, activated with `--dsr`. It is a fully separate
pipeline: its own database (`instance/catalogue_dsr.db`), its own ID scheme
(`<PROJECT>-<CLASS>-<SUBTYPE>-<SEQUENCE>-<VERSION>`, e.g.
`DSR-ART-MOD-0001-V1.0`), and its own output directory
(`instance/catalogued_files/dsr/`). It never opens or writes
`instance/catalogue.db` or the legacy outputs above - the pipeline described
in the previous section is completely unaffected whether or not `--dsr` is
ever used.

```
python3 catalogue.py scan --dsr --dry-run             # preview classification, writes nothing
python3 catalogue.py scan --dsr --apply                # inventory + classify into instance/catalogue_dsr.db
python3 catalogue.py migrate --dsr --dry-run|--apply   # re-classify already-scanned records against
                                                        # current rules, no filesystem walk
python3 catalogue.py validate --dsr                    # schema/relationship/version integrity check
python3 catalogue.py export --dsr                      # write research_catalogue.csv/json/md/sqlite,
                                                        # catalogue_relationships.csv, catalogue_schema.json,
                                                        # catalogue_controlled_vocabulary.json,
                                                        # catalogue_migration_log.csv, catalogue_manual_review.csv,
                                                        # catalogue_classification_rules.json
python3 catalogue.py update-references --dsr --dry-run|--apply
                                                        # replaces {{dsr-ref:<stable_id or relative_path>}}
                                                        # tokens in docs under project_config.json ->
                                                        # dsr_reference_roots with formatted catalogue
                                                        # citations. No-op until that key is set.
```

Classification is deterministic (extension -> directory override -> filename
token -> explicit `<file>.dsrmeta.json` sidecar, in that priority order) and
never invents metadata: anything it cannot derive is recorded as `Unknown`,
`Not Assigned`, or `Requires Review` rather than guessed. See
`dsr_catalogue.py`'s module docstring for the full decision order, and
`templates/project_config.template.json` -> `dsr_catalogue_rules` /
`dsr_reference_roots` for the (optional) project-specific overrides.

### Dublin Core catalogue mode (`--dublin-core`)

`dublin_core_catalogue.py` implements the Dublin Core Metadata Element Set
(the 15 core elements) plus the handful of DCTERMS refinements most commonly
used alongside them (`created`, `modified`, `extent`, `isPartOf`, `hasPart`,
`isVersionOf`, `hasVersion`, `conformsTo`, `license`, `accessRights`). Its own
database is `instance/catalogue_dublin_core.db`; its own output directory is
`instance/catalogued_files/dublin_core/`.

```
python3 catalogue.py scan --dublin-core --dry-run|--apply
python3 catalogue.py migrate --dublin-core --dry-run|--apply
python3 catalogue.py validate --dublin-core
python3 catalogue.py export --dublin-core   # writes dublin_core_catalogue.csv/json,
                                             # dublin_core_catalogue.xml (OAI simple-DC),
                                             # catalogue_schema.json, catalogue_manual_review.csv,
                                             # catalogue_migration_log.csv
```

Unlike DSR, Dublin Core is a flat description vocabulary, not a
classification taxonomy - there's no artefact-type decision tree, just
deterministic per-element derivation: `dc:identifier` is a content-addressed
`urn:sha256:<hash>` (stable across renames, since it depends only on file
content), `dc:type` comes from the DCMI Type Vocabulary via file extension,
`dc:format` is the file's MIME type, and `dc:date`/`dcterms:created`/
`dcterms:modified` come from filesystem timestamps. Elements this engine
can't deterministically derive (`creator`, `subject`, `description`,
`publisher`, `rights`, etc.) default to `Unknown`/empty rather than being
invented, unless a `<file>.dcmeta.json` sidecar supplies them explicitly.

### DataCite catalogue mode (`--datacite`)

`datacite_catalogue.py` implements the DataCite Metadata Schema's mandatory
properties (Identifier, Creator, Title, Publisher, PublicationYear,
ResourceType) plus the recommended properties most relevant to a local
catalogue (Subject, Contributor, Date, RelatedIdentifier, Description,
Language, Version, Rights, Formats, Sizes). Its own database is
`instance/catalogue_datacite.db`; its own output directory is
`instance/catalogued_files/datacite/`.

```
python3 catalogue.py scan --datacite --dry-run|--apply
python3 catalogue.py migrate --datacite --dry-run|--apply
python3 catalogue.py validate --datacite
python3 catalogue.py export --datacite   # writes datacite_catalogue.csv/json,
                                          # datacite_xml/<catalogue_id>.xml (one real
                                          # DataCite kernel-4 XML record per file),
                                          # catalogue_schema.json, catalogue_manual_review.csv,
                                          # catalogue_migration_log.csv
```

This engine never fabricates a DOI - `identifier` defaults to
`identifierType="Local"` with a content-addressed value (the file's sha256),
since a DOI is a formally registered identifier that can't be derived from a
file's bytes or path. A real DOI (or any other DataCite property) can only
enter a record via an explicit `<file>.datacite.json` sidecar. `publisher`
defaults to `project_config.json` -> `institution` when set (explicitly
configured data, not invented) else `Unknown`; `resourceTypeGeneral` comes
from DataCite's controlled vocabulary via file extension; `publicationYear`
defaults to the file's last-modified year but is always flagged Requires
Review since a filesystem timestamp is a proxy, not a true publication date.

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
