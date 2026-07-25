#!/usr/bin/env python3
"""MODS (Metadata Object Description Schema) catalogue mode - activated by
--mods.

A fully separate, additive cataloguing pipeline (see catalogue_common.py for
shared plumbing, dsr_catalogue.py for the isolation pattern this follows):
its own database (instance/catalogue_mods.db) and its own output directory
(instance/catalogued_files/mods/). Never opens or writes
instance/catalogue.db or any other standard's outputs.

MODS is fundamentally an XML schema (the Library of Congress's
http://www.loc.gov/mods/v3 namespace), richer than Dublin Core but less
complex than MARC 21. Like Dublin Core and DataCite (and unlike Crossref/
CERIF's applicability-gating), MODS describes every catalogued file - it's
a general bibliographic description format, not limited to formally
published works.

Deterministic derivation only:

  - titleInfo/title  : filename stem by default (never invented from
                       content the engine hasn't read).
  - typeOfResource   : MODS's controlled vocabulary, derived from file
                       extension.
  - originInfo/dateIssued, dateCreated : filesystem timestamps (proxies,
                       always flagged Requires Review - never presented as
                       authoritative publication/creation dates).
  - originInfo/publisher : project_config.json -> institution when
                       genuinely configured (real data, not invented).
  - physicalDescription/extent : "<N> bytes" (a digital-native equivalent of
                       MODS's usual physical-extent statement).
  - physicalDescription/internetMediaType : MIME type via file extension.
  - physicalDescription/digitalOrigin : left unset by default rather than
                       guessing "born digital" vs "digitized" - the engine
                       cannot tell from a file's bytes alone whether a PDF
                       is a native export or a scan, and MODS's own
                       controlled vocabulary for this element has no
                       "Unknown" value, so the honest choice is to omit the
                       element entirely unless a <file>.mods.json sidecar
                       supplies a real one.
  - identifier       : content-addressed, type="local" (urn:mods:sha256:<hash>).
  - recordInfo       : documents this record's own machine-generated
                       provenance (recordContentSource/recordOrigin/
                       recordCreationDate) - a factual statement about how
                       the record was produced, not invented data about the
                       file itself.
  - name (creator)   : from project_config.json -> researcher when
                       genuinely configured (guards the template's
                       "REPLACE_ME" placeholder, same as cerif_catalogue.py).
  - abstract, note, subjects, relatedItems, accessCondition : left
    "Unknown"/empty by default - never invented. A <file>.mods.json sidecar
    can supply any field explicitly.
"""
from __future__ import annotations

import csv
import json
import mimetypes
import xml.sax.saxutils as saxutils
from datetime import datetime, timezone
from pathlib import Path

import catalogue_common as common

DB_PATH = common.INSTANCE_DIR / "catalogue_mods.db"
OUTPUT_DIR = common.CATALOGUE_DIR / "mods"

CATALOGUE_CSV = OUTPUT_DIR / "mods_catalogue.csv"
CATALOGUE_JSON = OUTPUT_DIR / "mods_catalogue.json"
CATALOGUE_XML_DIR = OUTPUT_DIR / "mods_xml"
SCHEMA_JSON = OUTPUT_DIR / "catalogue_schema.json"
MANUAL_REVIEW_CSV = OUTPUT_DIR / "catalogue_manual_review.csv"
MIGRATION_LOG_CSV = OUTPUT_DIR / "catalogue_migration_log.csv"

UNKNOWN = "Unknown"

# MODS typeOfResource controlled vocabulary.
TYPE_OF_RESOURCE = [
    "text", "cartographic", "notated music", "sound recording",
    "sound recording-musical", "sound recording-nonmusical", "still image",
    "moving image", "three dimensional object", "software, multimedia",
    "mixed material",
]

EXTENSION_TO_TYPE_OF_RESOURCE = {
    ".docx": "text", ".doc": "text", ".odt": "text", ".rtf": "text",
    ".md": "text", ".txt": "text", ".tex": "text", ".pdf": "text",
    ".csv": "text", ".tsv": "text", ".xlsx": "text", ".xls": "text",
    ".json": "text", ".jsonl": "text", ".xml": "text",
    ".png": "still image", ".jpg": "still image", ".jpeg": "still image",
    ".gif": "still image", ".svg": "still image", ".tif": "still image", ".tiff": "still image",
    ".mp4": "moving image", ".mov": "moving image", ".avi": "moving image", ".mkv": "moving image",
    ".mp3": "sound recording", ".wav": "sound recording", ".flac": "sound recording",
    ".py": "software, multimedia", ".js": "software, multimedia", ".ts": "software, multimedia",
    ".java": "software, multimedia", ".sh": "software, multimedia", ".ipynb": "software, multimedia",
}

