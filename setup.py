#!/usr/bin/env python3
"""First-time setup for the research cataloguing standard.

Reads instance/project_config.json (+ instance/.env) and merges them onto
templates/schema_core.json / templates/vocabularies_core.json to produce a
project-specific, fully-enumerated instance/schema.generated.json, then
scaffolds the output repository layout under instance/.

templates/ holds the reusable, committed engine + example config.
instance/ holds this checkout's real, gitignored config and data.

This script does NOT catalogue, rename, move or copy any research files.
It only prepares the configuration/schema machinery those later steps read.

Usage:
    python3 setup.py            # validate config + generate schema + scaffold folders
    python3 setup.py --check    # validate only, no writes
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT_DIR / "templates"
INSTANCE_DIR = ROOT_DIR / "instance"

REQUIRED_PROJECT_CONFIG_KEYS = [
    "project_id",
    "project_name",
    "repository_layout",
    "organisations",
    "systems",
    "extended_artefact_types",
    "domain_identifier_fields",
    "research_taxonomies",
    "preview_max_words",
]

REQUIRED_ENV_KEYS = ["SOURCE_DATA_ROOTS", "OUTPUT_ROOT"]

STAGED_FOLDERS = [
    "00_INBOX_UNPROCESSED",
    "01_RAW_IMMUTABLE",
    "02_LITERATURE",
    "03_STANDARDS_AND_FRAMEWORKS",
    "04_OPERATIONAL_EVIDENCE",
    "05_API_SCHEMAS_AND_INTEGRATIONS",
    "06_DATA_EXPORTS",
    "07_RESEARCH_ARTEFACTS",
    "08_SUPERVISION_AND_TRANSCRIPTS",
    "09_VALIDATION",
    "10_OUTPUTS",
    "98_QUARANTINE_UNREADABLE",
    "99_ARCHIVE_SUPERSEDED",
]

CATALOGUE_OUTPUT_FILES = [
    "catalogue_master.jsonl",
    "catalogue_master.csv",
    "rename_plan.csv",
    "duplicate_report.csv",
    "unreadable_or_encrypted_report.csv",
    "human_review_queue.csv",
    "cataloguing_log.txt",
]


def load_json(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"ERROR: required file missing: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def parse_env_file(path: Path) -> dict:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def load_env() -> dict:
    env_path = INSTANCE_DIR / ".env"
    example_path = TEMPLATES_DIR / ".env.example"
    if not env_path.exists():
        raise SystemExit(
            f"ERROR: {env_path} not found. Copy {example_path} to {env_path} and fill in real paths first."
        )
    return parse_env_file(env_path)


def validate_project_config(config: dict) -> list[str]:
    errors = []
    for key in REQUIRED_PROJECT_CONFIG_KEYS:
        if key not in config:
            errors.append(f"project_config.json is missing required key: {key}")
    if config.get("repository_layout") not in ("flat", "staged"):
        errors.append("project_config.json -> repository_layout must be 'flat' or 'staged'")
    taxonomies = config.get("research_taxonomies")
    if taxonomies is not None and not isinstance(taxonomies, dict):
        errors.append("project_config.json -> research_taxonomies must be an object")
    return errors


def validate_env(env: dict) -> list[str]:
    errors = []
    for key in REQUIRED_ENV_KEYS:
        if not env.get(key):
            errors.append(f".env is missing required value: {key}")
    for root in env.get("SOURCE_DATA_ROOTS", "").split(","):
        root = root.strip()
        if root and not Path(root).exists():
            errors.append(f".env -> SOURCE_DATA_ROOTS entry does not exist on disk: {root}")
    return errors


def generate_schema(schema_core: dict, vocab_core: dict, project_config: dict) -> dict:
    schema = json.loads(json.dumps(schema_core))  # deep copy
    props = schema["properties"]

    organisations = sorted(set(project_config.get("organisations", [])) | {vocab_core["unknown_value"]})
    props["source_organisation"]["enum"] = organisations

    systems = sorted(set(project_config.get("systems", [])) | {"NA", vocab_core["unknown_value"]})
    props["source_system"]["enum"] = systems

    artefact_types = sorted(
        set(vocab_core.get("core_artefact_types", [])) | set(project_config.get("extended_artefact_types", []))
    )
    props["artefact_type"]["enum"] = artefact_types

    extended_entities = project_config.get("extended_entity_types", [])
    if extended_entities:
        props["primary_entity_type"]["enum"] = sorted(
            set(props["primary_entity_type"]["enum"]) | set(extended_entities)
        )

    props["domain_identifiers"]["description"] += (
        " Recognised keys for this project: " + ", ".join(project_config.get("domain_identifier_fields", [])) + "."
    )

    taxonomies = project_config.get("research_taxonomies", {})
    props["research_taxonomy"]["description"] += (
        " Recognised keys for this project: " + ", ".join(taxonomies.keys()) + "."
    )
    props["research_taxonomy"]["_allowed_values_by_key"] = taxonomies

    props["content_preview"]["description"] = (
        f"Short extracted or OCR'd preview of file content, capped at {project_config.get('preview_max_words', 500)} words."
    )

    schema["title"] = f"Research Source Catalogue Record ({project_config.get('project_name', 'project')})"
    schema["$id"] = (
        f"https://example.local/schemas/{project_config.get('project_id', 'project')}-catalogue.schema.json"
    )
    return schema


def csv_header_from_schema(schema: dict) -> str:
    return ",".join(schema["properties"].keys())


def scaffold_output(env: dict, project_config: dict, csv_header: str) -> list[Path]:
    output_root = Path(env["OUTPUT_ROOT"])
    layout = project_config["repository_layout"]
    created = []

    output_root.mkdir(parents=True, exist_ok=True)

    if layout == "staged":
        for folder in STAGED_FOLDERS:
            d = output_root / folder
            d.mkdir(parents=True, exist_ok=True)
            created.append(d)
        catalogue_dir = output_root / "10_OUTPUTS"
    else:
        catalogue_dir = output_root / "catalogued_files"
        catalogue_dir.mkdir(parents=True, exist_ok=True)
        created.append(catalogue_dir)

    for filename in CATALOGUE_OUTPUT_FILES:
        target = catalogue_dir / filename
        if target.exists():
            continue
        if filename.endswith(".csv") and filename == "catalogue_master.csv":
            target.write_text(csv_header + "\n", encoding="utf-8")
        else:
            target.touch()
        created.append(target)

    return created


def main() -> int:
    check_only = "--check" in sys.argv

    project_config_path = INSTANCE_DIR / "project_config.json"
    if not project_config_path.exists():
        template_path = TEMPLATES_DIR / "project_config.template.json"
        raise SystemExit(
            f"ERROR: {project_config_path} not found. "
            f"Copy {template_path} to {project_config_path} and fill in your project's real values first."
        )

    project_config = load_json(project_config_path)
    vocab_core = load_json(TEMPLATES_DIR / "vocabularies_core.json")
    schema_core = load_json(TEMPLATES_DIR / "schema_core.json")
    env = load_env()

    errors = validate_project_config(project_config) + validate_env(env)
    if errors:
        print("Setup blocked. Fix the following before cataloguing can begin:\n")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(f"Project: {project_config['project_name']} ({project_config['project_id']})")
    print(f"Repository layout: {project_config['repository_layout']}")
    print(f"Source roots: {env['SOURCE_DATA_ROOTS']}")
    print(f"Output root: {env['OUTPUT_ROOT']}")

    schema = generate_schema(schema_core, vocab_core, project_config)
    csv_header = csv_header_from_schema(schema)

    if check_only:
        print("\n--check passed: configuration is valid. No files written.")
        return 0

    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    schema_out = INSTANCE_DIR / "schema.generated.json"
    schema_out.write_text(json.dumps(schema, indent=2), encoding="utf-8")
    print(f"\nWrote {schema_out.relative_to(ROOT_DIR)}")

    created = scaffold_output(env, project_config, csv_header)
    print(f"Scaffolded {len(created)} output paths under {env['OUTPUT_ROOT']}")

    print(
        "\nSetup complete. No research files were touched. "
        "Cataloguing (Pass 1: inventory) can now be run as a separate, explicit step."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
