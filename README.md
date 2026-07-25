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

### Crossref catalogue mode (`--crossref`)

`crossref_catalogue.py` implements Crossref's metadata deposit fields for
formally published, peer-reviewed scholarly outputs. Its own database is
`instance/catalogue_crossref.db`; its own output directory is
`instance/catalogued_files/crossref/`.

```
python3 catalogue.py scan --crossref --dry-run|--apply
python3 catalogue.py migrate --crossref --dry-run|--apply
python3 catalogue.py validate --crossref
python3 catalogue.py export --crossref   # writes crossref_catalogue.csv/json,
                                          # crossref_xml/<catalogue_id>.xml (one per
                                          # applicable record only), catalogue_schema.json,
                                          # catalogue_manual_review.csv, catalogue_migration_log.csv
```

Crossref is fundamentally different from the standards above: it exists to
register formally published scholarly works, not to describe arbitrary
project files, so this mode does not force every scanned file into a
scholarly-work shape. Every record gets a `crossref_applicable` flag - true
only when directory/filename evidence suggests the file is plausibly a
scholarly-work manuscript (under `publications/`, `manuscripts/`, `papers/`,
or `submissions/`, or a filename containing a work-type token like
`journal-article` or `conference-paper`). Files where it's false get
`publication_type = "Not Applicable"`, are still catalogued (so `scan` stays
a complete inventory) but are excluded from `validate`'s issue counts and
`export`'s XML output. As with DataCite, a DOI is never fabricated - `doi`
is only ever populated via an explicit `<file>.crossref.json` sidecar.

### CERIF catalogue mode (`--cerif`)

`cerif_catalogue.py` implements the Common European Research Information
Format's output-bearing base entities and its hallmark time-stamped
relationship model. Its own database is `instance/catalogue_cerif.db`; its
own output directory is `instance/catalogued_files/cerif/`.

```
python3 catalogue.py scan --cerif --dry-run|--apply
python3 catalogue.py migrate --cerif --dry-run|--apply
python3 catalogue.py validate --cerif
python3 catalogue.py export --cerif   # writes cerif_catalogue.csv/json,
                                       # cerif_xml/<catalogue_id>.xml (one per record),
                                       # catalogue_schema.json, catalogue_manual_review.csv,
                                       # catalogue_migration_log.csv
```

