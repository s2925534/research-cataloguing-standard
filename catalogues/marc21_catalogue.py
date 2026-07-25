#!/usr/bin/env python3
"""MARC 21 bibliographic catalogue mode - activated by --marc21.

A fully separate, additive cataloguing pipeline (see catalogue_common.py for
shared plumbing, dsr_catalogue.py for the isolation pattern this follows):
its own database (instance/catalogue_marc21.db) and its own output directory
(instance/catalogued_files/marc21/). Never opens or writes
instance/catalogue.db or any other standard's outputs.

MARC 21 is the most structurally rigid standard here: a 24-position fixed
Leader, a 40-character 008 control field with byte-exact meaning per
position, and tagged/indicatored variable fields with subfields. Like
Dublin Core/DataCite/MODS, it describes every catalogued file - it's not
gated to scholarly manuscripts the way Crossref/CERIF are.

Every Leader/008 position this engine cannot honestly derive uses MARC's
own sanctioned "|" (pipe) fill character - the spec's real convention for
"no attempt to code" - rather than a fabricated value. Concretely:

  - Leader/05 (record status)      : 'n' New record (true - freshly catalogued)
  - Leader/06 (type of record)     : derived from file extension (falls back
                                     to 'm' Computer file - true of every
                                     catalogued item regardless of content)
  - Leader/07 (bibliographic level): 'm' Monograph/item (one file = one item)
  - Leader/17 (encoding level)     : 'u' Unknown (honest - this is a
                                     machine-generated, unreviewed record)
  - Leader/18 (descriptive form)   : 'u' Unknown
  - 008/06 (type of date)          : 's' Single known date (the *value* is a
                                     filesystem-timestamp proxy, flagged
                                     Requires Review via confidence_status,
                                     same pattern used across every other
                                     module here - not claimed authoritative)
  - 008/15-17 (place of publication), 008/18-34 (format-specific positions)
                                    : '|' fill - not evaluated
  - 008/35-37 (language)           : "und" (Undetermined, the correct
                                     ISO 639-2 code for "language unknown")
  - 008/39 (cataloging source)     : 'd' Other (this is not a national
                                     bibliographic agency)

100/700 (personal name) only appears when project_config.json ->
researcher is genuinely configured (guards the "REPLACE_ME" placeholder,
same as cerif_catalogue.py); 020/022/024 (ISBN/ISSN/DOI) only ever come
from an explicit <file>.marc21.json sidecar, since those are formally
assigned identifiers, not derivable from a file's bytes or path.
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import catalogue_common as common

DB_PATH = common.INSTANCE_DIR / "catalogue_marc21.db"
OUTPUT_DIR = common.CATALOGUE_DIR / "marc21"

CATALOGUE_CSV = OUTPUT_DIR / "marc21_catalogue.csv"
CATALOGUE_JSON = OUTPUT_DIR / "marc21_catalogue.json"
MARC_MRK_PATH = OUTPUT_DIR / "marc21_catalogue.mrk"
SCHEMA_JSON = OUTPUT_DIR / "catalogue_schema.json"
MANUAL_REVIEW_CSV = OUTPUT_DIR / "catalogue_manual_review.csv"
MIGRATION_LOG_CSV = OUTPUT_DIR / "catalogue_migration_log.csv"

UNKNOWN = "Unknown"
FILL = "|"  # MARC's "no attempt to code" convention

# Leader/06 Type of record.
EXTENSION_TO_RECORD_TYPE = {
    ".docx": "a", ".doc": "a", ".odt": "a", ".rtf": "a", ".md": "a",
    ".txt": "a", ".tex": "a", ".pdf": "a",
    ".png": "k", ".jpg": "k", ".jpeg": "k", ".gif": "k", ".svg": "k", ".tif": "k", ".tiff": "k",
    ".mp4": "g", ".mov": "g", ".avi": "g", ".mkv": "g",
    ".mp3": "i", ".wav": "i", ".flac": "i",
    ".py": "m", ".js": "m", ".ts": "m", ".java": "m", ".sh": "m", ".ipynb": "m",
    ".csv": "m", ".json": "m", ".xlsx": "m", ".xml": "m", ".sqlite": "m", ".db": "m",
}
RECORD_TYPES = set(EXTENSION_TO_RECORD_TYPE.values())

RECORD_TYPE_TO_RDA_CONTENT_TYPE = {
    "a": "text", "k": "still image", "g": "two-dimensional moving image",
    "i": "spoken word", "m": "computer program",
}

NONFILING_ARTICLES = ("the ", "a ", "an ")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS marc21_catalogue (
    catalogue_id TEXT PRIMARY KEY,
    source_path TEXT UNIQUE,
    relative_path TEXT,
    file_name TEXT,
    leader TEXT,
    field_001 TEXT,
    field_005 TEXT,
    field_008 TEXT,
    title TEXT,
    responsibility_statement TEXT,
    creator_name TEXT,
    rda_content_type TEXT,
    rda_media_type TEXT DEFAULT 'computer',
    rda_carrier_type TEXT DEFAULT 'online resource',
    extent TEXT,
    isbn TEXT,
    issn TEXT,
    other_standard_identifier TEXT,
    general_note TEXT DEFAULT 'Unknown',
    summary TEXT DEFAULT 'Unknown',
    subjects_json TEXT DEFAULT '[]',
    genre TEXT DEFAULT 'Unknown',
    contributors_json TEXT DEFAULT '[]',
    electronic_location TEXT,
    sha256 TEXT,
    file_size_bytes INTEGER,
    explicit_metadata_applied INTEGER DEFAULT 0,
    confidence_status TEXT DEFAULT 'Requires Review',
    notes TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_marc21_sha256 ON marc21_catalogue(sha256);

CREATE TABLE IF NOT EXISTS marc21_counters (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_seq INTEGER NOT NULL DEFAULT 1
);
"""


