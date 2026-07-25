#!/usr/bin/env python3
"""METS (Metadata Encoding and Transmission Standard) catalogue mode -
activated by --mets.

A fully separate, additive cataloguing pipeline (see catalogue_common.py for
shared plumbing, dsr_catalogue.py for the isolation pattern this follows):
its own database (instance/catalogue_mets.db) and its own output directory
(instance/catalogued_files/mets/). Never opens or writes
instance/catalogue.db or any other standard's outputs.

METS packages multiple files into one intellectual object via a
metsHdr/dmdSec/amdSec/fileSec/structMap document - one per configured
SOURCE_DATA_ROOTS entry, mirroring ro_crate_catalogue.py's and
dcat_catalogue.py's per-root grouping. Unlike RO-Crate (which this project
deliberately keeps flat - see ro_crate_catalogue.py), METS's defining
purpose is exactly the structural relationship between files, so
export builds a real nested structMap <div> tree mirroring the source
root's actual directory hierarchy rather than a flat list - the one place
across these ten modules where reproducing that nesting is the standard's
whole point rather than an unnecessary complication.

  - fileSec/file  : one per catalogued file, with a real CHECKSUM/
                    CHECKSUMTYPE (SHA-256), MIMETYPE, SIZE, CREATED, and an
                    FLocat pointing to the file's location.
  - amdSec/techMD : a lightweight inline technical-metadata block (size/
                    format/checksum). Real METS deployments commonly point
                    techMD at a full PREMIS object instead - this project's
                    own --premis mode can independently produce exactly that
                    (see premis_catalogue.py); duplicating PREMIS's full
                    object model inside METS here would be redundant, so
                    this techMD block stays intentionally minimal.
  - dmdSec        : a minimal descriptive block (title = the source root's
                    directory name) at the object level - not a full Dublin
                    Core/MODS record, since those are exactly what this
                    project's own --dublin-core/--mods modes already produce
                    for every file, independently.
  - structMap     : a real <div> tree mirroring the directory hierarchy,
                    with a leaf <div><fptr FILEID="..."/></div> per file.

Nothing here invents metadata: `label`/`title` default to the filename,
`description` defaults to "Unknown". A <file>.mets.json sidecar can supply
either explicitly.
"""
from __future__ import annotations

import csv
import json
import mimetypes
import re
import xml.sax.saxutils as saxutils
from datetime import datetime, timezone
from pathlib import Path

import catalogue_common as common

DB_PATH = common.INSTANCE_DIR / "catalogue_mets.db"
OUTPUT_DIR = common.CATALOGUE_DIR / "mets"

CATALOGUE_CSV = OUTPUT_DIR / "mets_catalogue.csv"
CATALOGUE_JSON = OUTPUT_DIR / "mets_catalogue.json"
METS_XML_DIR = OUTPUT_DIR / "packages"
SCHEMA_JSON = OUTPUT_DIR / "catalogue_schema.json"
MANUAL_REVIEW_CSV = OUTPUT_DIR / "catalogue_manual_review.csv"
MIGRATION_LOG_CSV = OUTPUT_DIR / "catalogue_migration_log.csv"

