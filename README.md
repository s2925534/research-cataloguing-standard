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
- **`catalogued_files/`** (`catalogue_master.csv` / `.jsonl`, `rename_plan.csv`,
  etc.) — scaffolded output files, ready for a later cataloguing pass to
  populate.

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
