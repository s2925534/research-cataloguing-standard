#!/usr/bin/env python3
"""Dublin Core Metadata Terms catalogue mode - activated by --dublin-core.

A fully separate, additive cataloguing pipeline (see catalogue_common.py for
the shared plumbing, and dsr_catalogue.py for the isolation pattern this
follows): its own database (instance/catalogue_dublin_core.db) and its own
output directory (instance/catalogued_files/dublin_core/). Never opens or
writes instance/catalogue.db, instance/catalogue_dsr.db, or any other
standard's outputs.

Models the 15 DCMES elements (Dublin Core Metadata Element Set) plus the
handful of DCTERMS refinements most commonly used alongside them (created,
modified, extent, isPartOf, hasPart, isVersionOf, hasVersion, conformsTo,
license, accessRights). Unlike DSR, Dublin Core is a flat description
vocabulary, not a classification taxonomy - there is no artefact-type
decision tree here, only deterministic per-element derivation:

  - identifier : urn:sha256:<hash> (content-addressed, stable across
                 renames/moves since it depends only on file content)
  - title      : filename stem (the only title deterministically available
                 without inventing one from content the engine hasn't read)
  - type       : DCMI Type Vocabulary, derived from file extension
  - format     : IANA MIME type, derived from file extension
  - date/created/modified : filesystem timestamps
  - extent     : file size in bytes
  - creator, subject, description, publisher, contributor, source, language,
    relation, coverage, rights, isPartOf, hasPart, isVersionOf, hasVersion,
    conformsTo, license, accessRights : left "Unknown" / empty by default -
    never invented. A <file>.dcmeta.json sidecar can supply any of these
    explicitly (Step 4 in dsr_catalogue.py's terms - explicit metadata beats
    deterministic defaults, but nothing here is ever guessed).
"""
from __future__ import annotations

import csv
import json
import mimetypes
import xml.sax.saxutils as saxutils
from datetime import datetime, timezone
from pathlib import Path

from . import catalogue_common as common

DB_PATH = common.INSTANCE_DIR / "catalogue_dublin_core.db"
OUTPUT_DIR = common.CATALOGUE_DIR / "dublin_core"

CATALOGUE_CSV = OUTPUT_DIR / "dublin_core_catalogue.csv"
CATALOGUE_JSON = OUTPUT_DIR / "dublin_core_catalogue.json"
CATALOGUE_XML = OUTPUT_DIR / "dublin_core_catalogue.xml"
SCHEMA_JSON = OUTPUT_DIR / "catalogue_schema.json"
MANUAL_REVIEW_CSV = OUTPUT_DIR / "catalogue_manual_review.csv"
MIGRATION_LOG_CSV = OUTPUT_DIR / "catalogue_migration_log.csv"

UNKNOWN = "Unknown"

# DCMI Type Vocabulary (the controlled vocabulary dc:type should draw from).
DCMI_TYPES = [
    "Collection", "Dataset", "Event", "Image", "InteractiveResource",
    "MovingImage", "PhysicalObject", "Service", "Software", "Sound",
    "StillImage", "Text",
]