UNKNOWN = "Unknown"
METS_NAMESPACE = "http://www.loc.gov/METS/"
XLINK_NAMESPACE = "http://www.w3.org/1999/xlink"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mets_catalogue (
    catalogue_id TEXT PRIMARY KEY,
    source_path TEXT UNIQUE,
    source_root TEXT,
    relative_path TEXT,
    file_name TEXT,
    file_id TEXT,
    label TEXT,
    description TEXT DEFAULT 'Unknown',
    mimetype TEXT,
    size INTEGER,
    created TEXT,
    checksum TEXT,
    checksum_type TEXT DEFAULT 'SHA-256',
    location_url TEXT,
    explicit_metadata_applied INTEGER DEFAULT 0,
    confidence_status TEXT DEFAULT 'Requires Review',
    notes TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_mets_checksum ON mets_catalogue(checksum);
CREATE INDEX IF NOT EXISTS idx_mets_source_root ON mets_catalogue(source_root);

CREATE TABLE IF NOT EXISTS mets_counters (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    next_seq INTEGER NOT NULL DEFAULT 1
);
"""


def get_db(db_path: Path | None = None):
    return common.get_sqlite_db(db_path or DB_PATH, SCHEMA_SQL)


def next_seq(conn) -> int:
    row = conn.execute("SELECT next_seq FROM mets_counters WHERE id = 1").fetchone()
    seq = row["next_seq"] if row else 1
    conn.execute(
        "INSERT INTO mets_counters (id, next_seq) VALUES (1, ?) "
        "ON CONFLICT(id) DO UPDATE SET next_seq = ?",
        (seq + 1, seq + 1),
    )
    return seq


def load_sidecar_metadata(path: Path) -> dict | None:
    sidecar = path.with_name(path.name + ".mets.json")
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def build_record(path: Path, source_root: Path, seq: int) -> dict:
    stat = path.stat()
    sha256 = common.sha256_file(path)
    sidecar = load_sidecar_metadata(path) or {}

    mime_type, _ = mimetypes.guess_type(path.name)
    created_iso = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat()

    catalogue_id = f"METS-{seq:05d}"
    has_explicit = bool(sidecar)
    return {
        "catalogue_id": catalogue_id,
        "source_path": str(path.resolve()),
        "source_root": str(source_root.resolve()),
        "relative_path": str(path.relative_to(source_root)).replace("\\", "/"),
        "file_name": path.name,
        "file_id": f"FILE_{seq:05d}",
        "label": sidecar.get("label", path.name),
        "description": sidecar.get("description", UNKNOWN),
        "mimetype": sidecar.get("mimetype", mime_type or UNKNOWN),
        "size": stat.st_size,
        "created": sidecar.get("created", created_iso),
        "checksum": sha256,
        "checksum_type": "SHA-256",
        "location_url": sidecar.get("location_url", path.resolve().as_uri()),
        "explicit_metadata_applied": 1 if has_explicit else 0,
        "confidence_status": "Confident" if (has_explicit or mime_type) else "Requires Review",
        "notes": None,
    }


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
            if path.name.endswith(".mets.json"):
                continue
            if common.is_excluded(path, source_root):
                excluded += 1
                continue

            source_path = str(path.resolve())
            existing = conn.execute(
                "SELECT catalogue_id FROM mets_catalogue WHERE source_path = ?", (source_path,)
            ).fetchone()
            if existing:
                continue

            seq = next_seq(conn)
            record = build_record(path, source_root, seq)

            columns = list(record.keys())
            values = list(record.values())
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO mets_catalogue ({', '.join(columns)}, created_at, updated_at) "
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
        raise SystemExit("scan --mets requires exactly one of --dry-run or --apply")
    if apply:
        conn = get_db()
        report = _run_scan(conn, env)
        conn.close()
        print(f"METS scan (applied): {report['scanned']} new files catalogued, "
              f"{report['excluded']} excluded, {report['requires_review']} flagged Requires Review.")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="mets_scan_dry_run_")
    conn = get_db(tmp_db_path)
    report = _run_scan(conn, env)
    conn.close()
    print(f"METS scan (--dry-run): {report['scanned']} new files WOULD be catalogued, "
          f"{report['excluded']} excluded, {report['requires_review']} would be flagged Requires Review.")
    print(f"Nothing written to {common.display_path(DB_PATH)}. Working copy left at {tmp_db_path}.")


def _run_migrate(conn) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    changes = []
    rows = conn.execute("SELECT * FROM mets_catalogue ORDER BY catalogue_id").fetchall()
    for row in rows:
        source_path = Path(row["source_path"])
        if not source_path.exists():
            continue
        source_root = Path(row["source_root"])
        seq = int(row["catalogue_id"].split("-")[1])
        record = build_record(source_path, source_root, seq)

        changed = {}
        for field in ("mimetype", "confidence_status"):
            if row[field] != record[field]:
                changed[field] = (row[field], record[field])
        if changed:
            conn.execute(
                "UPDATE mets_catalogue SET mimetype=?, confidence_status=?, updated_at=? WHERE catalogue_id=?",
                (record["mimetype"], record["confidence_status"], now, row["catalogue_id"]),
            )
            changes.append({"catalogue_id": row["catalogue_id"], "changed_fields": changed})
    conn.commit()
    return changes


def cmd_migrate(project_config: dict, env: dict, dry_run: bool, apply: bool) -> None:
    if dry_run == apply:
        raise SystemExit("migrate --mets requires exactly one of --dry-run or --apply")
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
        print(f"METS migrate (applied): {len(changes)} records reclassified -> {common.display_path(MIGRATION_LOG_CSV)}")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="mets_migrate_dry_run_")
    conn = get_db(tmp_db_path)
    changes = _run_migrate(conn)
    conn.close()
    print(f"METS migrate (--dry-run): {len(changes)} records WOULD be reclassified.")
    for change in changes[:20]:
        print(f"  {change['catalogue_id']}: {change['changed_fields']}")


def cmd_validate(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --mets --apply` first.")
        return
    conn = get_db()
    rows = conn.execute("SELECT * FROM mets_catalogue ORDER BY catalogue_id").fetchall()

    issues = []
    seen_ids: dict[tuple, str] = {}
    for row in rows:
        if not Path(row["source_path"]).exists():
            issues.append((row["catalogue_id"], "missing_file", row["source_path"]))
        if not re.match(r"^[A-Za-z_][\w.-]*$", row["file_id"]):
            issues.append((row["catalogue_id"], "invalid_xml_id", row["file_id"]))
        key = (row["source_root"], row["file_id"])
        if key in seen_ids:
            issues.append((row["catalogue_id"], "duplicate_file_id_within_package",
                            f"also used by {seen_ids[key]}"))
        else:
            seen_ids[key] = row["catalogue_id"]
        if row["checksum_type"] != "SHA-256" or not row["checksum"]:
            issues.append((row["catalogue_id"], "missing_or_invalid_checksum", row["checksum"]))
        if row["size"] is None or row["size"] < 0:
            issues.append((row["catalogue_id"], "invalid_size", row["size"]))
    conn.close()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["catalogue_id", "category", "detail"])
        writer.writerows(issues)

    if issues:
        from collections import Counter
        by_category = Counter(i[1] for i in issues)
        print(f"METS validate: {len(issues)} issues across {len(rows)} records -> {common.display_path(MANUAL_REVIEW_CSV)}")
        print(f"By category: {dict(by_category)}")
    else:
        print(f"METS validate: {len(rows)} records, no issues found.")


def _build_struct_tree(records: list[dict]) -> dict:
    root: dict = {}
    for record in records:
        parts = Path(record["relative_path"]).parts
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node.setdefault("__files__", []).append(record)
    return root


def _render_struct_div(node: dict, label: str, indent: str) -> list[str]:
    esc = saxutils.escape
    lines = [f'{indent}<div TYPE="directory" LABEL="{esc(label)}">']
    for key in sorted(k for k in node if k != "__files__"):
        lines.extend(_render_struct_div(node[key], key, indent + "  "))
    for record in sorted(node.get("__files__", []), key=lambda r: r["file_name"]):
        lines.append(
            f'{indent}  <div TYPE="file" LABEL="{esc(record["label"])}">'
            f'<fptr FILEID="{esc(record["file_id"])}"/></div>'
        )
    lines.append(f"{indent}</div>")
    return lines


def _build_mets_xml(root_name: str, records: list[dict]) -> str:
    esc = saxutils.escape
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<mets xmlns="{METS_NAMESPACE}" xmlns:xlink="{XLINK_NAMESPACE}">',
        f'  <metsHdr CREATEDATE="{now}">',
        '    <agent ROLE="CREATOR" TYPE="ORGANIZATION">',
        "      <name>research-cataloguing-standard mets_catalogue.py</name>",
        "    </agent>",
        "  </metsHdr>",
        '  <dmdSec ID="DMD_001">',
        '    <mdWrap MDTYPE="OTHER">',
        "      <xmlData>",
        f"        <title>{esc(root_name)}</title>",
        "      </xmlData>",
        "    </mdWrap>",
        "  </dmdSec>",
        '  <amdSec ID="AMD_001">',
    ]
    for record in records:
        lines.append(f'    <techMD ID="TECH_{record["file_id"]}">')
        lines.append('      <mdWrap MDTYPE="OTHER">')
        lines.append("        <xmlData>")
        lines.append(f'          <size>{record["size"]}</size>')
        lines.append(f'          <format>{esc(str(record["mimetype"]))}</format>')
        lines.append(f'          <checksum type="{esc(record["checksum_type"])}">{esc(record["checksum"])}</checksum>')
        lines.append("        </xmlData>")
        lines.append("      </mdWrap>")
        lines.append("    </techMD>")
    lines.append("  </amdSec>")
    lines.append('  <fileSec>')
    lines.append('    <fileGrp ID="FILEGRP_001">')
    for record in records:
        lines.append(
            f'      <file ID="{esc(record["file_id"])}" MIMETYPE="{esc(str(record["mimetype"]))}" '
            f'SIZE="{record["size"]}" CREATED="{esc(record["created"])}" '
            f'CHECKSUM="{esc(record["checksum"])}" CHECKSUMTYPE="{esc(record["checksum_type"])}">'
        )
        lines.append(f'        <FLocat LOCTYPE="URL" xlink:href="{esc(record["location_url"])}"/>')
        lines.append("      </file>")
    lines.append("    </fileGrp>")
    lines.append("  </fileSec>")
    lines.append('  <structMap TYPE="physical">')
    tree = _build_struct_tree(records)
    lines.extend(_render_struct_div(tree, root_name, "    "))
    lines.append("  </structMap>")
    lines.append("</mets>")
    return "\n".join(lines)


def cmd_export(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --mets --apply` first.")
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    METS_XML_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    rows = conn.execute("SELECT * FROM mets_catalogue ORDER BY catalogue_id").fetchall()
    conn.close()
    records = [dict(r) for r in rows]

    if records:
        fieldnames = list(records[0].keys())
        with CATALOGUE_CSV.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(records)
    else:
        CATALOGUE_CSV.write_text("", encoding="utf-8")

    CATALOGUE_JSON.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    by_root: dict[str, list[dict]] = {}
    for record in records:
        by_root.setdefault(record["source_root"], []).append(record)

    package_count = 0
    for source_root, root_records in by_root.items():
        root_name = Path(source_root).name or f"package-{package_count}"
        package_dir = METS_XML_DIR / root_name
        package_dir.mkdir(parents=True, exist_ok=True)
        (package_dir / "mets.xml").write_text(_build_mets_xml(root_name, root_records), encoding="utf-8")
        package_count += 1

    SCHEMA_JSON.write_text(json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "METS catalogue record",
        "type": "object",
        "required": ["catalogue_id", "file_id", "checksum", "checksum_type", "size"],
        "properties": {
            "file_id": {"type": "string", "pattern": r"^[A-Za-z_]"},
            "checksum_type": {"type": "string", "const": "SHA-256"},
            "size": {"type": "integer", "minimum": 0},
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not MIGRATION_LOG_CSV.exists():
        with MIGRATION_LOG_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["timestamp", "catalogue_id", "field", "old_value", "new_value"])
    if not MANUAL_REVIEW_CSV.exists():
        with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["catalogue_id", "category", "detail"])

    print(f"METS export: {len(records)} records across {package_count} package(s) -> "
          f"{common.display_path(OUTPUT_DIR)}/ (mets_catalogue.csv/json, "
          f"packages/<root-name>/mets.xml per package, catalogue_schema.json, "
          f"catalogue_manual_review.csv, catalogue_migration_log.csv)")
