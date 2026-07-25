#!/usr/bin/env python3
"""RO-Crate (Research Object Crate) catalogue mode - activated by --ro-crate.

A fully separate, additive cataloguing pipeline (see catalogue_common.py for
shared plumbing, dsr_catalogue.py for the isolation pattern this follows):
its own database (instance/catalogue_ro_crate.db) and its own output
directory (instance/catalogued_files/ro_crate/). Never opens or writes
instance/catalogue.db or any other standard's outputs.

RO-Crate is structurally different from every other standard here: it isn't
a flat per-file record schema, it's a JSON-LD graph. Its defining artefact
is a single ro-crate-metadata.json manifest per crate root (one crate per
configured SOURCE_DATA_ROOTS entry), containing:

  - a root Dataset entity ("./") describing the crate root itself
  - the metadata-descriptor entity (ro-crate-metadata.json itself, per the
    spec's self-describing convention), conformsTo RO-Crate 1.2
  - one File entity per catalogued file, with name/contentSize/
    encodingFormat/dateModified/sha256 (a common, widely-used extension
    property, not mandated by the base spec but harmless and useful here)
  - Person entities for configured authorship, linked via `author`

Scope limitation, stated rather than silently assumed: hasPart from the root
Dataset is flat (every File entity linked directly), not a nested tree of
intermediate directory-Dataset entities mirroring the real folder structure
- a valid simplification RO-Crate permits, but a simplification nonetheless.

Nothing here invents metadata: `name` defaults to the filename,
`description`/`license` default to "Unknown", and author relations are only
populated from explicitly configured project_config.json fields (mirroring
cerif_catalogue.py's guard against the template's "REPLACE_ME" placeholder).
A <file>.rocrate.json sidecar can supply any field explicitly.
"""
from __future__ import annotations

import csv
import json
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path

from . import catalogue_common as common

DB_PATH = common.INSTANCE_DIR / "catalogue_ro_crate.db"
OUTPUT_DIR = common.CATALOGUE_DIR / "ro_crate"

CATALOGUE_CSV = OUTPUT_DIR / "ro_crate_catalogue.csv"
CATALOGUE_JSON = OUTPUT_DIR / "ro_crate_catalogue.json"
RO_CRATE_METADATA_DIR = OUTPUT_DIR / "crates"
SCHEMA_JSON = OUTPUT_DIR / "catalogue_schema.json"
MANUAL_REVIEW_CSV = OUTPUT_DIR / "catalogue_manual_review.csv"
MIGRATION_LOG_CSV = OUTPUT_DIR / "catalogue_migration_log.csv"

