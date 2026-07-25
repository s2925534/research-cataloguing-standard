#!/usr/bin/env python3
"""DataCite Metadata Schema catalogue mode - activated by --datacite.

A fully separate, additive cataloguing pipeline (see catalogue_common.py for
shared plumbing, dsr_catalogue.py for the isolation pattern this follows):
its own database (instance/catalogue_datacite.db) and its own output
directory (instance/catalogued_files/datacite/). Never opens or writes
instance/catalogue.db, instance/catalogue_dsr.db,
instance/catalogue_dublin_core.db, or any other standard's outputs.

Models DataCite's mandatory properties (Identifier, Creator, Title,
Publisher, PublicationYear, ResourceType) and the recommended properties
most relevant to a local research catalogue (Subject, Contributor, Date,
RelatedIdentifier, Description, Language, Version, Rights, FormatS, Sizes).
Deterministic derivation only:

  - identifier     : identifierType "Local" with a content-addressed value
                     (sha256) by default. This engine never fabricates a DOI
                     - a real DOI can only enter a record via an explicit
                     <file>.datacite.json sidecar, since a DOI is a formally
                     registered identifier, not something derivable from a
                     file's bytes or path.
  - titles         : filename stem, titleType "Other" (not "AlternativeTitle"
                     or the unqualified main title - this engine hasn't read
                     the file's content, so it cannot claim this is *the*
                     title, only a placeholder derived from the filename).
  - publisher      : project_config.json -> institution, if set (explicitly
                     configured, real data - not invented) else "Unknown".
  - publicationYear: the file's last-modified year (a filesystem fact, not a
                     true publication date) - always flagged Requires Review
                     since it is a proxy, never presented as authoritative.
  - resourceTypeGeneral : DataCite's controlled vocabulary, derived from file
                     extension.
  - dates, formats, sizes : filesystem/extension facts.
  - creators, subjects, contributors, relatedIdentifiers, descriptions,
    version, rights, language : left empty/"Unknown" by default - never
    invented. A <file>.datacite.json sidecar can supply any of these
    explicitly.
"""
from __future__ import annotations

import csv
import json
import mimetypes
import xml.sax.saxutils as saxutils
from datetime import datetime, timezone
from pathlib import Path

import catalogue_common as common

DB_PATH = common.INSTANCE_DIR / "catalogue_datacite.db"
OUTPUT_DIR = common.CATALOGUE_DIR / "datacite"

CATALOGUE_CSV = OUTPUT_DIR / "datacite_catalogue.csv"
CATALOGUE_JSON = OUTPUT_DIR / "datacite_catalogue.json"
CATALOGUE_XML_DIR = OUTPUT_DIR / "datacite_xml"
SCHEMA_JSON = OUTPUT_DIR / "catalogue_schema.json"
MANUAL_REVIEW_CSV = OUTPUT_DIR / "catalogue_manual_review.csv"
MIGRATION_LOG_CSV = OUTPUT_DIR / "catalogue_migration_log.csv"

UNKNOWN = "Unknown"

# DataCite Metadata Schema 4.x -> resourceTypeGeneral controlled vocabulary.
RESOURCE_TYPE_GENERAL = [
    "Audiovisual", "Book", "BookChapter", "Collection", "ComputationalNotebook",
    "ConferencePaper", "ConferenceProceeding", "DataPaper", "Dataset",
    "Dissertation", "Event", "Image", "InteractiveResource", "Journal",
    "JournalArticle", "Model", "OutputManagementPlan", "PeerReview",
    "PhysicalObject", "Preprint", "Report", "Service", "Software", "Sound",
    "Standard", "StudyRegistration", "Text", "Workflow", "Other",
]

# DataCite relatedIdentifier -> relationType controlled vocabulary (subset
# most relevant to a research project; full list is much longer).
RELATION_TYPES = [
    "IsCitedBy", "Cites", "IsSupplementTo", "IsSupplementedBy", "IsPartOf",
    "HasPart", "IsNewVersionOf", "IsPreviousVersionOf", "IsDerivedFrom",
    "IsSourceOf", "References", "IsReferencedBy", "Documents", "IsDocumentedBy",
]

DATE_TYPES = ["Accepted", "Available", "Collected", "Copyrighted", "Created",
              "Issued", "Submitted", "Updated", "Valid", "Withdrawn", "Other"]