def get_db(db_path: Path | None = None):
    return common.get_sqlite_db(db_path or DB_PATH, SCHEMA_SQL)


def next_seq(conn) -> int:
    row = conn.execute("SELECT next_seq FROM marc21_counters WHERE id = 1").fetchone()
    seq = row["next_seq"] if row else 1
    conn.execute(
        "INSERT INTO marc21_counters (id, next_seq) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET next_seq = ?",
        (seq + 1, seq + 1),
    )
    return seq


def load_sidecar_metadata(path: Path) -> dict | None:
    sidecar = path.with_name(path.name + ".marc21.json")
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def classify_record_type(path: Path) -> str:
    return EXTENSION_TO_RECORD_TYPE.get(path.suffix.lower(), "m")


def build_leader(record_type: str) -> str:
    """24-position fixed Leader. Positions 00-04 (record length) and 12-16
    (base address of data) use "00000" - the standard placeholder MarcEdit's
    .mrk mnemonic format itself uses, since real values only apply to true
    ISO 2709 binary transmission, not this human-readable serialization."""
    return (
        "00000"      # 00-04 record length (placeholder, see docstring)
        "n"          # 05 record status: New
        + record_type  # 06 type of record
        + "m"        # 07 bibliographic level: Monograph/item
        + " "        # 08 type of control: no specified type
        + "a"        # 09 character coding scheme: UCS/Unicode
        + "2"        # 10 indicator count (fixed)
        + "2"        # 11 subfield code count (fixed)
        + "00000"    # 12-16 base address of data (placeholder, see docstring)
        + "u"        # 17 encoding level: Unknown
        + "u"        # 18 descriptive cataloging form: Unknown
        + " "        # 19 multipart resource record level: not specified
        + "4"        # 20 length of length-of-field portion (fixed)
        + "5"        # 21 length of starting-character-position portion (fixed)
        + "0"        # 22 length of implementation-defined portion (fixed)
        + "0"        # 23 undefined (fixed)
    )


def build_008(date_entered: str, pub_year: str) -> str:
    """40-character 008 control field. See module docstring for the
    honesty rationale behind each position's value."""
    return (
        date_entered   # 00-05 date entered on file (YYMMDD)
        + "s"          # 06 type of date: single known date
        + pub_year     # 07-10 date 1
        + FILL * 4     # 11-14 date 2: not applicable for type 's'
        + FILL * 3     # 15-17 place of publication: not evaluated
        + FILL * 17    # 18-34 format-specific positions: not evaluated
        + "und"        # 35-37 language: undetermined
        + " "          # 38 modified record: not modified
        + "d"          # 39 cataloging source: Other
    )


def _nonfiling_count(title: str) -> int:
    lowered = title.lower()
    for article in NONFILING_ARTICLES:
        if lowered.startswith(article):
            return len(article)
    return 0