Unlike Crossref, CERIF is explicitly broad-scope - it's meant to cover
essentially any research output, not just formally published works. So
every catalogued file becomes one of CERIF's two output-bearing entities:
`cfResPubl` (scholarly-manuscript files, using the same evidence as
`--crossref`'s applicability check) or `cfResProd` (everything else -
datasets, software, models, or an unresolved "Other Product"). Relationships
(`cfPers_ResPubl`/`cfResProd`, `cfOrgUnit_...`, `cfProj_...`, each carrying a
`cfStartDate` per CERIF's temporal-validity model) are populated only from
explicitly configured `project_config.json` fields (`researcher`,
`institution`, `project_name`) - never invented, and the template's
`REPLACE_ME` placeholder is never treated as real configured data.
`cfClassId`/`cfClassSchemeId` values are human-readable local labels rather
than the UUIDs a real Common CERIF Vocabulary server would use, since this
project has no such vocabulary service to resolve against.

### RO-Crate catalogue mode (`--ro-crate`)

`ro_crate_catalogue.py` implements Research Object Crate packaging. Its own
database is `instance/catalogue_ro_crate.db`; its own output directory is
`instance/catalogued_files/ro_crate/`.

```
python3 catalogue.py scan --ro-crate --dry-run|--apply
python3 catalogue.py migrate --ro-crate --dry-run|--apply
python3 catalogue.py validate --ro-crate
python3 catalogue.py export --ro-crate   # writes ro_crate_catalogue.csv/json,
                                          # crates/<source-root-name>/ro-crate-metadata.json
                                          # (one real RO-Crate 1.2 JSON-LD manifest per
                                          # configured SOURCE_DATA_ROOTS entry),
                                          # catalogue_schema.json, catalogue_manual_review.csv,
                                          # catalogue_migration_log.csv
```

RO-Crate is structurally different from every other mode here: it isn't a
flat per-file record schema, it's a JSON-LD graph. `export`'s defining
output is `ro-crate-metadata.json` itself - a root `Dataset` entity (`"./"`)
whose `hasPart` links to a `File` entity per catalogued file
(`name`/`contentSize`/`encodingFormat`/`dateModified`/`sha256`), plus the
self-describing metadata-descriptor entity conforming to RO-Crate 1.2, plus
`Person` entities for configured authorship. `hasPart` is flat (every file
linked directly from the root) rather than a nested tree of intermediate
directory-`Dataset` entities mirroring the real folder structure - a valid
simplification the spec permits, stated here rather than silently assumed.
As with CERIF, author relations only come from `project_config.json` ->
`researcher` when genuinely configured (never the `REPLACE_ME` placeholder).

### DCAT catalogue mode (`--dcat`)

`dcat_catalogue.py` implements the W3C Data Catalog Vocabulary's core
classes: one `dcat:Catalog` per configured `SOURCE_DATA_ROOTS` entry
(mirroring RO-Crate's per-root grouping), containing one `dcat:Dataset` per
catalogued file, each with exactly one `dcat:Distribution` describing its
format/size/checksum. Its own database is `instance/catalogue_dcat.db`; its
own output directory is `instance/catalogued_files/dcat/`.

```
python3 catalogue.py scan --dcat --dry-run|--apply
python3 catalogue.py migrate --dcat --dry-run|--apply
python3 catalogue.py validate --dcat
python3 catalogue.py export --dcat   # writes dcat_catalogue.csv/json,
                                      # turtle/<source-root-name>.ttl (one real DCAT
                                      # Turtle/RDF catalogue per SOURCE_DATA_ROOTS entry),
                                      # catalogue_schema.json, catalogue_manual_review.csv,
                                      # catalogue_migration_log.csv
```

DCAT is fundamentally an RDF vocabulary, so - unlike RO-Crate's JSON-LD
graph - `export`'s defining output here is Turtle, the form DCAT is most
commonly published in on open-data portals. Checksums are represented via
the standard SPDX `Checksum` blank-node pattern DCAT-AP profiles use, not a
bespoke property. `dataset`/`distribution` URIs are content-addressed
(`urn:dcat:dataset:sha256-<hash>`); `publisher` falls back to
`project_config.json` -> `institution` when genuinely configured (same
reasoning as `--datacite`); `issued` is a proxy from the file's creation
timestamp and is always flagged Requires Review, since a filesystem
timestamp is not a true issuance date.

### MODS catalogue mode (`--mods`)

`mods_catalogue.py` implements the Library of Congress's Metadata Object
Description Schema - richer bibliographic description than Dublin Core,
less complex than MARC 21. Like Dublin Core and DataCite, it describes
every catalogued file (no applicability-gating). Its own database is
`instance/catalogue_mods.db`; its own output directory is
`instance/catalogued_files/mods/`.

```
python3 catalogue.py scan --mods --dry-run|--apply
python3 catalogue.py migrate --mods --dry-run|--apply
python3 catalogue.py validate --mods
python3 catalogue.py export --mods   # writes mods_catalogue.csv/json,
                                      # mods_xml/<catalogue_id>.xml (one real MODS 3.7
                                      # XML record per file, in the Library of Congress's
                                      # http://www.loc.gov/mods/v3 namespace),
                                      # catalogue_schema.json, catalogue_manual_review.csv,
                                      # catalogue_migration_log.csv
```

`typeOfResource` comes from MODS's controlled vocabulary via file
extension; `extent` is a digital-native "`<N> bytes`" statement;
`identifier` is content-addressed (`urn:mods:sha256:<hash>`,
`type="local"`); `recordInfo` documents the record's own machine-generated
provenance (a factual statement about how the record was produced, not
invented data about the file). `digitalOrigin` (born-digital vs digitized)
is deliberately omitted rather than guessed - MODS's controlled vocabulary
for it has no "Unknown" value, and this engine cannot tell from a file's
bytes alone whether, say, a PDF is a native export or a scan. As with
CERIF/RO-Crate, the creator name only comes from `project_config.json` ->
`researcher` when genuinely configured.

### MARC 21 catalogue mode (`--marc21`)

`marc21_catalogue.py` implements MARC 21 bibliographic records - the most
structurally rigid standard here: a byte-exact 24-position Leader and
40-character 008 control field, plus tagged/indicatored variable fields.
Like Dublin Core/DataCite/MODS, it describes every catalogued file. Its own
database is `instance/catalogue_marc21.db`; its own output directory is
`instance/catalogued_files/marc21/`.

```
python3 catalogue.py scan --marc21 --dry-run|--apply
python3 catalogue.py migrate --marc21 --dry-run|--apply
python3 catalogue.py validate --marc21
python3 catalogue.py export --marc21   # writes marc21_catalogue.csv/json,
                                        # marc21_catalogue.mrk (one real MARC mnemonic
                                        # ".mrk" record per file - the MarcEdit/MARCMaker
                                        # interchange text format), catalogue_schema.json,
                                        # catalogue_manual_review.csv, catalogue_migration_log.csv
```

Every Leader/008 position this engine cannot honestly derive uses MARC's
own sanctioned `|` "no attempt to code" fill character, never a fabricated
value - e.g. Leader/17 (encoding level) is `u` Unknown (honest for a
machine-generated, unreviewed record), 008/35-37 (language) is `und`
(Undetermined, the correct ISO 639-2 code for "language unknown"), and
008/39 (cataloging source) is `d` Other. `020`/`022`/`024` (ISBN/ISSN/other
standard identifiers) only ever come from an explicit `<file>.marc21.json`
sidecar, since those are formally assigned identifiers. `245` (title
statement)'s indicator 2 - MARC's real "number of nonfiling characters"
convention for leading articles like "The"/"A"/"An" - is computed, not
hardcoded. `100`/`700` (personal names) only appear when
`project_config.json` -> `researcher` is genuinely configured.

### METS catalogue mode (`--mets`)

`mets_catalogue.py` implements the Metadata Encoding and Transmission
Standard - one `mets.xml` package per configured `SOURCE_DATA_ROOTS` entry
(mirroring RO-Crate's and DCAT's per-root grouping). Its own database is
`instance/catalogue_mets.db`; its own output directory is
`instance/catalogued_files/mets/`.

```
python3 catalogue.py scan --mets --dry-run|--apply
python3 catalogue.py migrate --mets --dry-run|--apply
python3 catalogue.py validate --mets
python3 catalogue.py export --mets   # writes mets_catalogue.csv/json,
                                      # packages/<source-root-name>/mets.xml (one real
                                      # METS document per SOURCE_DATA_ROOTS entry, in the
                                      # http://www.loc.gov/METS/ namespace),
                                      # catalogue_schema.json, catalogue_manual_review.csv,
                                      # catalogue_migration_log.csv
```

Unlike RO-Crate (deliberately kept flat - see that section above), METS's
whole purpose is the structural relationship between files, so `export`
builds a real nested `structMap` `<div>` tree mirroring the source root's
actual directory hierarchy, with a leaf `<div><fptr FILEID="..."/></div>`
per file - the one place across these ten modules where reproducing that
nesting is the standard's whole point rather than an unnecessary
complication. `fileSec/file` entries carry a real SHA-256
`CHECKSUM`/`CHECKSUMTYPE`. `amdSec/techMD` stays intentionally minimal
(size/format/checksum) rather than duplicating a full object model - real
METS deployments typically point `techMD` at a full PREMIS object instead,
which this project's own `--premis` mode can independently produce.

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