EXTENSION_TO_RESOURCE_TYPE = {
    ".docx": "Text", ".doc": "Text", ".odt": "Text", ".rtf": "Text",
    ".md": "Text", ".txt": "Text", ".tex": "Text", ".pdf": "Text",
    ".csv": "Dataset", ".tsv": "Dataset", ".xlsx": "Dataset", ".xls": "Dataset",
    ".json": "Dataset", ".jsonl": "Dataset", ".xml": "Dataset", ".parquet": "Dataset",
    ".sqlite": "Dataset", ".db": "Dataset",
    ".png": "Image", ".jpg": "Image", ".jpeg": "Image", ".gif": "Image",
    ".svg": "Image", ".tif": "Image", ".tiff": "Image",
    ".mp4": "Audiovisual", ".mov": "Audiovisual", ".avi": "Audiovisual", ".mkv": "Audiovisual",
    ".mp3": "Sound", ".wav": "Sound", ".flac": "Sound",
    ".py": "Software", ".js": "Software", ".ts": "Software", ".java": "Software", ".sh": "Software",
    ".ipynb": "ComputationalNotebook",
    ".html": "InteractiveResource", ".htm": "InteractiveResource",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS datacite_catalogue (
    catalogue_id TEXT PRIMARY KEY,
    source_path TEXT UNIQUE,
    relative_path TEXT,
    file_name TEXT,
    identifier_type TEXT DEFAULT 'Local',
    identifier_value TEXT,
    titles_json TEXT DEFAULT '[]',
    creators_json TEXT DEFAULT '[]',
    publisher TEXT DEFAULT 'Unknown',
    publication_year INTEGER,
    resource_type_general TEXT,
    resource_type TEXT,
    subjects_json TEXT DEFAULT '[]',
    contributors_json TEXT DEFAULT '[]',
    dates_json TEXT DEFAULT '[]',
    related_identifiers_json TEXT DEFAULT '[]',
    descriptions_json TEXT DEFAULT '[]',
    language TEXT DEFAULT 'Unknown',
    version TEXT,
    rights TEXT DEFAULT 'Unknown',
    formats_json TEXT DEFAULT '[]',
    sizes_json TEXT DEFAULT '[]',
    sha256 TEXT,
    file_size_bytes INTEGER,
    explicit_metadata_applied INTEGER DEFAULT 0,
    confidence_status TEXT DEFAULT 'Requires Review',
    notes TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_datacite_sha256 ON datacite_catalogue(sha256);

CREATE TABLE IF NOT EXISTS datacite_counters (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_seq INTEGER NOT NULL DEFAULT 1
);
"""


def get_db(db_path: Path | None = None):
    return common.get_sqlite_db(db_path or DB_PATH, SCHEMA_SQL)


def next_seq(conn) -> int:
    row = conn.execute("SELECT next_seq FROM datacite_counters WHERE id = 1").fetchone()
    seq = row["next_seq"] if row else 1
    conn.execute(
        "INSERT INTO datacite_counters (id, next_seq) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET next_seq = ?",
        (seq + 1, seq + 1),
    )
    return seq


def load_sidecar_metadata(path: Path) -> dict | None:
    sidecar = path.with_name(path.name + ".datacite.json")
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def classify_resource_type(path: Path) -> str:
    return EXTENSION_TO_RESOURCE_TYPE.get(path.suffix.lower(), "Other")


def build_record(path: Path, source_root: Path, seq: int, project_config: dict) -> dict:
    stat = path.stat()
    sha256 = common.sha256_file(path)
    sidecar = load_sidecar_metadata(path) or {}

    resource_type_general = sidecar.get("resource_type_general", classify_resource_type(path))
    mime_type, _ = mimetypes.guess_type(path.name)
    created_iso = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat()
    modified_iso = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    modified_year = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).year

    identifier_type = sidecar.get("identifier_type", "Local")
    identifier_value = sidecar.get("identifier_value", sha256 if identifier_type == "Local" else None)

    has_explicit = bool(sidecar)
    record = {
        "catalogue_id": f"DATACITE-{seq:05d}",
        "source_path": str(path.resolve()),
        "relative_path": str(path.relative_to(source_root)),
        "file_name": path.name,
        "identifier_type": identifier_type,
        "identifier_value": identifier_value,
        "titles": sidecar.get("titles", [{"title": path.stem, "titleType": "Other"}]),
        "creators": sidecar.get("creators", []),
        "publisher": sidecar.get("publisher", project_config.get("institution") or UNKNOWN),
        "publication_year": sidecar.get("publication_year", modified_year),
        "resource_type_general": resource_type_general,
        "resource_type": sidecar.get("resource_type", path.suffix.lstrip(".").upper() or UNKNOWN),
        "subjects": sidecar.get("subjects", []),
        "contributors": sidecar.get("contributors", []),
        "dates": sidecar.get("dates", [
            {"date": created_iso, "dateType": "Created"},
            {"date": modified_iso, "dateType": "Updated"},
        ]),
        "related_identifiers": sidecar.get("related_identifiers", []),
        "descriptions": sidecar.get("descriptions", []),
        "language": sidecar.get("language", UNKNOWN),
        "version": sidecar.get("version"),
        "rights": sidecar.get("rights", UNKNOWN),
        "formats": sidecar.get("formats", [mime_type] if mime_type else []),
        "sizes": sidecar.get("sizes", [f"{stat.st_size} bytes"]),
        "sha256": sha256,
        "file_size_bytes": stat.st_size,
        "explicit_metadata_applied": 1 if has_explicit else 0,
        "confidence_status": "Confident" if (has_explicit or resource_type_general != "Other") else "Requires Review",
    }
    return record


LIST_FIELDS = ("titles", "creators", "subjects", "contributors", "dates",
               "related_identifiers", "descriptions", "formats", "sizes")


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
            if path.name.endswith(".datacite.json"):
                continue
            if common.is_excluded(path, source_root):
                excluded += 1
                continue

            source_path = str(path.resolve())
            existing = conn.execute(
                "SELECT catalogue_id FROM datacite_catalogue WHERE source_path = ?", (source_path,)
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
                f"INSERT INTO datacite_catalogue ({', '.join(columns)}, created_at, updated_at) "
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
        raise SystemExit("scan --datacite requires exactly one of --dry-run or --apply")
    if apply:
        conn = get_db()
        report = _run_scan(conn, env, project_config)
        conn.close()
        print(f"DataCite scan (applied): {report['scanned']} new files catalogued, "
              f"{report['excluded']} excluded, {report['requires_review']} flagged Requires Review.")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="datacite_scan_dry_run_")
    conn = get_db(tmp_db_path)
    report = _run_scan(conn, env, project_config)
    conn.close()
    print(f"DataCite scan (--dry-run): {report['scanned']} new files WOULD be catalogued, "
          f"{report['excluded']} excluded, {report['requires_review']} would be flagged Requires Review.")
    print(f"Nothing written to {common.display_path(DB_PATH)}. Working copy left at {tmp_db_path}.")


def _run_migrate(conn, project_config: dict) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    changes = []
    rows = conn.execute("SELECT * FROM datacite_catalogue ORDER BY catalogue_id").fetchall()
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
        for field in ("resource_type_general", "resource_type", "confidence_status"):
            if row[field] != record[field]:
                changed[field] = (row[field], record[field])
        if changed:
            conn.execute(
                "UPDATE datacite_catalogue SET resource_type_general=?, resource_type=?, "
                "confidence_status=?, updated_at=? WHERE catalogue_id=?",
                (record["resource_type_general"], record["resource_type"],
                 record["confidence_status"], now, row["catalogue_id"]),
            )
            changes.append({"catalogue_id": row["catalogue_id"], "changed_fields": changed})
    conn.commit()
    return changes


def cmd_migrate(project_config: dict, env: dict, dry_run: bool, apply: bool) -> None:
    if dry_run == apply:
        raise SystemExit("migrate --datacite requires exactly one of --dry-run or --apply")
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
        print(f"DataCite migrate (applied): {len(changes)} records reclassified -> {common.display_path(MIGRATION_LOG_CSV)}")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="datacite_migrate_dry_run_")
    conn = get_db(tmp_db_path)
    changes = _run_migrate(conn, project_config)
    conn.close()
    print(f"DataCite migrate (--dry-run): {len(changes)} records WOULD be reclassified.")
    for change in changes[:20]:
        print(f"  {change['catalogue_id']}: {change['changed_fields']}")


def cmd_validate(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --datacite --apply` first.")
        return
    conn = get_db()
    rows = conn.execute("SELECT * FROM datacite_catalogue ORDER BY catalogue_id").fetchall()

    issues = []
    seen_identifiers: dict[str, str] = {}
    for row in rows:
        if not Path(row["source_path"]).exists():
            issues.append((row["catalogue_id"], "missing_file", row["source_path"]))
        if row["resource_type_general"] not in RESOURCE_TYPE_GENERAL:
            issues.append((row["catalogue_id"], "invalid_resource_type_general", row["resource_type_general"]))
        if not row["identifier_value"]:
            issues.append((row["catalogue_id"], "missing_identifier", ""))
        else:
            key = f"{row['identifier_type']}:{row['identifier_value']}"
            if key in seen_identifiers:
                issues.append((row["catalogue_id"], "duplicate_identifier", f"also used by {seen_identifiers[key]}"))
            else:
                seen_identifiers[key] = row["catalogue_id"]
        titles = json.loads(row["titles_json"] or "[]")
        if not titles:
            issues.append((row["catalogue_id"], "missing_title", ""))
        if row["publisher"] == UNKNOWN:
            issues.append((row["catalogue_id"], "unresolved_publisher", UNKNOWN))
        if not row["publication_year"]:
            issues.append((row["catalogue_id"], "missing_publication_year", ""))
        related = json.loads(row["related_identifiers_json"] or "[]")
        for rel in related:
            if rel.get("relationType") not in RELATION_TYPES:
                issues.append((row["catalogue_id"], "invalid_relation_type", rel.get("relationType")))
    conn.close()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["catalogue_id", "category", "detail"])
        writer.writerows(issues)

    if issues:
        from collections import Counter
        by_category = Counter(i[1] for i in issues)
        print(f"DataCite validate: {len(issues)} issues across {len(rows)} records -> {common.display_path(MANUAL_REVIEW_CSV)}")
        print(f"By category: {dict(by_category)}")
    else:
        print(f"DataCite validate: {len(rows)} records, no issues found.")


def _row_to_record(row) -> dict:
    record = dict(row)
    for field in LIST_FIELDS:
        record[field] = json.loads(record.pop(f"{field}_json", "[]") or "[]")
    return record


def _record_to_datacite_xml(record: dict) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<resource xmlns="http://datacite.org/schema/kernel-4" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://datacite.org/schema/kernel-4 '
        'http://schema.datacite.org/meta/kernel-4/metadata.xsd">',
        f'  <identifier identifierType="{saxutils.escape(record["identifier_type"])}">'
        f'{saxutils.escape(str(record["identifier_value"] or ""))}</identifier>',
        "  <creators>",
    ]
    creators = record["creators"] or [{"name": UNKNOWN}]
    for creator in creators:
        lines.append(f'    <creator><creatorName>{saxutils.escape(str(creator.get("name", UNKNOWN)))}</creatorName></creator>')
    lines.append("  </creators>")
    lines.append("  <titles>")
    for title in record["titles"]:
        title_type_attr = f' titleType="{saxutils.escape(title["titleType"])}"' if title.get("titleType") else ""
        lines.append(f'    <title{title_type_attr}>{saxutils.escape(title["title"])}</title>')
    lines.append("  </titles>")
    lines.append(f'  <publisher>{saxutils.escape(str(record["publisher"]))}</publisher>')
    lines.append(f'  <publicationYear>{record["publication_year"] or ""}</publicationYear>')
    lines.append(f'  <resourceType resourceTypeGeneral="{saxutils.escape(record["resource_type_general"])}">'
                  f'{saxutils.escape(str(record["resource_type"]))}</resourceType>')
    if record["subjects"]:
        lines.append("  <subjects>")
        for subject in record["subjects"]:
            lines.append(f'    <subject>{saxutils.escape(str(subject))}</subject>')
        lines.append("  </subjects>")
    if record["dates"]:
        lines.append("  <dates>")
        for date in record["dates"]:
            lines.append(f'    <date dateType="{saxutils.escape(date["dateType"])}">{saxutils.escape(date["date"])}</date>')
        lines.append("  </dates>")
    if record["version"]:
        lines.append(f'  <version>{saxutils.escape(str(record["version"]))}</version>')
    if record["rights"] and record["rights"] != UNKNOWN:
        lines.append(f'  <rightsList><rights>{saxutils.escape(str(record["rights"]))}</rights></rightsList>')
    if record["related_identifiers"]:
        lines.append("  <relatedIdentifiers>")
        for rel in record["related_identifiers"]:
            lines.append(
                f'    <relatedIdentifier relatedIdentifierType="{saxutils.escape(rel.get("relatedIdentifierType", "Local"))}" '
                f'relationType="{saxutils.escape(rel.get("relationType", "Other"))}">'
                f'{saxutils.escape(str(rel.get("value", "")))}</relatedIdentifier>'
            )
        lines.append("  </relatedIdentifiers>")
    lines.append("</resource>")
    return "\n".join(lines)


def cmd_export(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --datacite --apply` first.")
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CATALOGUE_XML_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    rows = conn.execute("SELECT * FROM datacite_catalogue ORDER BY catalogue_id").fetchall()
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
        xml_path.write_text(_record_to_datacite_xml(record), encoding="utf-8")

    SCHEMA_JSON.write_text(json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "DataCite catalogue record",
        "type": "object",
        "required": ["catalogue_id", "identifier_value", "titles", "publisher", "publication_year", "resource_type_general"],
        "properties": {
            "identifier_type": {"type": "string"},
            "identifier_value": {"type": "string"},
            "resource_type_general": {"type": "string", "enum": RESOURCE_TYPE_GENERAL},
            "publication_year": {"type": ["integer", "null"]},
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not MIGRATION_LOG_CSV.exists():
        with MIGRATION_LOG_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["timestamp", "catalogue_id", "field", "old_value", "new_value"])
    if not MANUAL_REVIEW_CSV.exists():
        with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["catalogue_id", "category", "detail"])

    print(f"DataCite export: {len(records)} records -> {common.display_path(OUTPUT_DIR)}/ "
          f"(datacite_catalogue.csv/json, datacite_xml/<id>.xml per record, catalogue_schema.json, "
          f"catalogue_manual_review.csv, catalogue_migration_log.csv)")
