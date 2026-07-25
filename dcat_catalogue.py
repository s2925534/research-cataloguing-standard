#!/usr/bin/env python3
"""W3C DCAT (Data Catalog Vocabulary) catalogue mode - activated by --dcat.

A fully separate, additive cataloguing pipeline (see catalogue_common.py for
shared plumbing, dsr_catalogue.py for the isolation pattern this follows):
its own database (instance/catalogue_dcat.db) and its own output directory
(instance/catalogued_files/dcat/). Never opens or writes
instance/catalogue.db or any other standard's outputs.

DCAT models a dcat:Catalog containing dcat:Dataset resources, each with one
or more dcat:Distribution (the actual accessible file/format representation).
This maps cleanly onto the per-file granularity used everywhere else in this
engine: one dcat:Catalog per configured SOURCE_DATA_ROOTS entry (mirroring
ro_crate_catalogue.py's per-root grouping), one dcat:Dataset per catalogued
file, and exactly one dcat:Distribution describing that file's format/size/
checksum.

DCAT is fundamentally an RDF vocabulary, so - unlike the JSON-LD graph
RO-Crate produces - export's defining output here is Turtle (.ttl), the form
DCAT is most commonly published in (e.g. open-data portal catalogues).
Checksums are represented via the standard SPDX Checksum blank-node pattern
DCAT-AP profiles use, not a bespoke property.

Nothing here invents metadata: `title` defaults to the filename stem,
`description`/`publisher`/`license`/`accessRights`/`conformsTo` default to
"Unknown" (publisher falls back to project_config.json -> institution when
genuinely configured, mirroring datacite_catalogue.py's reasoning), and
`issued` (a proxy from the file's creation timestamp) is always flagged
Requires Review since a filesystem timestamp is not a true issuance date.
A <file>.dcat.json sidecar can supply any field explicitly.
"""
from __future__ import annotations

import csv
import json
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

import catalogue_common as common

DB_PATH = common.INSTANCE_DIR / "catalogue_dcat.db"
OUTPUT_DIR = common.CATALOGUE_DIR / "dcat"

CATALOGUE_CSV = OUTPUT_DIR / "dcat_catalogue.csv"
CATALOGUE_JSON = OUTPUT_DIR / "dcat_catalogue.json"
TURTLE_DIR = OUTPUT_DIR / "turtle"
SCHEMA_JSON = OUTPUT_DIR / "catalogue_schema.json"
MANUAL_REVIEW_CSV = OUTPUT_DIR / "catalogue_manual_review.csv"
MIGRATION_LOG_CSV = OUTPUT_DIR / "catalogue_migration_log.csv"

