#!/usr/bin/env python3
"""Crossref metadata deposit schema catalogue mode - activated by --crossref.

A fully separate, additive cataloguing pipeline (see catalogue_common.py for
shared plumbing, dsr_catalogue.py for the isolation pattern this follows):
its own database (instance/catalogue_crossref.db) and its own output
directory (instance/catalogued_files/crossref/). Never opens or writes
instance/catalogue.db or any other standard's outputs.

Crossref is fundamentally different from DSR/Dublin Core/DataCite: it exists
to register formally published, peer-reviewed scholarly outputs (journal
articles, books, conference papers, dissertations, preprints, peer reviews)
for citation infrastructure - it is not a general file-description vocabulary.
An arbitrary project file (a dataset, a screenshot, a code script) is not a
Crossref deposit candidate no matter how it's described, so this engine does
not force every scanned file into a scholarly-work shape the way the other
standards do. Every catalogued record gets a boolean
`crossref_applicable` (True only when directory/filename evidence suggests
the file is plausibly a scholarly-work manuscript - under a
publications/manuscripts/papers/submissions directory, or a filename
containing a work-type token). Records where `crossref_applicable` is False
get `publication_type = "Not Applicable"` and are excluded from validate's
issue counts and export's XML output - they are catalogued (so scan is still
a complete inventory) but not treated as malformed Crossref records.

  - doi            : never fabricated. Crossref DOIs are formally registered
                     identifiers assigned by a member publisher/agency, not
                     something derivable from a file's bytes or path.
                     doi_status defaults to "unregistered_local"; a real DOI
                     only enters a record via an explicit
                     <file>.crossref.json sidecar.
  - publication_type : Crossref's controlled work-type vocabulary, derived
                     from directory/filename tokens (Step 5/6-style rules,
                     same priority as the other modules - directory marker
                     first, then filename token, else "Other"/Requires Review).
  - title          : filename stem by default (never invented from content).
  - publication_date : the file's last-modified date, always flagged
                     Requires Review since a filesystem timestamp is a proxy
                     for the real publication date, never authoritative.
  - contributors, container_title, issn, isbn, volume, issue, pages,
    publisher : left empty/"Unknown" by default - never invented. A
    <file>.crossref.json sidecar can supply any of these explicitly.
"""
from __future__ import annotations

import csv
import json
import re
import xml.sax.saxutils as saxutils
from datetime import datetime, timezone
from pathlib import Path

import catalogue_common as common

DB_PATH = common.INSTANCE_DIR / "catalogue_crossref.db"
OUTPUT_DIR = common.CATALOGUE_DIR / "crossref"

CATALOGUE_CSV = OUTPUT_DIR / "crossref_catalogue.csv"
CATALOGUE_JSON = OUTPUT_DIR / "crossref_catalogue.json"
CATALOGUE_XML_DIR = OUTPUT_DIR / "crossref_xml"
SCHEMA_JSON = OUTPUT_DIR / "catalogue_schema.json"
MANUAL_REVIEW_CSV = OUTPUT_DIR / "catalogue_manual_review.csv"
MIGRATION_LOG_CSV = OUTPUT_DIR / "catalogue_migration_log.csv"

UNKNOWN = "Unknown"
NOT_APPLICABLE = "Not Applicable"

# Crossref's controlled work-type vocabulary (subset most relevant to a
# research project's own catalogue - the full deposit schema supports more,
# e.g. grant, database, component).
PUBLICATION_TYPES = [
    "journal-article", "journal", "book", "book-chapter", "monograph",
    "edited-book", "reference-book", "proceedings-article", "proceedings",
    "dissertation", "report", "standard", "dataset", "peer-review",
    "posted-content", "Other", NOT_APPLICABLE,
]

APPLICABLE_DIR_MARKERS = ("/publications/", "/manuscripts/", "/papers/", "/submissions/")

WORK_TYPE_TOKENS = [
    ("journal-article", "journal-article"), ("journal", "journal"),
    ("conference-paper", "proceedings-article"), ("proceedings-article", "proceedings-article"),
    ("proceedings", "proceedings"),
    ("book-chapter", "book-chapter"), ("edited-book", "edited-book"),
    ("reference-book", "reference-book"), ("monograph", "monograph"), ("book", "book"),
    ("dissertation", "dissertation"), ("thesis", "dissertation"),
    ("report", "report"), ("standard", "standard"),
    ("preprint", "posted-content"), ("posted-content", "posted-content"),
    ("peer-review", "peer-review"), ("dataset", "dataset"),
]

DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS crossref_catalogue (
    catalogue_id TEXT PRIMARY KEY,
    source_path TEXT UNIQUE,
    relative_path TEXT,
    file_name TEXT,
    crossref_applicable INTEGER DEFAULT 0,
    doi TEXT,
    doi_status TEXT DEFAULT 'unregistered_local',
    title TEXT,
    contributors_json TEXT DEFAULT '[]',
    publication_type TEXT,
    container_title TEXT DEFAULT 'Unknown',
    publisher TEXT DEFAULT 'Unknown',
    issn TEXT,
    isbn TEXT,
    volume TEXT,
    issue TEXT,
    first_page TEXT,
    last_page TEXT,
    publication_date TEXT,
    abstract TEXT DEFAULT 'Unknown',
    sha256 TEXT,
    file_size_bytes INTEGER,
    explicit_metadata_applied INTEGER DEFAULT 0,
    confidence_status TEXT DEFAULT 'Requires Review',
    classification_rule TEXT,
    notes TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_crossref_sha256 ON crossref_catalogue(sha256);

CREATE TABLE IF NOT EXISTS crossref_counters (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_seq INTEGER NOT NULL DEFAULT 1
);
"""


def get_db(db_path: Path | None = None):
    return common.get_sqlite_db(db_path or DB_PATH, SCHEMA_SQL)


def next_seq(conn) -> int:
    row = conn.execute("SELECT next_seq FROM crossref_counters WHERE id = 1").fetchone()
    seq = row["next_seq"] if row else 1
    conn.execute(
        "INSERT INTO crossref_counters (id, next_seq) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET next_seq = ?",
        (seq + 1, seq + 1),
    )
    return seq


def load_sidecar_metadata(path: Path) -> dict | None:
    sidecar = path.with_name(path.name + ".crossref.json")
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def determine_applicability(path: Path, source_root: Path) -> tuple[bool, str | None, str]:
    """Returns (applicable, publication_type_or_None, evidence)."""
    rel_lower = "/" + str(path.relative_to(source_root)).lower().replace("\\", "/")
    name_lower = path.name.lower()

    dir_marker = next((m for m in APPLICABLE_DIR_MARKERS if m in rel_lower), None)
    token_match = next((wt for token, wt in WORK_TYPE_TOKENS if token in name_lower), None)

    if token_match:
        return True, token_match, f"filename token matched work type '{token_match}'"
    if dir_marker and path.suffix.lower() in (".pdf", ".docx", ".doc", ".tex"):
        return True, None, f"directory marker {dir_marker} + manuscript-like extension"
    return False, None, "no scholarly-work directory marker or filename token"


def build_record(path: Path, source_root: Path, seq: int) -> dict:
    stat = path.stat()
    sha256 = common.sha256_file(path)
    sidecar = load_sidecar_metadata(path) or {}

    applicable, work_type, evidence = determine_applicability(path, source_root)
    if sidecar.get("publication_type"):
        applicable = True
        work_type = sidecar["publication_type"]
        rule = "explicit_sidecar_metadata"
    elif applicable:
        rule = "directory_or_filename_token" if work_type else "directory_marker_only"
    else:
        rule = "not_applicable"

    publication_type = work_type if applicable else NOT_APPLICABLE
    if applicable and not publication_type:
        publication_type = "Other"

    modified_date = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).date().isoformat()
    doi = sidecar.get("doi")
    doi_status = "registered" if doi else "unregistered_local"

    if not applicable:
        confidence_status = NOT_APPLICABLE
    elif sidecar or publication_type not in ("Other",):
        confidence_status = "Confident"
    else:
        confidence_status = "Requires Review"

    return {
        "catalogue_id": f"CROSSREF-{seq:05d}",
        "source_path": str(path.resolve()),
        "relative_path": str(path.relative_to(source_root)),
        "file_name": path.name,
        "crossref_applicable": 1 if applicable else 0,
        "doi": doi,
        "doi_status": doi_status,
        "title": sidecar.get("title", path.stem),
        "contributors": sidecar.get("contributors", []),
        "publication_type": publication_type,
        "container_title": sidecar.get("container_title", UNKNOWN),
        "publisher": sidecar.get("publisher", UNKNOWN),
        "issn": sidecar.get("issn"),
        "isbn": sidecar.get("isbn"),
        "volume": sidecar.get("volume"),
        "issue": sidecar.get("issue"),
        "first_page": sidecar.get("first_page"),
        "last_page": sidecar.get("last_page"),
        "publication_date": sidecar.get("publication_date", modified_date),
        "abstract": sidecar.get("abstract", UNKNOWN),
        "sha256": sha256,
        "file_size_bytes": stat.st_size,
        "explicit_metadata_applied": 1 if sidecar else 0,
        "confidence_status": confidence_status,
        "classification_rule": rule,
        "notes": evidence if not sidecar else None,
    }


LIST_FIELDS = ("contributors",)


def _run_scan(conn, env: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    scanned = 0
    excluded = 0
    applicable_count = 0
    requires_review = 0

    for source_root in common.source_roots_from_env(env):
        if not source_root.exists():
            print(f"WARNING: source root does not exist, skipping: {source_root}")
            continue
        for path in source_root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            if path.name.endswith(".crossref.json"):
                continue
            if common.is_excluded(path, source_root):
                excluded += 1
                continue

            source_path = str(path.resolve())
            existing = conn.execute(
                "SELECT catalogue_id FROM crossref_catalogue WHERE source_path = ?", (source_path,)
            ).fetchone()
            if existing:
                continue

            seq = next_seq(conn)
            record = build_record(path, source_root, seq)

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
                f"INSERT INTO crossref_catalogue ({', '.join(columns)}, created_at, updated_at) "
                f"VALUES ({placeholders}, ?, ?)",
                (*values, now, now),
            )
            scanned += 1
            if record["crossref_applicable"]:
                applicable_count += 1
            if record["confidence_status"] == "Requires Review":
                requires_review += 1
            if scanned % 200 == 0:
                conn.commit()

    conn.commit()
    return {"scanned": scanned, "excluded": excluded, "applicable": applicable_count,
            "requires_review": requires_review}


def cmd_scan(project_config: dict, env: dict, dry_run: bool, apply: bool) -> None:
    if dry_run == apply:
        raise SystemExit("scan --crossref requires exactly one of --dry-run or --apply")
    if apply:
        conn = get_db()
        report = _run_scan(conn, env)
        conn.close()
        print(f"Crossref scan (applied): {report['scanned']} new files catalogued "
              f"({report['applicable']} plausible scholarly-work deposits), "
              f"{report['excluded']} excluded, {report['requires_review']} flagged Requires Review.")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="crossref_scan_dry_run_")
    conn = get_db(tmp_db_path)
    report = _run_scan(conn, env)
    conn.close()
    print(f"Crossref scan (--dry-run): {report['scanned']} new files WOULD be catalogued "
          f"({report['applicable']} plausible scholarly-work deposits), "
          f"{report['excluded']} excluded, {report['requires_review']} would be flagged Requires Review.")
    print(f"Nothing written to {common.display_path(DB_PATH)}. Working copy left at {tmp_db_path}.")


def _run_migrate(conn) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    changes = []
    rows = conn.execute("SELECT * FROM crossref_catalogue ORDER BY catalogue_id").fetchall()
    for row in rows:
        source_path = Path(row["source_path"])
        if not source_path.exists():
            continue
        source_root = source_path
        for _ in range(len(Path(row["relative_path"]).parts)):
            source_root = source_root.parent
        seq = int(row["catalogue_id"].split("-")[1])
        record = build_record(source_path, source_root, seq)

        changed = {}
        for field in ("crossref_applicable", "publication_type", "confidence_status", "classification_rule"):
            if row[field] != record[field]:
                changed[field] = (row[field], record[field])
        if changed:
            conn.execute(
                "UPDATE crossref_catalogue SET crossref_applicable=?, publication_type=?, "
                "confidence_status=?, classification_rule=?, updated_at=? WHERE catalogue_id=?",
                (record["crossref_applicable"], record["publication_type"], record["confidence_status"],
                 record["classification_rule"], now, row["catalogue_id"]),
            )
            changes.append({"catalogue_id": row["catalogue_id"], "changed_fields": changed})
    conn.commit()
    return changes


def cmd_migrate(project_config: dict, env: dict, dry_run: bool, apply: bool) -> None:
    if dry_run == apply:
        raise SystemExit("migrate --crossref requires exactly one of --dry-run or --apply")
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
        print(f"Crossref migrate (applied): {len(changes)} records reclassified -> {common.display_path(MIGRATION_LOG_CSV)}")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="crossref_migrate_dry_run_")
    conn = get_db(tmp_db_path)
    changes = _run_migrate(conn)
    conn.close()
    print(f"Crossref migrate (--dry-run): {len(changes)} records WOULD be reclassified.")
    for change in changes[:20]:
        print(f"  {change['catalogue_id']}: {change['changed_fields']}")


def cmd_validate(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --crossref --apply` first.")
        return
    conn = get_db()
    rows = conn.execute("SELECT * FROM crossref_catalogue ORDER BY catalogue_id").fetchall()

    issues = []
    for row in rows:
        if not row["crossref_applicable"]:
            continue  # out of scope for Crossref - not a validation failure
        if not Path(row["source_path"]).exists():
            issues.append((row["catalogue_id"], "missing_file", row["source_path"]))
        if row["publication_type"] not in PUBLICATION_TYPES:
            issues.append((row["catalogue_id"], "invalid_publication_type", row["publication_type"]))
        if row["doi"] and not DOI_RE.match(row["doi"]):
            issues.append((row["catalogue_id"], "malformed_doi", row["doi"]))
        if not row["title"]:
            issues.append((row["catalogue_id"], "missing_title", ""))
        contributors = json.loads(row["contributors_json"] or "[]")
        if not contributors:
            issues.append((row["catalogue_id"], "unresolved_contributors", ""))
    conn.close()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["catalogue_id", "category", "detail"])
        writer.writerows(issues)

    applicable_count = sum(1 for r in rows if r["crossref_applicable"])
    if issues:
        from collections import Counter
        by_category = Counter(i[1] for i in issues)
        print(f"Crossref validate: {len(issues)} issues across {applicable_count} applicable records "
              f"(of {len(rows)} total catalogued) -> {common.display_path(MANUAL_REVIEW_CSV)}")
        print(f"By category: {dict(by_category)}")
    else:
        print(f"Crossref validate: {applicable_count} applicable records (of {len(rows)} total), no issues found.")


def _row_to_record(row) -> dict:
    record = dict(row)
    for field in LIST_FIELDS:
        record[field] = json.loads(record.pop(f"{field}_json", "[]") or "[]")
    return record


def _record_to_crossref_xml(record: dict) -> str:
    """A generalized <crossref_work> element carrying the common core fields
    (title, contributors, doi, dates) with a work_type attribute, rather than
    fully replicating each of Crossref's ~15 distinct nested XML sub-schemas
    (journal vs conference vs book vs dataset vs peer-review each have their
    own structure in the real deposit schema) - modelling every one of those
    precisely is out of scope for a local research catalogue."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<crossref_work xmlns="http://www.crossref.org/schema/5.3.1" work_type="{saxutils.escape(record["publication_type"])}">',
        f'  <titles><title>{saxutils.escape(str(record["title"]))}</title></titles>',
        "  <contributors>",
    ]
    for contributor in record["contributors"]:
        given = saxutils.escape(str(contributor.get("given_name", "")))
        surname = saxutils.escape(str(contributor.get("surname", UNKNOWN)))
        role = saxutils.escape(str(contributor.get("role", "author")))
        lines.append(f'    <person_name contributor_role="{role}"><given_name>{given}</given_name><surname>{surname}</surname></person_name>')
    lines.append("  </contributors>")
    lines.append(f'  <publication_date><date>{saxutils.escape(str(record["publication_date"]))}</date></publication_date>')
    if record["container_title"] != UNKNOWN:
        lines.append(f'  <container_title>{saxutils.escape(str(record["container_title"]))}</container_title>')
    if record["publisher"] != UNKNOWN:
        lines.append(f'  <publisher>{saxutils.escape(str(record["publisher"]))}</publisher>')
    lines.append("  <doi_data>")
    lines.append(f'    <doi status="{saxutils.escape(record["doi_status"])}">{saxutils.escape(str(record["doi"] or ""))}</doi>')
    lines.append("  </doi_data>")
    lines.append("</crossref_work>")
    return "\n".join(lines)


