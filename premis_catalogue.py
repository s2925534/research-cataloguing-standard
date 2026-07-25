#!/usr/bin/env python3
"""PREMIS (Preservation Metadata Implementation Strategies) catalogue mode -
activated by --premis.

A fully separate, additive cataloguing pipeline (see catalogue_common.py for
shared plumbing, dsr_catalogue.py for the isolation pattern this follows):
its own database (instance/catalogue_premis.db) and its own output directory
(instance/catalogued_files/premis/). Never opens or writes
instance/catalogue.db or any other standard's outputs.

PREMIS is structurally different from every other module here: preservation
metadata is fundamentally an append-only *event log* over time, not just a
per-file snapshot. This engine has two tables, not one:

  - premis_objects : one row per file (PREMIS Object entity) - identity,
                     fixity (SHA-256 message digest), format, size.
  - premis_events   : append-only. `scan --premis --apply` logs an
                     "ingestion" event the first time a file is seen, and a
                     "fixity check" event every subsequent time (recomputing
                     the checksum and comparing it to the last known value -
                     outcome "success" if unchanged, "warning" if the
                     content has drifted since the last check). This is
                     genuinely what PREMIS fixity checking is for: repeated
                     verification over time, not a one-off classification.
                     `migrate --premis` logs a "migration" event only when a
                     file's technical characteristics (format) actually
                     change - it does not duplicate scan's fixity checking.

A single Agent (PREMIS agentType "software") represents this tool itself
and is referenced by every event's linkingAgentIdentifier - a true,
deterministic fact about what produced the record, not invented data about
the file.

Nothing here invents metadata: `creating_application` defaults to "Unknown"
(the engine cannot tell what software actually produced a file from its
bytes alone), `preservation_level` defaults to "Not Assigned" (a real
PREMIS concept representing an institutional preservation *commitment* this
engine has no authority to assign on its own). `format_name` uses the
file's MIME type rather than a resolved PRONOM PUID, since PRONOM
identification requires file-signature analysis against a format registry
this project doesn't have access to - stated here rather than silently
approximated as something more authoritative-looking. A <file>.premis.json
sidecar can supply any field explicitly.

Scope note on the exported XML: real PREMIS deployments keep Object/Event/
Agent as independently identified top-level entities cross-linked by
identifier (exactly how the two SQLite tables here are structured). The
exported per-object XML nests each object's related events directly inside
its own file purely for single-file readability - a legitimate simplified
serialization pattern, not a claim that this is PREMIS's canonical XML shape.
"""
from __future__ import annotations

import csv
import json
import mimetypes
import xml.sax.saxutils as saxutils
from datetime import datetime, timezone
from pathlib import Path

import catalogue_common as common

DB_PATH = common.INSTANCE_DIR / "catalogue_premis.db"
OUTPUT_DIR = common.CATALOGUE_DIR / "premis"

OBJECTS_CSV = OUTPUT_DIR / "premis_objects.csv"
OBJECTS_JSON = OUTPUT_DIR / "premis_objects.json"
EVENTS_CSV = OUTPUT_DIR / "premis_events.csv"
EVENTS_JSON = OUTPUT_DIR / "premis_events.json"
PREMIS_XML_DIR = OUTPUT_DIR / "premis_xml"
SCHEMA_JSON = OUTPUT_DIR / "catalogue_schema.json"
MANUAL_REVIEW_CSV = OUTPUT_DIR / "catalogue_manual_review.csv"
MIGRATION_LOG_CSV = OUTPUT_DIR / "catalogue_migration_log.csv"

UNKNOWN = "Unknown"
NOT_ASSIGNED = "Not Assigned"
AGENT_NAME = "research-cataloguing-standard premis_catalogue.py"
AGENT_TYPE = "software"