def build_record(path: Path, source_root: Path, seq: int, project_config: dict) -> dict:
    stat = path.stat()
    sha256 = common.sha256_file(path)
    sidecar = load_sidecar_metadata(path) or {}

    record_type = sidecar.get("record_type", classify_record_type(path))
    now = datetime.now(timezone.utc)
    date_entered = now.strftime("%y%m%d")
    pub_year = str(datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).year)

    researcher = project_config.get("researcher")
    creator_name = sidecar.get("creator_name", researcher if researcher and researcher != "REPLACE_ME" else None)
    title = sidecar.get("title", path.stem)

    catalogue_id = f"MARC21-{seq:05d}"
    has_explicit = bool(sidecar)
    return {
        "catalogue_id": catalogue_id,
        "source_path": str(path.resolve()),
        "relative_path": str(path.relative_to(source_root)).replace("\\", "/"),
        "file_name": path.name,
        "leader": build_leader(record_type),
        "field_001": catalogue_id,
        "field_005": now.strftime("%Y%m%d%H%M%S.0"),
        "field_008": build_008(date_entered, pub_year),
        "title": title,
        "responsibility_statement": sidecar.get("responsibility_statement", creator_name),
        "creator_name": creator_name,
        "rda_content_type": sidecar.get("rda_content_type", RECORD_TYPE_TO_RDA_CONTENT_TYPE.get(record_type, "computer program")),
        "rda_media_type": sidecar.get("rda_media_type", "computer"),
        "rda_carrier_type": sidecar.get("rda_carrier_type", "online resource"),
        "extent": sidecar.get("extent", f"1 online resource ({stat.st_size} bytes)"),
        "isbn": sidecar.get("isbn"),
        "issn": sidecar.get("issn"),
        "other_standard_identifier": sidecar.get("other_standard_identifier"),
        "general_note": sidecar.get("general_note", UNKNOWN),
        "summary": sidecar.get("summary", UNKNOWN),
        "subjects": sidecar.get("subjects", []),
        "genre": sidecar.get("genre", UNKNOWN),
        "contributors": sidecar.get("contributors", []),
        "electronic_location": sidecar.get("electronic_location", path.resolve().as_uri()),
        "sha256": sha256,
        "file_size_bytes": stat.st_size,
        "explicit_metadata_applied": 1 if has_explicit else 0,
        "confidence_status": "Confident" if has_explicit else "Requires Review",
        "notes": None,
    }


LIST_FIELDS = ("subjects", "contributors")


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
            if path.name.endswith(".marc21.json"):
                continue
            if common.is_excluded(path, source_root):
                excluded += 1
                continue

            source_path = str(path.resolve())
            existing = conn.execute(
                "SELECT catalogue_id FROM marc21_catalogue WHERE source_path = ?", (source_path,)
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
                f"INSERT INTO marc21_catalogue ({', '.join(columns)}, created_at, updated_at) "
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
        raise SystemExit("scan --marc21 requires exactly one of --dry-run or --apply")
    if apply:
        conn = get_db()
        report = _run_scan(conn, env, project_config)
        conn.close()
        print(f"MARC21 scan (applied): {report['scanned']} new files catalogued, "
              f"{report['excluded']} excluded, {report['requires_review']} flagged Requires Review.")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="marc21_scan_dry_run_")
    conn = get_db(tmp_db_path)
    report = _run_scan(conn, env, project_config)
    conn.close()
    print(f"MARC21 scan (--dry-run): {report['scanned']} new files WOULD be catalogued, "
          f"{report['excluded']} excluded, {report['requires_review']} would be flagged Requires Review.")
    print(f"Nothing written to {common.display_path(DB_PATH)}. Working copy left at {tmp_db_path}.")


def _run_migrate(conn, project_config: dict) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    changes = []
    rows = conn.execute("SELECT * FROM marc21_catalogue ORDER BY catalogue_id").fetchall()
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
        for field in ("leader", "rda_content_type", "confidence_status"):
            if row[field] != record[field]:
                changed[field] = (row[field], record[field])
        if changed:
            conn.execute(
                "UPDATE marc21_catalogue SET leader=?, rda_content_type=?, confidence_status=?, "
                "field_005=?, updated_at=? WHERE catalogue_id=?",
                (record["leader"], record["rda_content_type"], record["confidence_status"],
                 record["field_005"], now, row["catalogue_id"]),
            )
            changes.append({"catalogue_id": row["catalogue_id"], "changed_fields": changed})
    conn.commit()
    return changes


def cmd_migrate(project_config: dict, env: dict, dry_run: bool, apply: bool) -> None:
    if dry_run == apply:
        raise SystemExit("migrate --marc21 requires exactly one of --dry-run or --apply")
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
        print(f"MARC21 migrate (applied): {len(changes)} records reclassified -> {common.display_path(MIGRATION_LOG_CSV)}")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="marc21_migrate_dry_run_")
    conn = get_db(tmp_db_path)
    changes = _run_migrate(conn, project_config)
    conn.close()
    print(f"MARC21 migrate (--dry-run): {len(changes)} records WOULD be reclassified.")
    for change in changes[:20]:
        print(f"  {change['catalogue_id']}: {change['changed_fields']}")