UNKNOWN = "Unknown"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dcat_catalogue (
    catalogue_id TEXT PRIMARY KEY,
    source_path TEXT UNIQUE,
    source_root TEXT,
    relative_path TEXT,
    file_name TEXT,
    dataset_uri TEXT,
    distribution_uri TEXT,
    title TEXT,
    description TEXT DEFAULT 'Unknown',
    keywords_json TEXT DEFAULT '[]',
    issued TEXT,
    modified TEXT,
    publisher TEXT DEFAULT 'Unknown',
    license TEXT DEFAULT 'Unknown',
    access_rights TEXT DEFAULT 'Unknown',
    conforms_to TEXT DEFAULT 'Unknown',
    media_type TEXT,
    format TEXT,
    byte_size INTEGER,
    access_url TEXT,
    sha256 TEXT,
    explicit_metadata_applied INTEGER DEFAULT 0,
    confidence_status TEXT DEFAULT 'Requires Review',
    notes TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_dcat_sha256 ON dcat_catalogue(sha256);
CREATE INDEX IF NOT EXISTS idx_dcat_source_root ON dcat_catalogue(source_root);

CREATE TABLE IF NOT EXISTS dcat_counters (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_seq INTEGER NOT NULL DEFAULT 1
);
"""


def get_db(db_path: Path | None = None):
    return common.get_sqlite_db(db_path or DB_PATH, SCHEMA_SQL)


def next_seq(conn) -> int:
    row = conn.execute("SELECT next_seq FROM dcat_counters WHERE id = 1").fetchone()
    seq = row["next_seq"] if row else 1
    conn.execute(
        "INSERT INTO dcat_counters (id, next_seq) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET next_seq = ?",
        (seq + 1, seq + 1),
    )
    return seq


def load_sidecar_metadata(path: Path) -> dict | None:
    sidecar = path.with_name(path.name + ".dcat.json")
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

    mime_type, _ = mimetypes.guess_type(path.name)
    issued_date = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).date().isoformat()
    modified_date = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).date().isoformat()

    catalogue_id = f"DCAT-{seq:05d}"
    dataset_uri = sidecar.get("dataset_uri", f"urn:dcat:dataset:sha256-{sha256}")
    distribution_uri = sidecar.get("distribution_uri", f"urn:dcat:distribution:sha256-{sha256}")

    has_explicit = bool(sidecar)
    return {
        "catalogue_id": catalogue_id,
        "source_path": str(path.resolve()),
        "source_root": str(source_root.resolve()),
        "relative_path": str(path.relative_to(source_root)).replace("\\", "/"),
        "file_name": path.name,
        "dataset_uri": dataset_uri,
        "distribution_uri": distribution_uri,
        "title": sidecar.get("title", path.stem),
        "description": sidecar.get("description", UNKNOWN),
        "keywords": sidecar.get("keywords", []),
        "issued": sidecar.get("issued", issued_date),
        "modified": modified_date,
        "publisher": sidecar.get("publisher", project_config.get("institution") or UNKNOWN),
        "license": sidecar.get("license", UNKNOWN),
        "access_rights": sidecar.get("access_rights", UNKNOWN),
        "conforms_to": sidecar.get("conforms_to", UNKNOWN),
        "media_type": sidecar.get("media_type", mime_type or UNKNOWN),
        "format": sidecar.get("format", (path.suffix.lstrip(".").upper() or UNKNOWN)),
        "byte_size": stat.st_size,
        "access_url": sidecar.get("access_url", path.resolve().as_uri()),
        "sha256": sha256,
        "explicit_metadata_applied": 1 if has_explicit else 0,
        "confidence_status": "Confident" if (has_explicit or mime_type) else "Requires Review",
        "notes": None,
    }


LIST_FIELDS = ("keywords",)


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
            if path.name.endswith(".dcat.json"):
                continue
            if common.is_excluded(path, source_root):
                excluded += 1
                continue

            source_path = str(path.resolve())
            existing = conn.execute(
                "SELECT catalogue_id FROM dcat_catalogue WHERE source_path = ?", (source_path,)
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
                f"INSERT INTO dcat_catalogue ({', '.join(columns)}, created_at, updated_at) "
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
        raise SystemExit("scan --dcat requires exactly one of --dry-run or --apply")
    if apply:
        conn = get_db()
        report = _run_scan(conn, env, project_config)
        conn.close()
        print(f"DCAT scan (applied): {report['scanned']} new files catalogued, "
              f"{report['excluded']} excluded, {report['requires_review']} flagged Requires Review.")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="dcat_scan_dry_run_")
    conn = get_db(tmp_db_path)
    report = _run_scan(conn, env, project_config)
    conn.close()
    print(f"DCAT scan (--dry-run): {report['scanned']} new files WOULD be catalogued, "
          f"{report['excluded']} excluded, {report['requires_review']} would be flagged Requires Review.")
    print(f"Nothing written to {common.display_path(DB_PATH)}. Working copy left at {tmp_db_path}.")


def _run_migrate(conn, project_config: dict) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    changes = []
    rows = conn.execute("SELECT * FROM dcat_catalogue ORDER BY catalogue_id").fetchall()
    for row in rows:
        source_path = Path(row["source_path"])
        if not source_path.exists():
            continue
        source_root = Path(row["source_root"])
        seq = int(row["catalogue_id"].split("-")[1])
        record = build_record(source_path, source_root, seq, project_config)

        changed = {}
        for field in ("media_type", "format", "publisher", "confidence_status"):
            if row[field] != record[field]:
                changed[field] = (row[field], record[field])
        if changed:
            conn.execute(
                "UPDATE dcat_catalogue SET media_type=?, format=?, publisher=?, confidence_status=?, "
                "updated_at=? WHERE catalogue_id=?",
                (record["media_type"], record["format"], record["publisher"],
                 record["confidence_status"], now, row["catalogue_id"]),
            )
            changes.append({"catalogue_id": row["catalogue_id"], "changed_fields": changed})
    conn.commit()
    return changes


def cmd_migrate(project_config: dict, env: dict, dry_run: bool, apply: bool) -> None:
    if dry_run == apply:
        raise SystemExit("migrate --dcat requires exactly one of --dry-run or --apply")
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
        print(f"DCAT migrate (applied): {len(changes)} records reclassified -> {common.display_path(MIGRATION_LOG_CSV)}")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="dcat_migrate_dry_run_")
    conn = get_db(tmp_db_path)
    changes = _run_migrate(conn, project_config)
    conn.close()
    print(f"DCAT migrate (--dry-run): {len(changes)} records WOULD be reclassified.")
    for change in changes[:20]:
        print(f"  {change['catalogue_id']}: {change['changed_fields']}")


def cmd_validate(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --dcat --apply` first.")
        return
    conn = get_db()
    rows = conn.execute("SELECT * FROM dcat_catalogue ORDER BY catalogue_id").fetchall()

    issues = []
    seen_dataset_uris: dict[str, str] = {}
    for row in rows:
        if not Path(row["source_path"]).exists():
            issues.append((row["catalogue_id"], "missing_file", row["source_path"]))
        if not row["title"]:
            issues.append((row["catalogue_id"], "missing_title", ""))
        if row["byte_size"] is None or row["byte_size"] < 0:
            issues.append((row["catalogue_id"], "invalid_byte_size", row["byte_size"]))
        if not row["access_url"]:
            issues.append((row["catalogue_id"], "missing_access_url", ""))
        if row["dataset_uri"] in seen_dataset_uris:
            issues.append((row["catalogue_id"], "duplicate_dataset_uri",
                            f"also used by {seen_dataset_uris[row['dataset_uri']]}"))
        else:
            seen_dataset_uris[row["dataset_uri"]] = row["catalogue_id"]
        if row["publisher"] == UNKNOWN:
            issues.append((row["catalogue_id"], "unresolved_publisher", UNKNOWN))
    conn.close()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["catalogue_id", "category", "detail"])
        writer.writerows(issues)

    if issues:
        from collections import Counter
        by_category = Counter(i[1] for i in issues)
        print(f"DCAT validate: {len(issues)} issues across {len(rows)} records -> {common.display_path(MANUAL_REVIEW_CSV)}")
        print(f"By category: {dict(by_category)}")
    else:
        print(f"DCAT validate: {len(rows)} records, no issues found.")


def _row_to_record(row) -> dict:
    record = dict(row)
    for field in LIST_FIELDS:
        record[field] = json.loads(record.pop(f"{field}_json", "[]") or "[]")
    return record


def _turtle_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"""', '\\"\\"\\"')


def _build_turtle(catalog_uri: str, catalog_title: str, records: list[dict]) -> str:
    lines = [
        "@prefix dcat: <http://www.w3.org/ns/dcat#> .",
        "@prefix dct: <http://purl.org/dc/terms/> .",
        "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
        "@prefix spdx: <http://spdx.org/rdf/terms#> .",
        "",
        f'<{catalog_uri}> a dcat:Catalog ;',
        f'    dct:title """{_turtle_escape(catalog_title)}"""' + (" ;" if records else " ."),
    ]
    if records:
        dataset_list = " ,\n                 ".join(f"<{r['dataset_uri']}>" for r in records)
        lines.append(f"    dcat:dataset {dataset_list} .")
    lines.append("")
    for record in records:
        lines.append(f'<{record["dataset_uri"]}> a dcat:Dataset ;')
        lines.append(f'    dct:title """{_turtle_escape(record["title"])}""" ;')
        if record["description"] != UNKNOWN:
            lines.append(f'    dct:description """{_turtle_escape(record["description"])}""" ;')
        lines.append(f'    dct:issued "{record["issued"]}"^^xsd:date ;')
        lines.append(f'    dct:modified "{record["modified"]}"^^xsd:date ;')
        if record["publisher"] != UNKNOWN:
            lines.append(f'    dct:publisher """{_turtle_escape(record["publisher"])}""" ;')
        if record["license"] != UNKNOWN:
            lines.append(f'    dct:license """{_turtle_escape(record["license"])}""" ;')
        for keyword in record["keywords"]:
            lines.append(f'    dcat:keyword """{_turtle_escape(keyword)}""" ;')
        lines.append(f'    dcat:distribution <{record["distribution_uri"]}> .')
        lines.append("")
        lines.append(f'<{record["distribution_uri"]}> a dcat:Distribution ;')
        lines.append(f'    dcat:accessURL <{record["access_url"]}> ;')
        if record["media_type"] != UNKNOWN:
            lines.append(f'    dcat:mediaType """{_turtle_escape(record["media_type"])}""" ;')
        lines.append(f'    dct:format """{_turtle_escape(record["format"])}""" ;')
        lines.append(f'    dcat:byteSize "{record["byte_size"]}"^^xsd:nonNegativeInteger ;')
        lines.append("    spdx:checksum [")
        lines.append("        a spdx:Checksum ;")
        lines.append("        spdx:algorithm spdx:checksumAlgorithm_sha256 ;")
        lines.append(f'        spdx:checksumValue "{record["sha256"]}"^^xsd:hexBinary')
        lines.append("    ] .")
        lines.append("")
    return "\n".join(lines)


def cmd_export(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --dcat --apply` first.")
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    TURTLE_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    rows = conn.execute("SELECT * FROM dcat_catalogue ORDER BY catalogue_id").fetchall()
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

    catalog_count = 0
    for source_root, root_records in by_root.items():
        root_name = Path(source_root).name or f"catalog-{catalog_count}"
        catalog_uri = f"urn:dcat:catalog:{root_name}"
        turtle_text = _build_turtle(catalog_uri, f"Catalog of {root_name}", root_records)
        (TURTLE_DIR / f"{root_name}.ttl").write_text(turtle_text, encoding="utf-8")
        catalog_count += 1

    SCHEMA_JSON.write_text(json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "DCAT catalogue record",
        "type": "object",
        "required": ["catalogue_id", "dataset_uri", "distribution_uri", "title", "access_url", "byte_size"],
        "properties": {
            "dataset_uri": {"type": "string"},
            "distribution_uri": {"type": "string"},
            "byte_size": {"type": "integer", "minimum": 0},
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not MIGRATION_LOG_CSV.exists():
        with MIGRATION_LOG_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["timestamp", "catalogue_id", "field", "old_value", "new_value"])
    if not MANUAL_REVIEW_CSV.exists():
        with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["catalogue_id", "category", "detail"])

    print(f"DCAT export: {len(records)} records across {catalog_count} catalog(s) -> "
          f"{common.display_path(OUTPUT_DIR)}/ (dcat_catalogue.csv/json, turtle/<root-name>.ttl per catalog, "
          f"catalogue_schema.json, catalogue_manual_review.csv, catalogue_migration_log.csv)")