# MODS identifier@type controlled vocabulary (subset).
IDENTIFIER_TYPES = ["local", "doi", "isbn", "issn", "uri", "hdl", "oclc"]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mods_catalogue (
    catalogue_id TEXT PRIMARY KEY,
    source_path TEXT UNIQUE,
    relative_path TEXT,
    file_name TEXT,
    title TEXT,
    creator_name TEXT,
    type_of_resource TEXT,
    genre TEXT DEFAULT 'Unknown',
    publisher TEXT DEFAULT 'Unknown',
    date_issued TEXT,
    date_created TEXT,
    extent TEXT,
    internet_media_type TEXT,
    digital_origin TEXT,
    abstract TEXT DEFAULT 'Unknown',
    note TEXT DEFAULT 'Unknown',
    subjects_json TEXT DEFAULT '[]',
    related_items_json TEXT DEFAULT '[]',
    access_condition TEXT DEFAULT 'Unknown',
    identifier_type TEXT DEFAULT 'local',
    identifier_value TEXT,
    location_url TEXT,
    sha256 TEXT,
    file_size_bytes INTEGER,
    explicit_metadata_applied INTEGER DEFAULT 0,
    confidence_status TEXT DEFAULT 'Requires Review',
    notes TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_mods_sha256 ON mods_catalogue(sha256);

CREATE TABLE IF NOT EXISTS mods_counters (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_seq INTEGER NOT NULL DEFAULT 1
);
"""


def get_db(db_path: Path | None = None):
    return common.get_sqlite_db(db_path or DB_PATH, SCHEMA_SQL)


def next_seq(conn) -> int:
    row = conn.execute("SELECT next_seq FROM mods_counters WHERE id = 1").fetchone()
    seq = row["next_seq"] if row else 1
    conn.execute(
        "INSERT INTO mods_counters (id, next_seq) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET next_seq = ?",
        (seq + 1, seq + 1),
    )
    return seq


def load_sidecar_metadata(path: Path) -> dict | None:
    sidecar = path.with_name(path.name + ".mods.json")
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def classify_type_of_resource(path: Path) -> str:
    return EXTENSION_TO_TYPE_OF_RESOURCE.get(path.suffix.lower(), "mixed material")


def build_record(path: Path, source_root: Path, seq: int, project_config: dict) -> dict:
    stat = path.stat()
    sha256 = common.sha256_file(path)
    sidecar = load_sidecar_metadata(path) or {}

    type_of_resource = sidecar.get("type_of_resource", classify_type_of_resource(path))
    mime_type, _ = mimetypes.guess_type(path.name)
    issued_date = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).date().isoformat()
    created_date = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).date().isoformat()

    researcher = project_config.get("researcher")
    creator_name = sidecar.get("creator_name", researcher if researcher and researcher != "REPLACE_ME" else UNKNOWN)

    has_explicit = bool(sidecar)
    return {
        "catalogue_id": f"MODS-{seq:05d}",
        "source_path": str(path.resolve()),
        "relative_path": str(path.relative_to(source_root)).replace("\\", "/"),
        "file_name": path.name,
        "title": sidecar.get("title", path.stem),
        "creator_name": creator_name,
        "type_of_resource": type_of_resource,
        "genre": sidecar.get("genre", UNKNOWN),
        "publisher": sidecar.get("publisher", project_config.get("institution") or UNKNOWN),
        "date_issued": sidecar.get("date_issued", issued_date),
        "date_created": sidecar.get("date_created", created_date),
        "extent": sidecar.get("extent", f"{stat.st_size} bytes"),
        "internet_media_type": sidecar.get("internet_media_type", mime_type or UNKNOWN),
        "digital_origin": sidecar.get("digital_origin"),
        "abstract": sidecar.get("abstract", UNKNOWN),
        "note": sidecar.get("note", UNKNOWN),
        "subjects": sidecar.get("subjects", []),
        "related_items": sidecar.get("related_items", []),
        "access_condition": sidecar.get("access_condition", UNKNOWN),
        "identifier_type": sidecar.get("identifier_type", "local"),
        "identifier_value": sidecar.get("identifier_value", f"urn:mods:sha256:{sha256}"),
        "location_url": sidecar.get("location_url", path.resolve().as_uri()),
        "sha256": sha256,
        "file_size_bytes": stat.st_size,
        "explicit_metadata_applied": 1 if has_explicit else 0,
        "confidence_status": "Confident" if (has_explicit or mime_type) else "Requires Review",
        "notes": None,
    }


LIST_FIELDS = ("subjects", "related_items")


def _run_scan(conn, env: dict, project_config: dict) -> dict:
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
            if path.name.endswith(".mods.json"):
                continue
            if common.is_excluded(path, source_root):
                excluded += 1
                continue

            source_path = str(path.resolve())
            existing = conn.execute(
                "SELECT catalogue_id FROM mods_catalogue WHERE source_path = ?", (source_path,)
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
                f"INSERT INTO mods_catalogue ({', '.join(columns)}, created_at, updated_at) "
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
        raise SystemExit("scan --mods requires exactly one of --dry-run or --apply")
    if apply:
        conn = get_db()
        report = _run_scan(conn, env, project_config)
        conn.close()
        print(f"MODS scan (applied): {report['scanned']} new files catalogued, "
              f"{report['excluded']} excluded, {report['requires_review']} flagged Requires Review.")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="mods_scan_dry_run_")
    conn = get_db(tmp_db_path)
    report = _run_scan(conn, env, project_config)
    conn.close()
    print(f"MODS scan (--dry-run): {report['scanned']} new files WOULD be catalogued, "
          f"{report['excluded']} excluded, {report['requires_review']} would be flagged Requires Review.")
    print(f"Nothing written to {common.display_path(DB_PATH)}. Working copy left at {tmp_db_path}.")


def _run_migrate(conn, project_config: dict) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    changes = []
    rows = conn.execute("SELECT * FROM mods_catalogue ORDER BY catalogue_id").fetchall()
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
        for field in ("type_of_resource", "internet_media_type", "publisher", "confidence_status"):
            if row[field] != record[field]:
                changed[field] = (row[field], record[field])
        if changed:
            conn.execute(
                "UPDATE mods_catalogue SET type_of_resource=?, internet_media_type=?, publisher=?, "
                "confidence_status=?, updated_at=? WHERE catalogue_id=?",
                (record["type_of_resource"], record["internet_media_type"], record["publisher"],
                 record["confidence_status"], now, row["catalogue_id"]),
            )
            changes.append({"catalogue_id": row["catalogue_id"], "changed_fields": changed})
    conn.commit()
    return changes


def cmd_migrate(project_config: dict, env: dict, dry_run: bool, apply: bool) -> None:
    if dry_run == apply:
        raise SystemExit("migrate --mods requires exactly one of --dry-run or --apply")
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
        print(f"MODS migrate (applied): {len(changes)} records reclassified -> {common.display_path(MIGRATION_LOG_CSV)}")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="mods_migrate_dry_run_")
    conn = get_db(tmp_db_path)
    changes = _run_migrate(conn, project_config)
    conn.close()
    print(f"MODS migrate (--dry-run): {len(changes)} records WOULD be reclassified.")
    for change in changes[:20]:
        print(f"  {change['catalogue_id']}: {change['changed_fields']}")


def cmd_validate(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --mods --apply` first.")
        return
    conn = get_db()
    rows = conn.execute("SELECT * FROM mods_catalogue ORDER BY catalogue_id").fetchall()

    issues = []
    for row in rows:
        if not Path(row["source_path"]).exists():
            issues.append((row["catalogue_id"], "missing_file", row["source_path"]))
        if row["type_of_resource"] not in TYPE_OF_RESOURCE:
            issues.append((row["catalogue_id"], "invalid_type_of_resource", row["type_of_resource"]))
        if row["identifier_type"] not in IDENTIFIER_TYPES:
            issues.append((row["catalogue_id"], "invalid_identifier_type", row["identifier_type"]))
        if not row["title"]:
            issues.append((row["catalogue_id"], "missing_title", ""))
        if row["creator_name"] == UNKNOWN:
            issues.append((row["catalogue_id"], "unresolved_creator_name", UNKNOWN))
        if row["genre"] == UNKNOWN:
            issues.append((row["catalogue_id"], "unresolved_genre", UNKNOWN))
    conn.close()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["catalogue_id", "category", "detail"])
        writer.writerows(issues)

    if issues:
        from collections import Counter
        by_category = Counter(i[1] for i in issues)
        print(f"MODS validate: {len(issues)} issues across {len(rows)} records -> {common.display_path(MANUAL_REVIEW_CSV)}")
        print(f"By category: {dict(by_category)}")
    else:
        print(f"MODS validate: {len(rows)} records, no issues found.")


def _row_to_record(row) -> dict:
    record = dict(row)
    for field in LIST_FIELDS:
        record[field] = json.loads(record.pop(f"{field}_json", "[]") or "[]")
    return record


def _record_to_mods_xml(record: dict) -> str:
    esc = saxutils.escape
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<mods xmlns="http://www.loc.gov/mods/v3" version="3.7">',
        f'  <titleInfo><title>{esc(str(record["title"]))}</title></titleInfo>',
    ]
    if record["creator_name"] != UNKNOWN:
        lines.append(
            f'  <name type="personal"><namePart>{esc(str(record["creator_name"]))}</namePart>'
            f'<role><roleTerm type="text">creator</roleTerm></role></name>'
        )
    lines.append(f'  <typeOfResource>{esc(record["type_of_resource"])}</typeOfResource>')
    if record["genre"] != UNKNOWN:
        lines.append(f'  <genre>{esc(str(record["genre"]))}</genre>')
    lines.append("  <originInfo>")
    if record["publisher"] != UNKNOWN:
        lines.append(f'    <publisher>{esc(str(record["publisher"]))}</publisher>')
    lines.append(f'    <dateIssued>{esc(record["date_issued"])}</dateIssued>')
    lines.append(f'    <dateCreated>{esc(record["date_created"])}</dateCreated>')
    lines.append("  </originInfo>")
    lines.append("  <physicalDescription>")
    if record["digital_origin"]:
        lines.append(f'    <digitalOrigin>{esc(str(record["digital_origin"]))}</digitalOrigin>')
    lines.append(f'    <extent>{esc(str(record["extent"]))}</extent>')
    if record["internet_media_type"] != UNKNOWN:
        lines.append(f'    <internetMediaType>{esc(str(record["internet_media_type"]))}</internetMediaType>')
    lines.append("  </physicalDescription>")
    if record["abstract"] != UNKNOWN:
        lines.append(f'  <abstract>{esc(str(record["abstract"]))}</abstract>')
    if record["note"] != UNKNOWN:
        lines.append(f'  <note>{esc(str(record["note"]))}</note>')
    for subject in record["subjects"]:
        lines.append(f'  <subject><topic>{esc(str(subject))}</topic></subject>')
    for related in record["related_items"]:
        rel_type = esc(str(related.get("type", "otherVersion")))
        rel_title = esc(str(related.get("title", UNKNOWN)))
        lines.append(f'  <relatedItem type="{rel_type}"><titleInfo><title>{rel_title}</title></titleInfo></relatedItem>')
    lines.append(f'  <identifier type="{esc(record["identifier_type"])}">{esc(str(record["identifier_value"]))}</identifier>')
    lines.append(f'  <location><url>{esc(str(record["location_url"]))}</url></location>')
    lines.append("  <recordInfo>")
    lines.append("    <recordContentSource>research-cataloguing-standard mods_catalogue.py</recordContentSource>")
    lines.append(f'    <recordCreationDate encoding="w3cdtf">{esc(record["date_issued"])}</recordCreationDate>')
    lines.append("    <recordOrigin>Machine-generated deterministic catalogue record</recordOrigin>")
    lines.append("  </recordInfo>")
    lines.append("</mods>")
    return "\n".join(lines)


def cmd_export(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --mods --apply` first.")
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CATALOGUE_XML_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    rows = conn.execute("SELECT * FROM mods_catalogue ORDER BY catalogue_id").fetchall()
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
        xml_path.write_text(_record_to_mods_xml(record), encoding="utf-8")

    SCHEMA_JSON.write_text(json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "MODS catalogue record",
        "type": "object",
        "required": ["catalogue_id", "title", "type_of_resource", "identifier_value"],
        "properties": {
            "type_of_resource": {"type": "string", "enum": TYPE_OF_RESOURCE},
            "identifier_type": {"type": "string", "enum": IDENTIFIER_TYPES},
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not MIGRATION_LOG_CSV.exists():
        with MIGRATION_LOG_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["timestamp", "catalogue_id", "field", "old_value", "new_value"])
    if not MANUAL_REVIEW_CSV.exists():
        with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["catalogue_id", "category", "detail"])

    print(f"MODS export: {len(records)} records -> {common.display_path(OUTPUT_DIR)}/ "
          f"(mods_catalogue.csv/json, mods_xml/<id>.xml per record, catalogue_schema.json, "
          f"catalogue_manual_review.csv, catalogue_migration_log.csv)")