def cmd_validate(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --marc21 --apply` first.")
        return
    conn = get_db()
    rows = conn.execute("SELECT * FROM marc21_catalogue ORDER BY catalogue_id").fetchall()

    issues = []
    for row in rows:
        if not Path(row["source_path"]).exists():
            issues.append((row["catalogue_id"], "missing_file", row["source_path"]))
        if len(row["leader"]) != 24:
            issues.append((row["catalogue_id"], "malformed_leader_length", len(row["leader"])))
        elif row["leader"][6] not in RECORD_TYPES:
            issues.append((row["catalogue_id"], "invalid_leader_type_of_record", row["leader"][6]))
        if len(row["field_008"]) != 40:
            issues.append((row["catalogue_id"], "malformed_008_length", len(row["field_008"])))
        if not row["title"]:
            issues.append((row["catalogue_id"], "missing_title", ""))
        if row["general_note"] == UNKNOWN and row["summary"] == UNKNOWN:
            issues.append((row["catalogue_id"], "unresolved_note_and_summary", ""))
        if row["isbn"] and not re.match(r"^[\d-]{10,17}X?$", row["isbn"]):
            issues.append((row["catalogue_id"], "malformed_isbn", row["isbn"]))
    conn.close()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["catalogue_id", "category", "detail"])
        writer.writerows(issues)

    if issues:
        from collections import Counter
        by_category = Counter(i[1] for i in issues)
        print(f"MARC21 validate: {len(issues)} issues across {len(rows)} records -> {common.display_path(MANUAL_REVIEW_CSV)}")
        print(f"By category: {dict(by_category)}")
    else:
        print(f"MARC21 validate: {len(rows)} records, no issues found.")


def _row_to_record(row) -> dict:
    record = dict(row)
    for field in LIST_FIELDS:
        record[field] = json.loads(record.pop(f"{field}_json", "[]") or "[]")
    return record


def _record_to_mrk(record: dict) -> str:
    lines = [f"=LDR  {record['leader']}"]
    lines.append(f"=001  {record['field_001']}")
    lines.append(f"=005  {record['field_005']}")
    lines.append(f"=008  {record['field_008']}")
    if record["isbn"]:
        lines.append(f"=020  \\\\$a{record['isbn']}")
    if record["issn"]:
        lines.append(f"=022  \\\\$a{record['issn']}")
    if record["other_standard_identifier"]:
        lines.append(f"=024  7\\$a{record['other_standard_identifier']}")
    lines.append("=040  \\\\$aresearch-cataloguing-standard$bund")
    if record["creator_name"]:
        lines.append(f"=100  0\\$a{record['creator_name']}")
    nonfiling = _nonfiling_count(record["title"])
    ind1 = "1" if record["creator_name"] else "0"
    title_field = f"=245  {ind1}{nonfiling}$a{record['title']}"
    if record["responsibility_statement"]:
        title_field += f"$c{record['responsibility_statement']}"
    lines.append(title_field)
    lines.append(f"=300  \\\\$a{record['extent']}")
    lines.append(f"=336  \\\\$a{record['rda_content_type']}$2rdacontent")
    lines.append(f"=337  \\\\$a{record['rda_media_type']}$2rdamedia")
    lines.append(f"=338  \\\\$a{record['rda_carrier_type']}$2rdacarrier")
    if record["general_note"] != UNKNOWN:
        lines.append(f"=500  \\\\$a{record['general_note']}")
    if record["summary"] != UNKNOWN:
        lines.append(f"=520  \\\\$a{record['summary']}")
    for subject in record["subjects"]:
        lines.append(f"=650  \\0$a{subject}")
    if record["genre"] != UNKNOWN:
        lines.append(f"=655  \\7$a{record['genre']}")
    for contributor in record["contributors"]:
        lines.append(f"=700  0\\$a{contributor}")
    lines.append(f"=856  0\\$u{record['electronic_location']}")
    return "\n".join(lines)


def cmd_export(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --marc21 --apply` first.")
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    rows = conn.execute("SELECT * FROM marc21_catalogue ORDER BY catalogue_id").fetchall()
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

    mrk_blocks = [_record_to_mrk(record) for record in records]
    MARC_MRK_PATH.write_text("\n\n".join(mrk_blocks) + ("\n" if mrk_blocks else ""), encoding="utf-8")

    SCHEMA_JSON.write_text(json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "MARC21 catalogue record",
        "type": "object",
        "required": ["catalogue_id", "leader", "field_001", "field_008", "title"],
        "properties": {
            "leader": {"type": "string", "minLength": 24, "maxLength": 24},
            "field_008": {"type": "string", "minLength": 40, "maxLength": 40},
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not MIGRATION_LOG_CSV.exists():
        with MIGRATION_LOG_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["timestamp", "catalogue_id", "field", "old_value", "new_value"])
    if not MANUAL_REVIEW_CSV.exists():
        with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["catalogue_id", "category", "detail"])

    print(f"MARC21 export: {len(records)} records -> {common.display_path(OUTPUT_DIR)}/ "
          f"(marc21_catalogue.csv/json, marc21_catalogue.mrk (mnemonic MARC text, all records), "
          f"catalogue_schema.json, catalogue_manual_review.csv, catalogue_migration_log.csv)")