UNKNOWN = "Unknown"
RO_CRATE_CONTEXT = "https://w3id.org/ro/crate/1.2/context"
RO_CRATE_PROFILE = "https://w3id.org/ro/crate/1.2"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ro_crate_catalogue (
    catalogue_id TEXT PRIMARY KEY,
    source_path TEXT UNIQUE,
    source_root TEXT,
    relative_path TEXT,
    file_name TEXT,
    ro_crate_id TEXT,
    entity_type TEXT DEFAULT 'File',
    name TEXT,
    description TEXT DEFAULT 'Unknown',
    content_size INTEGER,
    encoding_format TEXT,
    date_modified TEXT,
    license TEXT DEFAULT 'Unknown',
    author_relations_json TEXT DEFAULT '[]',
    sha256 TEXT,
    explicit_metadata_applied INTEGER DEFAULT 0,
    confidence_status TEXT DEFAULT 'Requires Review',
    notes TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_ro_crate_sha256 ON ro_crate_catalogue(sha256);
CREATE INDEX IF NOT EXISTS idx_ro_crate_source_root ON ro_crate_catalogue(source_root);

CREATE TABLE IF NOT EXISTS ro_crate_counters (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_seq INTEGER NOT NULL DEFAULT 1
);
"""


def get_db(db_path: Path | None = None):
    return common.get_sqlite_db(db_path or DB_PATH, SCHEMA_SQL)


def next_seq(conn) -> int:
    row = conn.execute("SELECT next_seq FROM ro_crate_counters WHERE id = 1").fetchone()
    seq = row["next_seq"] if row else 1
    conn.execute(
        "INSERT INTO ro_crate_counters (id, next_seq) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET next_seq = ?",
        (seq + 1, seq + 1),
    )
    return seq


def load_sidecar_metadata(path: Path) -> dict | None:
    sidecar = path.with_name(path.name + ".rocrate.json")
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def build_record(path: Path, source_root: Path, seq: int, project_config: dict) -> dict:
    stat = path.stat()
    sha256 = common.sha256_file(path)
    sidecar = load_sidecar_metadata(path) or {}

    relative_path = str(path.relative_to(source_root)).replace("\\", "/")
    mime_type, _ = mimetypes.guess_type(path.name)
    modified_iso = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()

    researcher = project_config.get("researcher")
    default_authors = (
        [{"name": researcher}] if researcher and researcher != "REPLACE_ME" else []
    )

    has_explicit = bool(sidecar)
    return {
        "catalogue_id": f"ROCRATE-{seq:05d}",
        "source_path": str(path.resolve()),
        "source_root": str(source_root.resolve()),
        "relative_path": relative_path,
        "file_name": path.name,
        "ro_crate_id": sidecar.get("ro_crate_id", f"./{relative_path}"),
        "entity_type": sidecar.get("entity_type", "File"),
        "name": sidecar.get("name", path.name),
        "description": sidecar.get("description", UNKNOWN),
        "content_size": stat.st_size,
        "encoding_format": sidecar.get("encoding_format", mime_type or UNKNOWN),
        "date_modified": modified_iso,
        "license": sidecar.get("license", UNKNOWN),
        "author_relations": sidecar.get("author_relations", default_authors),
        "sha256": sha256,
        "explicit_metadata_applied": 1 if has_explicit else 0,
        "confidence_status": "Confident" if (has_explicit or mime_type) else "Requires Review",
        "notes": None,
    }


LIST_FIELDS = ("author_relations",)


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
            if path.name.endswith(".rocrate.json"):
                continue
            if common.is_excluded(path, source_root):
                excluded += 1
                continue

            source_path = str(path.resolve())
            existing = conn.execute(
                "SELECT catalogue_id FROM ro_crate_catalogue WHERE source_path = ?", (source_path,)
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
                f"INSERT INTO ro_crate_catalogue ({', '.join(columns)}, created_at, updated_at) "
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
        raise SystemExit("scan --ro-crate requires exactly one of --dry-run or --apply")
    if apply:
        conn = get_db()
        report = _run_scan(conn, env, project_config)
        conn.close()
        print(f"RO-Crate scan (applied): {report['scanned']} new files catalogued, "
              f"{report['excluded']} excluded, {report['requires_review']} flagged Requires Review.")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="ro_crate_scan_dry_run_")
    conn = get_db(tmp_db_path)
    report = _run_scan(conn, env, project_config)
    conn.close()
    print(f"RO-Crate scan (--dry-run): {report['scanned']} new files WOULD be catalogued, "
          f"{report['excluded']} excluded, {report['requires_review']} would be flagged Requires Review.")
    print(f"Nothing written to {common.display_path(DB_PATH)}. Working copy left at {tmp_db_path}.")


def _run_migrate(conn, project_config: dict) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    changes = []
    rows = conn.execute("SELECT * FROM ro_crate_catalogue ORDER BY catalogue_id").fetchall()
    for row in rows:
        source_path = Path(row["source_path"])
        if not source_path.exists():
            continue
        source_root = Path(row["source_root"])
        seq = int(row["catalogue_id"].split("-")[1])
        record = build_record(source_path, source_root, seq, project_config)

        changed = {}
        for field in ("encoding_format", "confidence_status"):
            if row[field] != record[field]:
                changed[field] = (row[field], record[field])
        if changed:
            conn.execute(
                "UPDATE ro_crate_catalogue SET encoding_format=?, confidence_status=?, updated_at=? "
                "WHERE catalogue_id=?",
                (record["encoding_format"], record["confidence_status"], now, row["catalogue_id"]),
            )
            changes.append({"catalogue_id": row["catalogue_id"], "changed_fields": changed})
    conn.commit()
    return changes


def cmd_migrate(project_config: dict, env: dict, dry_run: bool, apply: bool) -> None:
    if dry_run == apply:
        raise SystemExit("migrate --ro-crate requires exactly one of --dry-run or --apply")
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
        print(f"RO-Crate migrate (applied): {len(changes)} records reclassified -> {common.display_path(MIGRATION_LOG_CSV)}")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="ro_crate_migrate_dry_run_")
    conn = get_db(tmp_db_path)
    changes = _run_migrate(conn, project_config)
    conn.close()
    print(f"RO-Crate migrate (--dry-run): {len(changes)} records WOULD be reclassified.")
    for change in changes[:20]:
        print(f"  {change['catalogue_id']}: {change['changed_fields']}")


def cmd_validate(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --ro-crate --apply` first.")
        return
    conn = get_db()
    rows = conn.execute("SELECT * FROM ro_crate_catalogue ORDER BY catalogue_id").fetchall()

    issues = []
    seen_ids: dict[tuple, str] = {}
    for row in rows:
        if not Path(row["source_path"]).exists():
            issues.append((row["catalogue_id"], "missing_file", row["source_path"]))
        if not row["ro_crate_id"].startswith("./"):
            issues.append((row["catalogue_id"], "malformed_ro_crate_id", row["ro_crate_id"]))
        key = (row["source_root"], row["ro_crate_id"])
        if key in seen_ids:
            issues.append((row["catalogue_id"], "duplicate_ro_crate_id_within_crate",
                            f"also used by {seen_ids[key]}"))
        else:
            seen_ids[key] = row["catalogue_id"]
        if row["content_size"] is None or row["content_size"] < 0:
            issues.append((row["catalogue_id"], "invalid_content_size", row["content_size"]))
    conn.close()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["catalogue_id", "category", "detail"])
        writer.writerows(issues)

    if issues:
        from collections import Counter
        by_category = Counter(i[1] for i in issues)
        print(f"RO-Crate validate: {len(issues)} issues across {len(rows)} records -> {common.display_path(MANUAL_REVIEW_CSV)}")
        print(f"By category: {dict(by_category)}")
    else:
        print(f"RO-Crate validate: {len(rows)} records, no issues found.")


def _row_to_record(row) -> dict:
    record = dict(row)
    for field in LIST_FIELDS:
        record[field] = json.loads(record.pop(f"{field}_json", "[]") or "[]")
    return record


def _slugify_person_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "unknown"


def _build_crate_graph(records: list[dict]) -> dict:
    persons: dict[str, dict] = {}
    file_entities = []
    has_part = []

    for record in records:
        author_ids = []
        for author in record["author_relations"]:
            name = author.get("name", UNKNOWN)
            person_id = f"#person-{_slugify_person_name(name)}"
            persons[person_id] = {"@id": person_id, "@type": "Person", "name": name}
            author_ids.append({"@id": person_id})

        entity = {
            "@id": record["ro_crate_id"],
            "@type": record["entity_type"],
            "name": record["name"],
            "contentSize": record["content_size"],
            "encodingFormat": record["encoding_format"],
            "dateModified": record["date_modified"],
            "sha256": record["sha256"],
        }
        if record["description"] != UNKNOWN:
            entity["description"] = record["description"]
        if record["license"] != UNKNOWN:
            entity["license"] = record["license"]
        if author_ids:
            entity["author"] = author_ids if len(author_ids) > 1 else author_ids[0]
        file_entities.append(entity)
        has_part.append({"@id": record["ro_crate_id"]})

    root_dataset = {
        "@id": "./",
        "@type": "Dataset",
        "name": "Research object crate",
        "hasPart": has_part,
    }
    metadata_descriptor = {
        "@id": "ro-crate-metadata.json",
        "@type": "CreativeWork",
        "conformsTo": {"@id": RO_CRATE_PROFILE},
        "about": {"@id": "./"},
    }

    return {
        "@context": RO_CRATE_CONTEXT,
        "@graph": [metadata_descriptor, root_dataset, *file_entities, *persons.values()],
    }


def cmd_export(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --ro-crate --apply` first.")
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    RO_CRATE_METADATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    rows = conn.execute("SELECT * FROM ro_crate_catalogue ORDER BY catalogue_id").fetchall()
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

    by_root: dict[str, list[dict]] = {}
    for record in records:
        by_root.setdefault(record["source_root"], []).append(record)

    crate_count = 0
    for source_root, root_records in by_root.items():
        crate_dir = RO_CRATE_METADATA_DIR / (Path(source_root).name or f"crate-{crate_count}")
        crate_dir.mkdir(parents=True, exist_ok=True)
        crate_path = crate_dir / "ro-crate-metadata.json"
        crate_path.write_text(json.dumps(_build_crate_graph(root_records), ensure_ascii=False, indent=2), encoding="utf-8")
        crate_count += 1

    SCHEMA_JSON.write_text(json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "RO-Crate catalogue record",
        "type": "object",
        "required": ["catalogue_id", "ro_crate_id", "name", "content_size"],
        "properties": {
            "ro_crate_id": {"type": "string", "pattern": r"^\./"},
            "entity_type": {"type": "string", "enum": ["File", "Dataset"]},
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not MIGRATION_LOG_CSV.exists():
        with MIGRATION_LOG_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["timestamp", "catalogue_id", "field", "old_value", "new_value"])
    if not MANUAL_REVIEW_CSV.exists():
        with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["catalogue_id", "category", "detail"])

    print(f"RO-Crate export: {len(records)} records across {crate_count} crate(s) -> "
          f"{common.display_path(OUTPUT_DIR)}/ (ro_crate_catalogue.csv/json, "
          f"crates/<root-name>/ro-crate-metadata.json per crate, catalogue_schema.json, "
          f"catalogue_manual_review.csv, catalogue_migration_log.csv)")
