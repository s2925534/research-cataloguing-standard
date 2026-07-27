#!/usr/bin/env python3
"""One-off migration utility: legacy engine (instance/catalogue.db) -> DSR
catalogue (instance/catalogue_dsr.db), plus reference-marker rewriting for
files that cite legacy catalogue IDs.

Three independent stages, each explicit and inspectable before the next runs:

  1. crosswalk        - reads instance/catalogue.db READ-ONLY (sqlite URI
                         mode=ro - this module can never write to it, by
                         construction, not just by convention), classifies
                         each legacy record's underlying file through the
                         real DSR classifier (catalogues.dsr_catalogue.
                         classify_file - the same function a normal --dsr
                         scan uses), and mints DSR entries via the same
                         get_dsr_db()/next_dsr_seq() the DSR module already
                         uses (shared counters, so IDs never collide with a
                         separately-run --dsr scan). Writes a human-readable
                         legacy_id -> dsr_stable_id crosswalk CSV.

  2. apply-references /
     apply-register     - given a target file and the crosswalk CSV, rewrites
                         legacy-ID tokens found inside [INTERNAL EVIDENCE -
                         ...] markers (apply-references) or inside the
                         trailing "(LEGACY-ID)" parenthetical on a citation
                         register's "Source file:" line (apply-register) to
                         the new DSR stable_id. Always writes to a *copy* -
                         never overwrites the target file in this stage.
                         Academic citation markers ([CE-####]) are untouched;
                         only catalogue-storage reference tokens are in scope.

  3. validate / promote - validate proves the rewrite touched only reference
                         tokens (strips marker spans from both old and new
                         text and asserts what remains is byte-identical).
                         promote is the only stage that ever writes to a
                         "live" file - it backs the current one up to a
                         timestamped, tracked archive path first, then swaps
                         in the updated copy. Dry-run by default; requires
                         --apply.

Engine-generic: nothing here hardcodes a thesis-specific path. Target files,
the crosswalk CSV, and the archive directory are all caller-supplied.
"""
from __future__ import annotations

import csv
import json
import mimetypes
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from catalogues import dsr_catalogue

ROOT_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = ROOT_DIR / "instance"
LEGACY_DB_PATH = INSTANCE_DIR / "catalogue.db"
CATALOGUE_DOCUMENTS_DIR = INSTANCE_DIR / "catalogued_files" / "documents"
CROSSWALK_CSV_PATH = dsr_catalogue.DSR_OUTPUT_DIR / "legacy_dsr_crosswalk.csv"

CROSSWALK_FIELDS = [
    "legacy_catalogue_id", "legacy_file_class", "status", "source_kind",
    "resolved_path", "sha256", "dsr_stable_id", "dsr_catalogue_id",
    "dsr_class_code", "dsr_subtype_code", "dsr_confidence_status", "notes",
]

# Legacy IDs look like <2-5 uppercase letters>-<4-6 digits> (STD-01533,
# API-00001, CODE-00004, ...). A DSR stable_id's own internal segments (e.g.
# the "MOD-0001" inside "DSR-ART-MOD-0001") can also match this token shape in
# isolation - harmless, since the substitution below only ever acts on tokens
# that are keys in the crosswalk dict (legacy IDs), so a spurious match against
# an already-migrated marker is a no-op, not a corruption.
LEGACY_ID_TOKEN_RE = re.compile(r"\b([A-Z]{2,5}-\d{4,6})\b")
INTERNAL_EVIDENCE_MARKER_RE = re.compile(r"\[INTERNAL EVIDENCE\s+—\s+[^\]]+\]")
SOURCE_FILE_TRAILING_ID_RE = re.compile(r"\(([A-Z]{2,5}-\d{4,6})\)\s*$")

ROLLUP_CLASS_CODE = "COD"


def display_path(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT_DIR))
    except ValueError:
        return str(p)


# --------------------------------------------------------------------------
# Stage 1: crosswalk
# --------------------------------------------------------------------------

