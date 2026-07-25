#!/usr/bin/env python3
"""CERIF (Common European Research Information Format) catalogue mode -
activated by --cerif.

A fully separate, additive cataloguing pipeline (see catalogue_common.py for
shared plumbing, dsr_catalogue.py for the isolation pattern this follows):
its own database (instance/catalogue_cerif.db) and its own output directory
(instance/catalogued_files/cerif/). Never opens or writes
instance/catalogue.db or any other standard's outputs.

CERIF models research information as base entities (cfPers, cfOrgUnit,
cfProj, cfResPubl, cfResProd, ...) linked by time-stamped relationships
(every relationship carries a cfStartDate/cfEndDate - CERIF's hallmark
temporal-validity model). Unlike Crossref, CERIF is explicitly broad-scope:
it's meant to cover essentially any research output, not just formally
published works. So every catalogued file here becomes one of CERIF's two
output-bearing base entities:

  - cfResPubl (Research Publication) : scholarly-manuscript files, using the
    same directory/filename evidence as crossref_catalogue.py's
    determine_applicability (a journal-article/conference-paper/book/etc
    token, or a publications/manuscripts/papers/submissions directory).
  - cfResProd (Research Product)     : everything else - datasets, software,
    models, images, or an unresolved "Other Product".

Relationships (cfPers_ResPubl/cfResProd, cfOrgUnit_ResPubl/cfResProd,
cfProj_ResPubl/cfResProd) are populated only from explicitly configured
project_config.json fields (researcher, institution, project_name) - never
invented. cfClassId/cfClassSchemeId values below are human-readable local
labels rather than the UUIDs a real Common CERIF Vocabulary instance would
use, since this project has no such vocabulary server to resolve against -
documented here rather than fabricated.
"""
from __future__ import annotations

import csv
import json
import xml.sax.saxutils as saxutils
from datetime import datetime, timezone
from pathlib import Path

from . import catalogue_common as common
from . import crossref_catalogue

DB_PATH = common.INSTANCE_DIR / "catalogue_cerif.db"
OUTPUT_DIR = common.CATALOGUE_DIR / "cerif"

CATALOGUE_CSV = OUTPUT_DIR / "cerif_catalogue.csv"
CATALOGUE_JSON = OUTPUT_DIR / "cerif_catalogue.json"
CATALOGUE_XML_DIR = OUTPUT_DIR / "cerif_xml"
SCHEMA_JSON = OUTPUT_DIR / "catalogue_schema.json"
MANUAL_REVIEW_CSV = OUTPUT_DIR / "catalogue_manual_review.csv"
MIGRATION_LOG_CSV = OUTPUT_DIR / "catalogue_migration_log.csv"

UNKNOWN = "Unknown"

ENTITY_TYPES = ["cfResPubl", "cfResProd"]

RESPUBL_RESULT_TYPES = [
    "Journal Article", "Book", "Book Chapter", "Conference Proceedings",
    "Report", "Thesis/Dissertation", "Preprint", "Other Publication",
]
RESPROD_RESULT_TYPES = ["Dataset", "Software", "Model", "Patent", "Other Product"]
RESULT_TYPES = RESPUBL_RESULT_TYPES + RESPROD_RESULT_TYPES