EXTENSION_TO_DCMI_TYPE = {
    ".docx": "Text", ".doc": "Text", ".odt": "Text", ".rtf": "Text",
    ".md": "Text", ".txt": "Text", ".tex": "Text", ".pdf": "Text",
    ".csv": "Dataset", ".tsv": "Dataset", ".xlsx": "Dataset", ".xls": "Dataset",
    ".json": "Dataset", ".jsonl": "Dataset", ".xml": "Dataset", ".parquet": "Dataset",
    ".sqlite": "Dataset", ".db": "Dataset",
    ".png": "StillImage", ".jpg": "StillImage", ".jpeg": "StillImage",
    ".gif": "StillImage", ".svg": "StillImage", ".tif": "StillImage", ".tiff": "StillImage",
    ".mp4": "MovingImage", ".mov": "MovingImage", ".avi": "MovingImage", ".mkv": "MovingImage",
    ".mp3": "Sound", ".wav": "Sound", ".flac": "Sound", ".m4a": "Sound",
    ".py": "Software", ".js": "Software", ".ts": "Software", ".java": "Software",
    ".sh": "Software", ".ipynb": "Software", ".html": "InteractiveResource", ".htm": "InteractiveResource",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dublin_core_catalogue (
    catalogue_id TEXT PRIMARY KEY,
    source_path TEXT UNIQUE,
    relative_path TEXT,
    file_name TEXT,
    dc_identifier TEXT,
    dc_title TEXT,
    dc_creator TEXT DEFAULT 'Unknown',
    dc_subject_json TEXT DEFAULT '[]',
    dc_description TEXT DEFAULT 'Unknown',
    dc_publisher TEXT DEFAULT 'Unknown',
    dc_contributor TEXT DEFAULT 'Unknown',
    dc_date TEXT,
    dc_type TEXT,
    dc_format TEXT,
    dc_source TEXT DEFAULT 'Unknown',
    dc_language TEXT DEFAULT 'Unknown',
    dc_relation_json TEXT DEFAULT '[]',
    dc_coverage TEXT DEFAULT 'Unknown',
    dc_rights TEXT DEFAULT 'Unknown',
    dcterms_created TEXT,
    dcterms_modified TEXT,
    dcterms_extent TEXT,
    dcterms_is_part_of TEXT,
    dcterms_has_part_json TEXT DEFAULT '[]',
    dcterms_is_version_of TEXT,
    dcterms_has_version TEXT,
    dcterms_conforms_to TEXT,
    dcterms_license TEXT DEFAULT 'Unknown',
    dcterms_access_rights TEXT DEFAULT 'Unknown',
    sha256 TEXT,
    file_size_bytes INTEGER,
    explicit_metadata_applied INTEGER DEFAULT 0,
    confidence_status TEXT DEFAULT 'Requires Review',
    notes TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_dc_sha256 ON dublin_core_catalogue(sha256);

CREATE TABLE IF NOT EXISTS dublin_core_counters (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_seq INTEGER NOT NULL DEFAULT 1
);
"""


def get_db(db_path: Path | None = None):
    return common.get_sqlite_db(db_path or DB_PATH, SCHEMA_SQL)


def next_seq(conn) -> int:
    row = conn.execute("SELECT next_seq FROM dublin_core_counters WHERE id = 1").fetchone()
    seq = row["next_seq"] if row else 1
    conn.execute(
        "INSERT INTO dublin_core_counters (id, next_seq) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET next_seq = ?",
        (seq + 1, seq + 1),
    )
    return seq


def load_sidecar_metadata(path: Path) -> dict | None:
    sidecar = path.with_name(path.name + ".dcmeta.json")
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def classify_type(path: Path) -> str:
    return EXTENSION_TO_DCMI_TYPE.get(path.suffix.lower(), UNKNOWN)


def guess_format(path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(path.name)
    return mime_type or UNKNOWN


def build_record(path: Path, source_root: Path, seq: int) -> dict:
    stat = path.stat()
    sha256 = common.sha256_file(path)
    sidecar = load_sidecar_metadata(path) or {}

    dc_type = sidecar.get("dc_type", classify_type(path))
    modified_iso = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()

    record = {
        "catalogue_id": f"DC-{seq:05d}",
        "source_path": str(path.resolve()),
        "relative_path": str(path.relative_to(source_root)),
        "file_name": path.name,
        "dc_identifier": sidecar.get("dc_identifier", f"urn:sha256:{sha256}"),
        "dc_title": sidecar.get("dc_title", path.stem),
        "dc_creator": sidecar.get("dc_creator", UNKNOWN),
        "dc_subject": sidecar.get("dc_subject", []),
        "dc_description": sidecar.get("dc_description", UNKNOWN),
        "dc_publisher": sidecar.get("dc_publisher", UNKNOWN),
        "dc_contributor": sidecar.get("dc_contributor", UNKNOWN),
        "dc_date": sidecar.get("dc_date", modified_iso),
        "dc_type": dc_type,
        "dc_format": sidecar.get("dc_format", guess_format(path)),
        "dc_source": sidecar.get("dc_source", UNKNOWN),
        "dc_language": sidecar.get("dc_language", UNKNOWN),
        "dc_relation": sidecar.get("dc_relation", []),
        "dc_coverage": sidecar.get("dc_coverage", UNKNOWN),
        "dc_rights": sidecar.get("dc_rights", UNKNOWN),
        "dcterms_created": sidecar.get("dcterms_created", modified_iso),
        "dcterms_modified": modified_iso,
        "dcterms_extent": f"{stat.st_size} bytes",
        "dcterms_is_part_of": sidecar.get("dcterms_is_part_of"),
        "dcterms_has_part": sidecar.get("dcterms_has_part", []),
        "dcterms_is_version_of": sidecar.get("dcterms_is_version_of"),
        "dcterms_has_version": sidecar.get("dcterms_has_version"),
        "dcterms_conforms_to": sidecar.get("dcterms_conforms_to"),
        "dcterms_license": sidecar.get("dcterms_license", UNKNOWN),
        "dcterms_access_rights": sidecar.get("dcterms_access_rights", UNKNOWN),
        "sha256": sha256,
        "file_size_bytes": stat.st_size,
        "explicit_metadata_applied": 1 if sidecar else 0,
        "confidence_status": "Confident" if (sidecar or dc_type != UNKNOWN) else "Requires Review",
    }
    return record


def _run_scan(conn, env: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    scanned = 0
    excluded = 0
    requires_review = 0

    for source_root in common.source_roots_from_env(env):
        if not source_root.exists():
            print(f"WARNING: source root does not exist, skipping: {source_root}")
            continue
        for path in source_root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            if path.name.endswith(".dcmeta.json"):
                continue
            if common.is_excluded(path, source_root):
                excluded += 1
                continue

            source_path = str(path.resolve())
            existing = conn.execute(
                "SELECT catalogue_id, sha256 FROM dublin_core_catalogue WHERE source_path = ?",
                (source_path,),
            ).fetchone()
            if existing:
                continue

            seq = next_seq(conn)
            record = build_record(path, source_root, seq)
            columns = list(record.keys())
            values = []
            for col in columns:
                val = record[col]
                if isinstance(val, list):
                    val = json.dumps(val)
                values.append(val)
            placeholders = ", ".join("?" for _ in columns)
            col_sql = ", ".join(
                f"{c}_json" if isinstance(record[c], list) else c for c in columns
            )
            conn.execute(
                f"INSERT INTO dublin_core_catalogue ({col_sql}, created_at, updated_at) "
                f"VALUES ({placeholders}, ?, ?)",
                (*values, now, now),
            )
            scanned += 1
            if record["confidence_status"] == "Requires Review":
                requires_review += 1
            if scanned % 200 == 0:
                conn.commit()

    conn.commit()
    return {"scanned": scanned, "excluded": excluded, "requires_review": requires_review}


def cmd_scan(project_config: dict, env: dict, dry_run: bool, apply: bool) -> None:
    if dry_run == apply:
        raise SystemExit("scan --dublin-core requires exactly one of --dry-run or --apply")
    if apply:
        conn = get_db()
        report = _run_scan(conn, env)
        conn.close()
        print(f"Dublin Core scan (applied): {report['scanned']} new files catalogued, "
              f"{report['excluded']} excluded, {report['requires_review']} flagged Requires Review.")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="dublin_core_scan_dry_run_")
    conn = get_db(tmp_db_path)
    report = _run_scan(conn, env)
    conn.close()
    print(f"Dublin Core scan (--dry-run): {report['scanned']} new files WOULD be catalogued, "
          f"{report['excluded']} excluded, {report['requires_review']} would be flagged Requires Review.")
    print(f"Nothing written to {common.display_path(DB_PATH)}. Working copy left at {tmp_db_path}.")


def _run_migrate(conn) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    changes = []
    rows = conn.execute("SELECT * FROM dublin_core_catalogue ORDER BY catalogue_id").fetchall()
    for row in rows:
        source_path = Path(row["source_path"])
        if not source_path.exists():
            continue
        source_root = source_path
        for _ in range(len(Path(row["relative_path"]).parts)):
            source_root = source_root.parent
        record = build_record(source_path, source_root, int(row["catalogue_id"].split("-")[1]))

        changed = {}
        for field in ("dc_type", "dc_format", "dc_identifier", "confidence_status"):
            if row[field] != record[field]:
                changed[field] = (row[field], record[field])
        if changed:
            conn.execute(
                "UPDATE dublin_core_catalogue SET dc_type=?, dc_format=?, dc_identifier=?, "
                "confidence_status=?, updated_at=? WHERE catalogue_id=?",
                (record["dc_type"], record["dc_format"], record["dc_identifier"],
                 record["confidence_status"], now, row["catalogue_id"]),
            )
            changes.append({"catalogue_id": row["catalogue_id"], "changed_fields": changed})
    conn.commit()
    return changes


def cmd_migrate(project_config: dict, env: dict, dry_run: bool, apply: bool) -> None:
    if dry_run == apply:
        raise SystemExit("migrate --dublin-core requires exactly one of --dry-run or --apply")
    if apply:
        conn = get_db()
        changes = _run_migrate(conn)
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
        print(f"Dublin Core migrate (applied): {len(changes)} records reclassified -> {common.display_path(MIGRATION_LOG_CSV)}")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="dublin_core_migrate_dry_run_")
    conn = get_db(tmp_db_path)
    changes = _run_migrate(conn)
    conn.close()
    print(f"Dublin Core migrate (--dry-run): {len(changes)} records WOULD be reclassified.")
    for change in changes[:20]:
        print(f"  {change['catalogue_id']}: {change['changed_fields']}")


def cmd_validate(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --dublin-core --apply` first.")
        return
    conn = get_db()
    rows = conn.execute("SELECT * FROM dublin_core_catalogue ORDER BY catalogue_id").fetchall()

    issues = []
    seen_identifiers: dict[str, str] = {}
    for row in rows:
        if not Path(row["source_path"]).exists():
            issues.append((row["catalogue_id"], "missing_file", row["source_path"]))
        if row["dc_type"] not in DCMI_TYPES and row["dc_type"] != UNKNOWN:
            issues.append((row["catalogue_id"], "invalid_dc_type", row["dc_type"]))
        if not row["dc_identifier"]:
            issues.append((row["catalogue_id"], "missing_identifier", ""))
        elif row["dc_identifier"] in seen_identifiers:
            issues.append((row["catalogue_id"], "duplicate_identifier",
                            f"also used by {seen_identifiers[row['dc_identifier']]}"))
        else:
            seen_identifiers[row["dc_identifier"]] = row["catalogue_id"]
        for recommended in ("dc_creator", "dc_description", "dc_rights"):
            if row[recommended] == UNKNOWN:
                issues.append((row["catalogue_id"], f"unresolved_{recommended}", UNKNOWN))
    conn.close()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["catalogue_id", "category", "detail"])
        writer.writerows(issues)

    if issues:
        from collections import Counter
        by_category = Counter(i[1] for i in issues)
        print(f"Dublin Core validate: {len(issues)} issues across {len(rows)} records -> {common.display_path(MANUAL_REVIEW_CSV)}")
        print(f"By category: {dict(by_category)}")
    else:
        print(f"Dublin Core validate: {len(rows)} records, no issues found.")


def _row_to_record(row) -> dict:
    record = dict(row)
    for field in ("dc_subject", "dc_relation", "dcterms_has_part"):
        record[field] = json.loads(record.pop(f"{field}_json", "[]") or "[]")
    return record


def _write_oai_dc_xml(records: list[dict]) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<records>"]
    simple_fields = [
        "title", "creator", "subject", "description", "publisher", "contributor",
        "date", "type", "format", "identifier", "source", "language", "relation",
        "coverage", "rights",
    ]
    for record in records:
        lines.append(
            '  <oai_dc:dc xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        )
        for field in simple_fields:
            value = record.get(f"dc_{field}")
            if isinstance(value, list):
                for item in value:
                    lines.append(f"    <dc:{field}>{saxutils.escape(str(item))}</dc:{field}>")
            elif value:
                lines.append(f"    <dc:{field}>{saxutils.escape(str(value))}</dc:{field}>")
        lines.append("  </oai_dc:dc>")
    lines.append("</records>")
    return "\n".join(lines)


def cmd_export(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --dublin-core --apply` first.")
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    rows = conn.execute("SELECT * FROM dublin_core_catalogue ORDER BY catalogue_id").fetchall()
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
    CATALOGUE_XML.write_text(_write_oai_dc_xml(records), encoding="utf-8")

    SCHEMA_JSON.write_text(json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Dublin Core catalogue record",
        "type": "object",
        "required": ["catalogue_id", "dc_identifier", "dc_title", "dc_type"],
        "properties": {
            "dc_identifier": {"type": "string"},
            "dc_title": {"type": "string"},
            "dc_type": {"type": "string", "enum": DCMI_TYPES + [UNKNOWN]},
            "dc_format": {"type": "string"},
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not MIGRATION_LOG_CSV.exists():
        with MIGRATION_LOG_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["timestamp", "catalogue_id", "field", "old_value", "new_value"])
    if not MANUAL_REVIEW_CSV.exists():
        with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["catalogue_id", "category", "detail"])

    print(f"Dublin Core export: {len(records)} records -> {common.display_path(OUTPUT_DIR)}/ "
          f"(dublin_core_catalogue.csv/json/xml, catalogue_schema.json, catalogue_manual_review.csv, "
          f"catalogue_migration_log.csv)")