def open_legacy_db_readonly(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or LEGACY_DB_PATH
    if not path.exists():
        raise SystemExit(f"Legacy catalogue not found at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _build_documents_name_index(documents_dir: Path) -> dict:
    index: dict = {}
    if documents_dir.exists():
        for path in documents_dir.rglob("*"):
            if path.is_file():
                index[path.name] = path
    return index


def _resolve_legacy_file(row: sqlite3.Row, name_index: dict) -> tuple[Path | None, str]:
    """Prefers the original source_path (richer directory context for
    classification); falls back to the renamed copy under
    catalogued_files/documents/ (apply-rename's shutil.copy2 output, byte-
    identical to the original) when the source has moved/been deleted since
    cataloguing."""
    source_path = row["source_path"]
    if source_path and Path(source_path).exists():
        return Path(source_path), "source_path"
    proposed = row["proposed_filename"]
    if proposed and proposed in name_index:
        return name_index[proposed], "renamed_copy"
    return None, "unavailable"


def _insert_dsr_record(dsr_conn: sqlite3.Connection, *, legacy_id: str, path: Path,
                        classification: dict, rules: dict, now: str,
                        is_directory: bool = False) -> sqlite3.Row:
    padding = rules["id_padding"]
    project_code = rules["project_code"]
    class_code, subtype_code = classification["class_code"], classification["subtype_code"]

    source_path = str(path.resolve())
    existing = dsr_conn.execute(
        "SELECT * FROM dsr_catalogue WHERE source_path = ?", (source_path,)
    ).fetchone()
    if existing:
        legacy_ids = json.loads(existing["legacy_ids_json"] or "[]")
        if legacy_id not in legacy_ids:
            legacy_ids.append(legacy_id)
            dsr_conn.execute(
                "UPDATE dsr_catalogue SET legacy_ids_json = ?, updated_at = ? WHERE catalogue_id = ?",
                (json.dumps(legacy_ids), now, existing["catalogue_id"]),
            )
        return existing

    # sha256_file() opens the path as a regular file - never valid for a repo
    # rollup's directory path, so directories skip hashing entirely (they're
    # already forced to Requires Review by _classify_rollup, so an absent
    # sha256 doesn't hide anything from a reviewer).
    sha256 = None if is_directory else dsr_catalogue.sha256_file(path)
    stat = path.stat()
    mime_type = None if is_directory else mimetypes.guess_type(path.name)[0]
    seq = dsr_catalogue.next_dsr_seq(dsr_conn, class_code, subtype_code)
    stable_id = f"{project_code}-{class_code}-{subtype_code}-{seq:0{padding}d}"
    catalogue_id = f"{stable_id}-{classification['version']}"

    dsr_conn.execute(
        """
        INSERT INTO dsr_catalogue (
            catalogue_id, stable_id, version, title, file_name, relative_path,
            source_path, extension, mime_type, class_code, subtype_code,
            dsr_artefact_type, knowledge_contribution, sha256, file_size_bytes,
            classification_rule, classification_evidence, confidence_status,
            legacy_ids_json, notes,
            created_date, modified_date, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            catalogue_id, stable_id, classification["version"], path.stem, path.name,
            path.name, source_path, path.suffix.lower(), mime_type,
            class_code, subtype_code, classification["dsr_artefact_type"],
            classification["knowledge_contribution"], sha256, stat.st_size,
            classification["classification_rule"], classification["classification_evidence"],
            classification["confidence_status"], json.dumps([legacy_id]),
            f"migrated from legacy catalogue_id={legacy_id}",
            datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).date().isoformat(),
            datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).date().isoformat(), now, now,
        ),
    )
    return dsr_conn.execute("SELECT * FROM dsr_catalogue WHERE catalogue_id = ?", (catalogue_id,)).fetchone()


def _classify_rollup(row: sqlite3.Row) -> dict:
    file_count = row["repo_file_count"]
    total_size = row["repo_total_size_bytes"]
    return {
        "class_code": ROLLUP_CLASS_CODE,
        "subtype_code": dsr_catalogue.ART_UNRESOLVED_SUBTYPE,
        "dsr_artefact_type": dsr_catalogue.NOT_APPLICABLE,
        "knowledge_contribution": dsr_catalogue.NOT_ASSIGNED,
        "version": "V0.1",
        "confidence_status": dsr_catalogue.REQUIRES_REVIEW,
        "classification_rule": "repo_rollup:directory_not_single_file",
        "classification_evidence": (
            f"legacy repo rollup ({file_count} files, {total_size} bytes total) - "
            "DSR classifies single files; a human must assign the real subtype "
            "(or split the rollup) before this entry can leave Requires Review"
        ),
    }


def build_crosswalk(legacy_conn: sqlite3.Connection, dsr_conn: sqlite3.Connection,
                     project_config: dict) -> tuple[list[dict], dict]:
    rules = dsr_catalogue.load_dsr_rules(project_config)
    name_index = _build_documents_name_index(CATALOGUE_DOCUMENTS_DIR)
    now = datetime.now(timezone.utc).isoformat()

    rows_out: list[dict] = []
    stats = {"legacy_total": 0, "rollups": 0, "no_file": 0, "migrated": 0, "requires_review": 0}

    for row in legacy_conn.execute("SELECT * FROM catalogue ORDER BY catalogue_id"):
        stats["legacy_total"] += 1
        legacy_id = row["catalogue_id"]

        if row["is_repo_rollup"]:
            stats["rollups"] += 1
            source_path = row["source_path"] or ""
            if not source_path or not Path(source_path).is_dir():
                rows_out.append({
                    "legacy_catalogue_id": legacy_id, "legacy_file_class": row["file_class"],
                    "status": "skipped_rollup_no_directory", "source_kind": "unavailable",
                    "resolved_path": source_path, "sha256": "", "dsr_stable_id": "",
                    "dsr_catalogue_id": "", "dsr_class_code": "", "dsr_subtype_code": "",
                    "dsr_confidence_status": "", "notes": "repo rollup directory no longer exists on disk",
                })
                stats["no_file"] += 1
                continue
            classification = _classify_rollup(row)
            dsr_row = _insert_dsr_record(
                dsr_conn, legacy_id=legacy_id, path=Path(source_path),
                classification=classification, rules=rules, now=now, is_directory=True,
            )
            rows_out.append({
                "legacy_catalogue_id": legacy_id, "legacy_file_class": row["file_class"],
                "status": "migrated", "source_kind": "rollup_directory",
                "resolved_path": source_path, "sha256": dsr_row["sha256"] or "",
                "dsr_stable_id": dsr_row["stable_id"], "dsr_catalogue_id": dsr_row["catalogue_id"],
                "dsr_class_code": dsr_row["class_code"], "dsr_subtype_code": dsr_row["subtype_code"],
                "dsr_confidence_status": dsr_row["confidence_status"],
                "notes": "repo rollup - Requires Review, subtype not auto-assigned",
            })
            stats["migrated"] += 1
            stats["requires_review"] += 1
            continue

        path, source_kind = _resolve_legacy_file(row, name_index)
        if path is None:
            stats["no_file"] += 1
            rows_out.append({
                "legacy_catalogue_id": legacy_id, "legacy_file_class": row["file_class"],
                "status": "skipped_no_file", "source_kind": "unavailable",
                "resolved_path": "", "sha256": "", "dsr_stable_id": "", "dsr_catalogue_id": "",
                "dsr_class_code": "", "dsr_subtype_code": "", "dsr_confidence_status": "",
                "notes": "neither original source_path nor a renamed copy under catalogued_files/documents/ exists",
            })
            continue

        classification = dsr_catalogue.classify_file(path, path.parent, rules)
        dsr_row = _insert_dsr_record(dsr_conn, legacy_id=legacy_id, path=path,
                                      classification=classification, rules=rules, now=now)
        rows_out.append({
            "legacy_catalogue_id": legacy_id, "legacy_file_class": row["file_class"],
            "status": "migrated", "source_kind": source_kind,
            "resolved_path": str(path), "sha256": dsr_row["sha256"] or "",
            "dsr_stable_id": dsr_row["stable_id"], "dsr_catalogue_id": dsr_row["catalogue_id"],
            "dsr_class_code": dsr_row["class_code"], "dsr_subtype_code": dsr_row["subtype_code"],
            "dsr_confidence_status": dsr_row["confidence_status"], "notes": "",
        })
        stats["migrated"] += 1
        if dsr_row["confidence_status"] == dsr_catalogue.REQUIRES_REVIEW:
            stats["requires_review"] += 1

    return rows_out, stats


def write_crosswalk_csv(rows: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CROSSWALK_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def load_crosswalk_map(csv_path: Path) -> dict:
    """legacy_catalogue_id -> dsr_stable_id, migrated rows only."""
    mapping: dict = {}
    with csv_path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["status"] == "migrated" and row["dsr_stable_id"]:
                mapping[row["legacy_catalogue_id"]] = row["dsr_stable_id"]
    return mapping


# --------------------------------------------------------------------------
# Stage 2: reference rewriting (always to a copy - target file is only ever
# read, never written, in this stage)
# --------------------------------------------------------------------------

def rewrite_internal_evidence_markers(text: str, crosswalk: dict) -> tuple[str, dict]:
    replaced: dict = {}
    unmapped: dict = {}

    def _sub_marker(marker_match: re.Match) -> str:
        marker = marker_match.group(0)

        def _sub_token(token_match: re.Match) -> str:
            token = token_match.group(1)
            new_id = crosswalk.get(token)
            if new_id is None:
                unmapped[token] = unmapped.get(token, 0) + 1
                return token
            replaced[token] = replaced.get(token, 0) + 1
            return new_id

        return LEGACY_ID_TOKEN_RE.sub(_sub_token, marker)

    new_text = INTERNAL_EVIDENCE_MARKER_RE.sub(_sub_marker, text)
    return new_text, {"replaced": replaced, "unmapped": unmapped}


def rewrite_register_source_lines(text: str, crosswalk: dict) -> tuple[str, dict]:
    replaced: dict = {}
    unmapped: dict = {}
    out_lines = []
    for line in text.split("\n"):
        m = SOURCE_FILE_TRAILING_ID_RE.search(line)
        if m and line.startswith("instance/catalogued_files/"):
            token = m.group(1)
            new_id = crosswalk.get(token)
            if new_id is None:
                unmapped[token] = unmapped.get(token, 0) + 1
            else:
                replaced[token] = replaced.get(token, 0) + 1
                line = line[:m.start()] + f"({new_id})"
        out_lines.append(line)
    return "\n".join(out_lines), {"replaced": replaced, "unmapped": unmapped}


def apply_references(target_path: Path, out_path: Path, crosswalk: dict) -> dict:
    text = target_path.read_text(encoding="utf-8")
    new_text, report = rewrite_internal_evidence_markers(text, crosswalk)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(new_text, encoding="utf-8")
    return report


def apply_register(target_path: Path, out_path: Path, crosswalk: dict) -> dict:
    text = target_path.read_text(encoding="utf-8")
    new_text, report = rewrite_register_source_lines(text, crosswalk)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(new_text, encoding="utf-8")
    return report


# --------------------------------------------------------------------------
# Stage 3: validate + promote
# --------------------------------------------------------------------------

def _strip_reference_spans(text: str) -> str:
    text = INTERNAL_EVIDENCE_MARKER_RE.sub("[INTERNAL EVIDENCE]", text)
    text = re.sub(
        r"^(instance/catalogued_files/.*)\([A-Z]{2,5}-\d{4,6}\)\s*$",
        r"\1(ID)", text, flags=re.MULTILINE,
    )
    return text


def validate_content_preserved(original_path: Path, updated_path: Path) -> tuple[bool, str]:
    original = _strip_reference_spans(original_path.read_text(encoding="utf-8"))
    updated = _strip_reference_spans(updated_path.read_text(encoding="utf-8"))
    if original == updated:
        return True, "content outside catalogue-reference tokens is unchanged"
    import difflib
    diff = "\n".join(difflib.unified_diff(
        original.splitlines(), updated.splitlines(),
        fromfile=display_path(original_path), tofile=display_path(updated_path), lineterm="",
    ))
    return False, diff


def promote(live_path: Path, new_content_path: Path, archive_dir: Path) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")
    backup_path = archive_dir / f"{live_path.stem}_pre-dsr-migration_{timestamp}{live_path.suffix}"
    if backup_path.exists():
        raise SystemExit(f"Backup path already exists, refusing to overwrite: {backup_path}")
    shutil.copy2(live_path, backup_path)
    shutil.copy2(new_content_path, live_path)
    return backup_path