EXTENSION_TO_RESPROD_TYPE = {
    ".csv": "Dataset", ".tsv": "Dataset", ".xlsx": "Dataset", ".xls": "Dataset",
    ".json": "Dataset", ".jsonl": "Dataset", ".xml": "Dataset", ".parquet": "Dataset",
    ".sqlite": "Dataset", ".db": "Dataset",
    ".py": "Software", ".js": "Software", ".ts": "Software", ".java": "Software", ".sh": "Software",
    ".ipynb": "Software",
    ".drawio": "Model", ".mmd": "Model", ".puml": "Model", ".bpmn": "Model", ".archimate": "Model",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cerif_catalogue (
    catalogue_id TEXT PRIMARY KEY,
    source_path TEXT UNIQUE,
    relative_path TEXT,
    file_name TEXT,
    cerif_id TEXT,
    entity_type TEXT,
    result_type TEXT,
    title TEXT,
    abstract TEXT DEFAULT 'Unknown',
    language TEXT DEFAULT 'Unknown',
    publication_date TEXT,
    person_relations_json TEXT DEFAULT '[]',
    org_unit_relations_json TEXT DEFAULT '[]',
    project_relations_json TEXT DEFAULT '[]',
    sha256 TEXT,
    file_size_bytes INTEGER,
    explicit_metadata_applied INTEGER DEFAULT 0,
    confidence_status TEXT DEFAULT 'Requires Review',
    classification_rule TEXT,
    notes TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_cerif_sha256 ON cerif_catalogue(sha256);

CREATE TABLE IF NOT EXISTS cerif_counters (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_seq INTEGER NOT NULL DEFAULT 1
);
"""


def get_db(db_path: Path | None = None):
    return common.get_sqlite_db(db_path or DB_PATH, SCHEMA_SQL)


def next_seq(conn) -> int:
    row = conn.execute("SELECT next_seq FROM cerif_counters WHERE id = 1").fetchone()
    seq = row["next_seq"] if row else 1
    conn.execute(
        "INSERT INTO cerif_counters (id, next_seq) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET next_seq = ?",
        (seq + 1, seq + 1),
    )
    return seq


def load_sidecar_metadata(path: Path) -> dict | None:
    sidecar = path.with_name(path.name + ".cerif.json")
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


CROSSREF_TO_CERIF_RESPUBL_TYPE = {
    "journal-article": "Journal Article", "journal": "Journal Article",
    "book": "Book", "book-chapter": "Book Chapter", "monograph": "Book",
    "edited-book": "Book", "reference-book": "Book",
    "proceedings-article": "Conference Proceedings", "proceedings": "Conference Proceedings",
    "dissertation": "Thesis/Dissertation", "report": "Report",
    "posted-content": "Preprint", "standard": "Other Publication",
    "peer-review": "Other Publication", "dataset": "Other Publication",
}


def classify_entity(path: Path, source_root: Path) -> tuple[str, str, str, str]:
    """Returns (entity_type, result_type, classification_rule, evidence)."""
    applicable, work_type, evidence = crossref_catalogue.determine_applicability(path, source_root)
    if applicable:
        result_type = CROSSREF_TO_CERIF_RESPUBL_TYPE.get(work_type, "Other Publication") if work_type else "Other Publication"
        rule = "manuscript_evidence" if work_type else "manuscript_directory_only"
        return "cfResPubl", result_type, rule, evidence

    ext = path.suffix.lower()
    if ext in EXTENSION_TO_RESPROD_TYPE:
        return "cfResProd", EXTENSION_TO_RESPROD_TYPE[ext], "extension_mapping", f"ext={ext}"
    return "cfResProd", "Other Product", "fallback:unmapped_extension", f"ext={ext or '(none)'} has no mapping"


def build_record(path: Path, source_root: Path, seq: int, project_config: dict) -> dict:
    stat = path.stat()
    sha256 = common.sha256_file(path)
    sidecar = load_sidecar_metadata(path) or {}

    entity_type, result_type, rule, evidence = classify_entity(path, source_root)
    if sidecar.get("entity_type"):
        entity_type = sidecar["entity_type"]
        result_type = sidecar.get("result_type", result_type)
        rule = "explicit_sidecar_metadata"

    modified_date = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).date().isoformat()
    created_date = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).date().isoformat()

    researcher = project_config.get("researcher")
    institution = project_config.get("institution")
    project_name = project_config.get("project_name")

    default_person_relations = (
        [{"name": researcher, "role": "Author", "start_date": created_date}]
        if researcher and researcher != "REPLACE_ME" else []
    )
    default_org_relations = (
        [{"name": institution, "role": "Responsible Organisation", "start_date": created_date}]
        if institution and institution != "REPLACE_ME" else []
    )
    default_project_relations = (
        [{"name": project_name, "role": "Output", "start_date": created_date}]
        if project_name else []
    )

    has_explicit = bool(sidecar)
    confidence_status = "Confident" if (has_explicit or result_type not in ("Other Product", "Other Publication")) else "Requires Review"

    return {
        "catalogue_id": f"CERIF-{seq:05d}",
        "source_path": str(path.resolve()),
        "relative_path": str(path.relative_to(source_root)),
        "file_name": path.name,
        "cerif_id": sidecar.get("cerif_id", f"urn:cerif:sha256:{sha256}"),
        "entity_type": entity_type,
        "result_type": result_type,
        "title": sidecar.get("title", path.stem),
        "abstract": sidecar.get("abstract", UNKNOWN),
        "language": sidecar.get("language", UNKNOWN),
        "publication_date": sidecar.get("publication_date", modified_date),
        "person_relations": sidecar.get("person_relations", default_person_relations),
        "org_unit_relations": sidecar.get("org_unit_relations", default_org_relations),
        "project_relations": sidecar.get("project_relations", default_project_relations),
        "sha256": sha256,
        "file_size_bytes": stat.st_size,
        "explicit_metadata_applied": 1 if has_explicit else 0,
        "confidence_status": confidence_status,
        "classification_rule": rule,
        "notes": evidence if not has_explicit else None,
    }


LIST_FIELDS = ("person_relations", "org_unit_relations", "project_relations")


def _run_scan(conn, env: dict, project_config: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    scanned = 0
    excluded = 0
    requires_review = 0
    by_entity: dict = {}

    for source_root in common.source_roots_from_env(env):
        if not source_root.exists():
            print(f"WARNING: source root does not exist, skipping: {source_root}")
            continue
        for path in source_root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            if path.name.endswith(".cerif.json"):
                continue
            if common.is_excluded(path, source_root):
                excluded += 1
                continue

            source_path = str(path.resolve())
            existing = conn.execute(
                "SELECT catalogue_id FROM cerif_catalogue WHERE source_path = ?", (source_path,)
            ).fetchone()
            if existing:
                continue

            seq = next_seq(conn)
            record = build_record(path, source_root, seq, project_config)

            columns = []
            values = []
            for key, val in record.items():
                if key in LIST_FIELDS:
                    columns.append(f"{key}_json")
                    values.append(json.dumps(val))
                else:
                    columns.append(key)
                    values.append(val)
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO cerif_catalogue ({', '.join(columns)}, created_at, updated_at) "
                f"VALUES ({placeholders}, ?, ?)",
                (*values, now, now),
            )
            scanned += 1
            by_entity[record["entity_type"]] = by_entity.get(record["entity_type"], 0) + 1
            if record["confidence_status"] == "Requires Review":
                requires_review += 1
            if scanned % 200 == 0:
                conn.commit()

    conn.commit()
    return {"scanned": scanned, "excluded": excluded, "requires_review": requires_review, "by_entity": by_entity}


def cmd_scan(project_config: dict, env: dict, dry_run: bool, apply: bool) -> None:
    if dry_run == apply:
        raise SystemExit("scan --cerif requires exactly one of --dry-run or --apply")
    if apply:
        conn = get_db()
        report = _run_scan(conn, env, project_config)
        conn.close()
        print(f"CERIF scan (applied): {report['scanned']} new files catalogued {report['by_entity']}, "
              f"{report['excluded']} excluded, {report['requires_review']} flagged Requires Review.")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="cerif_scan_dry_run_")
    conn = get_db(tmp_db_path)
    report = _run_scan(conn, env, project_config)
    conn.close()
    print(f"CERIF scan (--dry-run): {report['scanned']} new files WOULD be catalogued {report['by_entity']}, "
          f"{report['excluded']} excluded, {report['requires_review']} would be flagged Requires Review.")
    print(f"Nothing written to {common.display_path(DB_PATH)}. Working copy left at {tmp_db_path}.")


def _run_migrate(conn, project_config: dict) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    changes = []
    rows = conn.execute("SELECT * FROM cerif_catalogue ORDER BY catalogue_id").fetchall()
    for row in rows:
        source_path = Path(row["source_path"])
        if not source_path.exists():
            continue
        source_root = source_path
        for _ in range(len(Path(row["relative_path"]).parts)):
            source_root = source_root.parent
        seq = int(row["catalogue_id"].split("-")[1])
        record = build_record(source_path, source_root, seq, project_config)

        changed = {}
        for field in ("entity_type", "result_type", "confidence_status", "classification_rule"):
            if row[field] != record[field]:
                changed[field] = (row[field], record[field])
        if changed:
            conn.execute(
                "UPDATE cerif_catalogue SET entity_type=?, result_type=?, confidence_status=?, "
                "classification_rule=?, updated_at=? WHERE catalogue_id=?",
                (record["entity_type"], record["result_type"], record["confidence_status"],
                 record["classification_rule"], now, row["catalogue_id"]),
            )
            changes.append({"catalogue_id": row["catalogue_id"], "changed_fields": changed})
    conn.commit()
    return changes


def cmd_migrate(project_config: dict, env: dict, dry_run: bool, apply: bool) -> None:
    if dry_run == apply:
        raise SystemExit("migrate --cerif requires exactly one of --dry-run or --apply")
    if apply:
        conn = get_db()
        changes = _run_migrate(conn, project_config)
        conn.close()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        write_header = not MIGRATION_LOG_CSV.exists()
        with MIGRATION_LOG_CSV.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            if write_header:
                writer.writerow(["timestamp", "catalogue_id", "field", "old_value", "new_value"])
            now = datetime.now(timezone.utc).isoformat()
            for change in changes:
                for field, (old, new) in change["changed_fields"].items():
                    writer.writerow([now, change["catalogue_id"], field, old, new])
        print(f"CERIF migrate (applied): {len(changes)} records reclassified -> {common.display_path(MIGRATION_LOG_CSV)}")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="cerif_migrate_dry_run_")
    conn = get_db(tmp_db_path)
    changes = _run_migrate(conn, project_config)
    conn.close()
    print(f"CERIF migrate (--dry-run): {len(changes)} records WOULD be reclassified.")
    for change in changes[:20]:
        print(f"  {change['catalogue_id']}: {change['changed_fields']}")


def cmd_validate(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --cerif --apply` first.")
        return
    conn = get_db()
    rows = conn.execute("SELECT * FROM cerif_catalogue ORDER BY catalogue_id").fetchall()

    issues = []
    for row in rows:
        if not Path(row["source_path"]).exists():
            issues.append((row["catalogue_id"], "missing_file", row["source_path"]))
        if row["entity_type"] not in ENTITY_TYPES:
            issues.append((row["catalogue_id"], "invalid_entity_type", row["entity_type"]))
        if row["result_type"] not in RESULT_TYPES:
            issues.append((row["catalogue_id"], "invalid_result_type", row["result_type"]))
        elif row["entity_type"] == "cfResPubl" and row["result_type"] not in RESPUBL_RESULT_TYPES:
            issues.append((row["catalogue_id"], "result_type_entity_type_mismatch",
                            f"{row['result_type']} is not a cfResPubl type"))
        elif row["entity_type"] == "cfResProd" and row["result_type"] not in RESPROD_RESULT_TYPES:
            issues.append((row["catalogue_id"], "result_type_entity_type_mismatch",
                            f"{row['result_type']} is not a cfResProd type"))
        if not row["title"]:
            issues.append((row["catalogue_id"], "missing_title", ""))
        if not json.loads(row["person_relations_json"] or "[]"):
            issues.append((row["catalogue_id"], "unresolved_person_relations", ""))
    conn.close()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["catalogue_id", "category", "detail"])
        writer.writerows(issues)

    if issues:
        from collections import Counter
        by_category = Counter(i[1] for i in issues)
        print(f"CERIF validate: {len(issues)} issues across {len(rows)} records -> {common.display_path(MANUAL_REVIEW_CSV)}")
        print(f"By category: {dict(by_category)}")
    else:
        print(f"CERIF validate: {len(rows)} records, no issues found.")


def _row_to_record(row) -> dict:
    record = dict(row)
    for field in LIST_FIELDS:
        record[field] = json.loads(record.pop(f"{field}_json", "[]") or "[]")
    return record


def _record_to_cerif_xml(record: dict) -> str:
    entity = record["entity_type"]
    id_field = f"cf{entity[2:]}Id"  # cfResPublId / cfResProdId
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<{entity} xmlns="urn:xmlns:org:eurocris:cerif-1.6-1" cfClassId="{saxutils.escape(record["result_type"])}">',
        f'  <{id_field}>{saxutils.escape(str(record["cerif_id"]))}</{id_field}>',
        f'  <cfTitle cfLangCode="und">{saxutils.escape(str(record["title"]))}</cfTitle>',
    ]
    if record["abstract"] != UNKNOWN:
        lines.append(f'  <cfAbstr cfLangCode="und">{saxutils.escape(str(record["abstract"]))}</cfAbstr>')
    for person in record["person_relations"]:
        lines.append(
            f'  <cfPers_{entity[2:]} cfClassId="{saxutils.escape(str(person.get("role", "Author")))}" '
            f'cfStartDate="{saxutils.escape(str(person.get("start_date", "")))}">'
            f'{saxutils.escape(str(person.get("name", UNKNOWN)))}</cfPers_{entity[2:]}>'
        )
    for org in record["org_unit_relations"]:
        lines.append(
            f'  <cfOrgUnit_{entity[2:]} cfClassId="{saxutils.escape(str(org.get("role", "Responsible Organisation")))}" '
            f'cfStartDate="{saxutils.escape(str(org.get("start_date", "")))}">'
            f'{saxutils.escape(str(org.get("name", UNKNOWN)))}</cfOrgUnit_{entity[2:]}>'
        )
    for project in record["project_relations"]:
        lines.append(
            f'  <cfProj_{entity[2:]} cfClassId="{saxutils.escape(str(project.get("role", "Output")))}" '
            f'cfStartDate="{saxutils.escape(str(project.get("start_date", "")))}">'
            f'{saxutils.escape(str(project.get("name", UNKNOWN)))}</cfProj_{entity[2:]}>'
        )
    lines.append(f"</{entity}>")
    return "\n".join(lines)


def cmd_export(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --cerif --apply` first.")
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CATALOGUE_XML_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    rows = conn.execute("SELECT * FROM cerif_catalogue ORDER BY catalogue_id").fetchall()
    conn.close()
    records = [_row_to_record(r) for r in rows]

    if records:
        fieldnames = list(records[0].keys())
        with CATALOGUE_CSV.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for record in records:
                writer.writerow({k: (json.dumps(v) if isinstance(v, list) else v) for k, v in record.items()})
    else:
        CATALOGUE_CSV.write_text("", encoding="utf-8")

    CATALOGUE_JSON.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    for record in records:
        xml_path = CATALOGUE_XML_DIR / f"{record['catalogue_id']}.xml"
        xml_path.write_text(_record_to_cerif_xml(record), encoding="utf-8")

    SCHEMA_JSON.write_text(json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "CERIF catalogue record",
        "type": "object",
        "required": ["catalogue_id", "cerif_id", "entity_type", "result_type", "title"],
        "properties": {
            "entity_type": {"type": "string", "enum": ENTITY_TYPES},
            "result_type": {"type": "string", "enum": RESULT_TYPES},
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not MIGRATION_LOG_CSV.exists():
        with MIGRATION_LOG_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["timestamp", "catalogue_id", "field", "old_value", "new_value"])
    if not MANUAL_REVIEW_CSV.exists():
        with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["catalogue_id", "category", "detail"])

    print(f"CERIF export: {len(records)} records -> {common.display_path(OUTPUT_DIR)}/ "
          f"(cerif_catalogue.csv/json, cerif_xml/<id>.xml per record, catalogue_schema.json, "
          f"catalogue_manual_review.csv, catalogue_migration_log.csv)")