EVENT_TYPES = ["ingestion", "fixity check", "migration", "validation", "normalization"]
EVENT_OUTCOMES = ["success", "warning", "failure"]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS premis_objects (
    catalogue_id TEXT PRIMARY KEY,
    source_path TEXT UNIQUE,
    relative_path TEXT,
    file_name TEXT,
    object_identifier_type TEXT DEFAULT 'local',
    object_identifier_value TEXT,
    object_category TEXT DEFAULT 'file',
    format_name TEXT,
    format_version TEXT DEFAULT 'Unknown',
    message_digest TEXT,
    message_digest_algorithm TEXT DEFAULT 'SHA-256',
    size_bytes INTEGER,
    creating_application TEXT DEFAULT 'Unknown',
    preservation_level TEXT DEFAULT 'Not Assigned',
    storage_location TEXT,
    explicit_metadata_applied INTEGER DEFAULT 0,
    confidence_status TEXT DEFAULT 'Requires Review',
    notes TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_premis_message_digest ON premis_objects(message_digest);

CREATE TABLE IF NOT EXISTS premis_events (
    event_id TEXT PRIMARY KEY,
    object_catalogue_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_datetime TEXT NOT NULL,
    event_detail TEXT,
    event_outcome TEXT NOT NULL,
    event_outcome_detail TEXT,
    linking_agent TEXT DEFAULT '""" + AGENT_NAME + """'
);

CREATE INDEX IF NOT EXISTS idx_premis_events_object ON premis_events(object_catalogue_id);

CREATE TABLE IF NOT EXISTS premis_counters (
    kind TEXT PRIMARY KEY,
    next_seq INTEGER NOT NULL DEFAULT 1
);
"""


def get_db(db_path: Path | None = None):
    return common.get_sqlite_db(db_path or DB_PATH, SCHEMA_SQL)


def next_seq(conn, kind: str) -> int:
    row = conn.execute("SELECT next_seq FROM premis_counters WHERE kind = ?", (kind,)).fetchone()
    seq = row["next_seq"] if row else 1
    conn.execute(
        "INSERT INTO premis_counters (kind, next_seq) VALUES (?, ?) "
        "ON CONFLICT(kind) DO UPDATE SET next_seq = ?",
        (kind, seq + 1, seq + 1),
    )
    return seq


def load_sidecar_metadata(path: Path) -> dict | None:
    sidecar = path.with_name(path.name + ".premis.json")
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def build_object_record(path: Path, source_root: Path, seq: int, sha256: str) -> dict:
    stat = path.stat()
    sidecar = load_sidecar_metadata(path) or {}
    mime_type, _ = mimetypes.guess_type(path.name)

    has_explicit = bool(sidecar)
    return {
        "catalogue_id": f"PREMIS-{seq:05d}",
        "source_path": str(path.resolve()),
        "relative_path": str(path.relative_to(source_root)).replace("\\", "/"),
        "file_name": path.name,
        "object_identifier_type": "local",
        "object_identifier_value": f"urn:premis:sha256:{sha256}",
        "object_category": "file",
        "format_name": sidecar.get("format_name", mime_type or UNKNOWN),
        "format_version": sidecar.get("format_version", UNKNOWN),
        "message_digest": sha256,
        "message_digest_algorithm": "SHA-256",
        "size_bytes": stat.st_size,
        "creating_application": sidecar.get("creating_application", UNKNOWN),
        "preservation_level": sidecar.get("preservation_level", NOT_ASSIGNED),
        "storage_location": sidecar.get("storage_location", path.resolve().as_uri()),
        "explicit_metadata_applied": 1 if has_explicit else 0,
        "confidence_status": "Confident" if (has_explicit or mime_type) else "Requires Review",
        "notes": None,
    }


def _log_event(conn, object_catalogue_id: str, event_type: str, detail: str,
                outcome: str, outcome_detail: str) -> str:
    seq = next_seq(conn, "event")
    event_id = f"EVENT-{seq:06d}"
    conn.execute(
        "INSERT INTO premis_events (event_id, object_catalogue_id, event_type, event_datetime, "
        "event_detail, event_outcome, event_outcome_detail, linking_agent) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (event_id, object_catalogue_id, event_type, datetime.now(timezone.utc).isoformat(),
         detail, outcome, outcome_detail, AGENT_NAME),
    )
    return event_id


def _run_scan(conn, env: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    ingested = 0
    fixity_checked = 0
    fixity_warnings = 0
    excluded = 0

    for source_root in common.source_roots_from_env(env):
        if not source_root.exists():
            print(f"WARNING: source root does not exist, skipping: {source_root}")
            continue
        for path in source_root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            if path.name.endswith(".premis.json"):
                continue
            if common.is_excluded(path, source_root):
                excluded += 1
                continue

            source_path = str(path.resolve())
            existing = conn.execute(
                "SELECT catalogue_id, message_digest FROM premis_objects WHERE source_path = ?",
                (source_path,),
            ).fetchone()
            sha256 = common.sha256_file(path)

            if existing:
                fixity_checked += 1
                if existing["message_digest"] == sha256:
                    _log_event(conn, existing["catalogue_id"], "fixity check",
                               "Recomputed checksum during scan --premis --apply",
                               "success", "Checksum matches previous record.")
                else:
                    _log_event(conn, existing["catalogue_id"], "fixity check",
                               "Recomputed checksum during scan --premis --apply",
                               "warning",
                               f"Checksum changed since last check (was {existing['message_digest']}, now {sha256}).")
                    conn.execute(
                        "UPDATE premis_objects SET message_digest = ?, updated_at = ? WHERE catalogue_id = ?",
                        (sha256, now, existing["catalogue_id"]),
                    )
                    fixity_warnings += 1
                continue

            seq = next_seq(conn, "object")
            record = build_object_record(path, source_root, seq, sha256)
            columns = list(record.keys())
            values = list(record.values())
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT INTO premis_objects ({', '.join(columns)}, created_at, updated_at) "
                f"VALUES ({placeholders}, ?, ?)",
                (*values, now, now),
            )
            _log_event(conn, record["catalogue_id"], "ingestion",
                       "Initial ingestion via scan --premis --apply", "success",
                       "Object first catalogued; baseline fixity established.")
            ingested += 1
            if (ingested + fixity_checked) % 200 == 0:
                conn.commit()

    conn.commit()
    return {"ingested": ingested, "fixity_checked": fixity_checked,
            "fixity_warnings": fixity_warnings, "excluded": excluded}


def cmd_scan(project_config: dict, env: dict, dry_run: bool, apply: bool) -> None:
    if dry_run == apply:
        raise SystemExit("scan --premis requires exactly one of --dry-run or --apply")
    if apply:
        conn = get_db()
        report = _run_scan(conn, env)
        conn.close()
        print(f"PREMIS scan (applied): {report['ingested']} new objects ingested, "
              f"{report['fixity_checked']} existing objects fixity-checked "
              f"({report['fixity_warnings']} checksum-changed warnings), {report['excluded']} excluded.")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="premis_scan_dry_run_")
    conn = get_db(tmp_db_path)
    report = _run_scan(conn, env)
    conn.close()
    print(f"PREMIS scan (--dry-run): {report['ingested']} new objects WOULD be ingested, "
          f"{report['fixity_checked']} existing objects WOULD be fixity-checked "
          f"({report['fixity_warnings']} checksum-changed warnings), {report['excluded']} excluded.")
    print(f"Nothing written to {common.display_path(DB_PATH)}. Working copy left at {tmp_db_path}.")


def _run_migrate(conn) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    changes = []
    rows = conn.execute("SELECT * FROM premis_objects ORDER BY catalogue_id").fetchall()
    for row in rows:
        source_path = Path(row["source_path"])
        if not source_path.exists():
            continue
        source_root = source_path
        for _ in range(len(Path(row["relative_path"]).parts)):
            source_root = source_root.parent
        seq = int(row["catalogue_id"].split("-")[1])
        record = build_object_record(source_path, source_root, seq, row["message_digest"])

        if row["format_name"] != record["format_name"]:
            conn.execute(
                "UPDATE premis_objects SET format_name = ?, updated_at = ? WHERE catalogue_id = ?",
                (record["format_name"], now, row["catalogue_id"]),
            )
            _log_event(conn, row["catalogue_id"], "migration",
                       "Technical characteristics re-evaluated during migrate --premis",
                       "success", f"format_name changed from {row['format_name']} to {record['format_name']}.")
            changes.append({"catalogue_id": row["catalogue_id"],
                             "changed_fields": {"format_name": (row["format_name"], record["format_name"])}})
    conn.commit()
    return changes


def cmd_migrate(project_config: dict, env: dict, dry_run: bool, apply: bool) -> None:
    if dry_run == apply:
        raise SystemExit("migrate --premis requires exactly one of --dry-run or --apply")
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
        print(f"PREMIS migrate (applied): {len(changes)} objects had a migration event logged -> {common.display_path(MIGRATION_LOG_CSV)}")
        return

    tmp_dir, tmp_db_path = common.dry_run_db_copy(DB_PATH, prefix="premis_migrate_dry_run_")
    conn = get_db(tmp_db_path)
    changes = _run_migrate(conn)
    conn.close()
    print(f"PREMIS migrate (--dry-run): {len(changes)} objects WOULD have a migration event logged.")
    for change in changes[:20]:
        print(f"  {change['catalogue_id']}: {change['changed_fields']}")


def cmd_validate(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --premis --apply` first.")
        return
    conn = get_db()
    objects = conn.execute("SELECT * FROM premis_objects ORDER BY catalogue_id").fetchall()
    events = conn.execute("SELECT * FROM premis_events ORDER BY event_id").fetchall()
    object_ids = {row["catalogue_id"] for row in objects}

    issues = []
    for row in objects:
        if not Path(row["source_path"]).exists():
            issues.append((row["catalogue_id"], "missing_file", row["source_path"]))
        if row["message_digest_algorithm"] != "SHA-256":
            issues.append((row["catalogue_id"], "invalid_digest_algorithm", row["message_digest_algorithm"]))
        if not row["message_digest"]:
            issues.append((row["catalogue_id"], "missing_message_digest", ""))
        if row["size_bytes"] is None or row["size_bytes"] < 0:
            issues.append((row["catalogue_id"], "invalid_size", row["size_bytes"]))

    for row in events:
        if row["object_catalogue_id"] not in object_ids:
            issues.append((row["event_id"], "orphaned_event", f"references missing object {row['object_catalogue_id']}"))
        if row["event_type"] not in EVENT_TYPES:
            issues.append((row["event_id"], "invalid_event_type", row["event_type"]))
        if row["event_outcome"] not in EVENT_OUTCOMES:
            issues.append((row["event_id"], "invalid_event_outcome", row["event_outcome"]))
    conn.close()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["catalogue_id_or_event_id", "category", "detail"])
        writer.writerows(issues)

    warning_events = [e for e in events if e["event_outcome"] == "warning"]
    if issues:
        from collections import Counter
        by_category = Counter(i[1] for i in issues)
        print(f"PREMIS validate: {len(issues)} issues across {len(objects)} objects/{len(events)} events -> "
              f"{common.display_path(MANUAL_REVIEW_CSV)}")
        print(f"By category: {dict(by_category)}")
    else:
        print(f"PREMIS validate: {len(objects)} objects, {len(events)} events, no structural issues found.")
    if warning_events:
        print(f"NOTE: {len(warning_events)} fixity-check event(s) recorded a checksum change since last check - "
              f"review these objects for unintended content drift.")


def cmd_export(project_config: dict, env: dict) -> None:
    if not DB_PATH.exists():
        print(f"{common.display_path(DB_PATH)} does not exist yet - run `scan --premis --apply` first.")
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PREMIS_XML_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    objects = [dict(r) for r in conn.execute("SELECT * FROM premis_objects ORDER BY catalogue_id").fetchall()]
    events = [dict(r) for r in conn.execute("SELECT * FROM premis_events ORDER BY event_id").fetchall()]
    conn.close()

    if objects:
        with OBJECTS_CSV.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(objects[0].keys()))
            writer.writeheader()
            writer.writerows(objects)
    else:
        OBJECTS_CSV.write_text("", encoding="utf-8")
    OBJECTS_JSON.write_text(json.dumps(objects, ensure_ascii=False, indent=2), encoding="utf-8")

    if events:
        with EVENTS_CSV.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(events[0].keys()))
            writer.writeheader()
            writer.writerows(events)
    else:
        EVENTS_CSV.write_text("", encoding="utf-8")
    EVENTS_JSON.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")

    events_by_object: dict[str, list[dict]] = {}
    for event in events:
        events_by_object.setdefault(event["object_catalogue_id"], []).append(event)

    esc = saxutils.escape
    for obj in objects:
        obj_events = events_by_object.get(obj["catalogue_id"], [])
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<premis:premis xmlns:premis="http://www.loc.gov/premis/v3" version="3.0">',
            '  <premis:object xsi:type="premis:file" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
            "    <premis:objectIdentifier>",
            f'      <premis:objectIdentifierType>{esc(obj["object_identifier_type"])}</premis:objectIdentifierType>',
            f'      <premis:objectIdentifierValue>{esc(obj["object_identifier_value"])}</premis:objectIdentifierValue>',
            "    </premis:objectIdentifier>",
            "    <premis:objectCharacteristics>",
            "      <premis:fixity>",
            f'        <premis:messageDigestAlgorithm>{esc(obj["message_digest_algorithm"])}</premis:messageDigestAlgorithm>',
            f'        <premis:messageDigest>{esc(obj["message_digest"])}</premis:messageDigest>',
            "      </premis:fixity>",
            f'      <premis:size>{obj["size_bytes"]}</premis:size>',
            "      <premis:format>",
            "        <premis:formatDesignation>",
            f'          <premis:formatName>{esc(str(obj["format_name"]))}</premis:formatName>',
            "        </premis:formatDesignation>",
            "      </premis:format>",
            "    </premis:objectCharacteristics>",
            f'    <premis:preservationLevel><premis:preservationLevelValue>{esc(str(obj["preservation_level"]))}</premis:preservationLevelValue></premis:preservationLevel>',
            "    <premis:storage>",
            f'      <premis:contentLocation><premis:contentLocationValue>{esc(str(obj["storage_location"]))}</premis:contentLocationValue></premis:contentLocation>',
            "    </premis:storage>",
            "  </premis:object>",
        ]
        for event in obj_events:
            lines.append("  <premis:event>")
            lines.append("    <premis:eventIdentifier>")
            lines.append(f'      <premis:eventIdentifierValue>{esc(event["event_id"])}</premis:eventIdentifierValue>')
            lines.append("    </premis:eventIdentifier>")
            lines.append(f'    <premis:eventType>{esc(event["event_type"])}</premis:eventType>')
            lines.append(f'    <premis:eventDateTime>{esc(event["event_datetime"])}</premis:eventDateTime>')
            if event["event_detail"]:
                lines.append(f'    <premis:eventDetailInformation><premis:eventDetail>{esc(event["event_detail"])}</premis:eventDetail></premis:eventDetailInformation>')
            lines.append("    <premis:eventOutcomeInformation>")
            lines.append(f'      <premis:eventOutcome>{esc(event["event_outcome"])}</premis:eventOutcome>')
            if event["event_outcome_detail"]:
                lines.append(f'      <premis:eventOutcomeDetail><premis:eventOutcomeDetailNote>{esc(event["event_outcome_detail"])}</premis:eventOutcomeDetailNote></premis:eventOutcomeDetail>')
            lines.append("    </premis:eventOutcomeInformation>")
            lines.append(f'    <premis:linkingAgentIdentifier><premis:linkingAgentIdentifierValue>{esc(event["linking_agent"])}</premis:linkingAgentIdentifierValue></premis:linkingAgentIdentifier>')
            lines.append("  </premis:event>")
        lines.append("  <premis:agent>")
        lines.append(f'    <premis:agentIdentifier><premis:agentIdentifierValue>{esc(AGENT_NAME)}</premis:agentIdentifierValue></premis:agentIdentifier>')
        lines.append(f'    <premis:agentName>{esc(AGENT_NAME)}</premis:agentName>')
        lines.append(f'    <premis:agentType>{esc(AGENT_TYPE)}</premis:agentType>')
        lines.append("  </premis:agent>")
        lines.append("</premis:premis>")

        xml_path = PREMIS_XML_DIR / f"{obj['catalogue_id']}.xml"
        xml_path.write_text("\n".join(lines), encoding="utf-8")

    SCHEMA_JSON.write_text(json.dumps({
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "PREMIS catalogue records",
        "type": "object",
        "properties": {
            "object": {
                "type": "object",
                "required": ["catalogue_id", "object_identifier_value", "message_digest", "message_digest_algorithm"],
                "properties": {"message_digest_algorithm": {"type": "string", "const": "SHA-256"}},
            },
            "event": {
                "type": "object",
                "required": ["event_id", "object_catalogue_id", "event_type", "event_outcome"],
                "properties": {
                    "event_type": {"type": "string", "enum": EVENT_TYPES},
                    "event_outcome": {"type": "string", "enum": EVENT_OUTCOMES},
                },
            },
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not MIGRATION_LOG_CSV.exists():
        with MIGRATION_LOG_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["timestamp", "catalogue_id", "field", "old_value", "new_value"])
    if not MANUAL_REVIEW_CSV.exists():
        with MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["catalogue_id_or_event_id", "category", "detail"])

    print(f"PREMIS export: {len(objects)} objects, {len(events)} events -> {common.display_path(OUTPUT_DIR)}/ "
          f"(premis_objects.csv/json, premis_events.csv/json, premis_xml/<id>.xml per object, "
          f"catalogue_schema.json, catalogue_manual_review.csv, catalogue_migration_log.csv)")