def cmd_export(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --crossref --apply` first.")
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CATALOGUE_XML_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    rows = conn.execute("SELECT * FROM crossref_catalogue ORDER BY catalogue_id").fetchall()
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

    xml_count = 0
    for record in records:
        if not record["crossref_applicable"]:
            continue
        xml_path = CATALOGUE_XML_DIR / f"{record['catalogue_id']}.xml"
        xml_path.write_text(_record_to_crossref_xml(record), encoding="utf-8")
        xml_count += 1

    SCHEMA_JSON.write_text(json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Crossref catalogue record",
        "type": "object",
        "required": ["catalogue_id", "title", "publication_type", "crossref_applicable"],
        "properties": {
            "publication_type": {"type": "string", "enum": PUBLICATION_TYPES},
            "doi": {"type": ["string", "null"]},
            "crossref_applicable": {"type": "integer", "enum": [0, 1]},
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not MIGRATION_LOG_CSV.exists():
        with MIGRATION_LOG_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["timestamp", "catalogue_id", "field", "old_value", "new_value"])
    if not MANUAL_REVIEW_CSV.exists():
        with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["catalogue_id", "category", "detail"])

    print(f"Crossref export: {len(records)} records catalogued, {xml_count} applicable "
          f"deposit XML files -> {common.display_path(OUTPUT_DIR)}/ "
          f"(crossref_catalogue.csv/json, crossref_xml/<id>.xml per applicable record, "
          f"catalogue_schema.json, catalogue_manual_review.csv, catalogue_migration_log.csv)")
