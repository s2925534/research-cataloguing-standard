#!/usr/bin/env python3
"""Research file cataloguing engine (Pass 1-3).

Reads instance/project_config.json + instance/.env, walks SOURCE_DATA_ROOTS,
and produces:
  - instance/catalogue.db               (SQLite, primary queryable store)
  - instance/catalogued_files/catalogue_master.jsonl  (append-only audit log)
  - instance/catalogued_files/duplicate_report.csv
  - instance/catalogued_files/rename_plan.csv          (PROPOSAL ONLY)
  - instance/catalogued_files/human_review_queue.csv   (triage view, ranked)

Never renames, moves, copies or deletes a source file. Pass 4 (approved
rename) is a separate, explicit, human-approved step not implemented here.

Project-specific folder names, organisation/system codes and domain-identifier
patterns are never hardcoded here - they come from
instance/project_config.json -> cataloguing_rules, loaded at runtime.

Usage:
    python3 catalogue.py scan          # Pass 1: inventory + hash + repo rollups
    python3 catalogue.py extract       # Pass 2: content preview + heuristic classification
    python3 catalogue.py enrich        # Pass 2.5: embedded metadata + domain identifiers
    python3 catalogue.py duplicates    # group by hash, flag exact duplicates
    python3 catalogue.py near-duplicates # content-similarity match (not just hash/size), flag near_duplicate
    python3 catalogue.py group         # group by base filename (e.g. repeat report exports/downloads)
    python3 catalogue.py context [N]   # AI: fill `summary` with document purpose/producing-system/
                                        # counterparty context (costs 1 API call/record; not part of
                                        # `all` - run explicitly; optional N caps it to a pilot batch)
    python3 catalogue.py classify-evidence [N]
                                        # AI: flag `evidence_source_type` (industry_expert_feedback,
                                        # change_request, survey_results, walkthrough_results, etc. -
                                        # see project_config.json -> evidence_source_types). Most
                                        # records match none, stored as "none" so reruns only cover
                                        # new records. Costs 1 API call/record; not part of `all`.
    python3 catalogue.py rename-plan   # Pass 3: propose filenames, write rename_plan.csv (no renaming)
    python3 catalogue.py review-queue  # write human_review_queue.csv, ranked by why each record
                                        # needs a look (duplicate flags, low confidence, undecided use)
    python3 catalogue.py apply-rename [--skip-duplicates] [--nested] [--group-literature] [--execute]
                                        # Pass 4: copy sources -> instance/catalogued_files/documents/ under
                                        # their proposed_filename (kept out of catalogued_files/ itself so
                                        # research files never mix with the pipeline's own tool/report output
                                        # there - catalog.html, catalogue_master.*, *_report.csv). No per-file
                                        # sidecar either - catalogue_master.jsonl + catalog.html already cover
                                        # per-file lookup. Dry-run by default (prints the plan); nothing is
                                        # written until --execute is passed.
                                        # --skip-duplicates omits files flagged duplicate_status=exact_duplicate.
                                        # --nested mirrors each file's original source subdirectory instead
                                        # of the default flat layout. --group-literature carves LIT records
                                        # out into documents/literature/ regardless of the other layout.
    python3 catalogue.py export-jsonl  # refresh catalogue_master.jsonl from the DB
    python3 catalogue.py validate-schema
                                        # optional: validate every record against instance/schema.generated.json
                                        # (needs `pip install jsonschema` - see requirements.txt; not part of
                                        # `all`), writes schema_validation_report.csv
    python3 catalogue.py verify        # data-integrity regression check (filename/path consistency)
    python3 catalogue.py all [--dry-run] [--limit N]
                                        # scan + extract + enrich + duplicates + near-duplicates + group +
                                        # rename-plan + review-queue + export + verify + stats.
                                        # --dry-run runs the real pipeline against a disposable copy of
                                        # catalogue.db (deleted afterward) - instance/catalogue.db and
                                        # instance/catalogued_files/ are never written to. --limit N trims
                                        # the copy to the first N catalogue_ids (by catalogue_id) right
                                        # after scan, so the rest of the pipeline only processes N records.
    python3 catalogue.py stats         # summary counts
"""
from __future__ import annotations

import csv
import difflib
import hashlib
import html
import json
import mimetypes
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT_DIR / "templates"
INSTANCE_DIR = ROOT_DIR / "instance"
CATALOGUE_DIR = INSTANCE_DIR / "catalogued_files"
# apply-rename's copies live one level down from CATALOGUE_DIR, not mixed in
# with the pipeline's own report/tool files (catalog.html, catalogue_master.*,
# *_report.csv) that also live directly under CATALOGUE_DIR - so a listing of
# either directory is either "the research documents" or "the tooling", never
# both at once.
CATALOGUE_DOCUMENTS_DIR = CATALOGUE_DIR / "documents"
DB_PATH = INSTANCE_DIR / "catalogue.db"
JSONL_PATH = CATALOGUE_DIR / "catalogue_master.jsonl"
DUPLICATE_REPORT_PATH = CATALOGUE_DIR / "duplicate_report.csv"
RENAME_PLAN_PATH = CATALOGUE_DIR / "rename_plan.csv"
UNREADABLE_REPORT_PATH = CATALOGUE_DIR / "unreadable_or_encrypted_report.csv"


def display_path(p: Path) -> str:
    """cmd_all's --dry-run redirects the module-level *_PATH constants to a
    temp dir outside ROOT_DIR, so relative_to(ROOT_DIR) would raise there."""
    try:
        return str(p.relative_to(ROOT_DIR))
    except ValueError:
        return str(p)

# Generic, project-agnostic engine defaults: file-extension heuristics that
# apply to any research project. Anything that names a specific folder,
# organisation, system or research-domain identifier pattern is NOT hardcoded
# here - it comes from instance/project_config.json -> cataloguing_rules,
# loaded at runtime by load_cataloguing_rules().

SKIP_NAMES = {".DS_Store", ".idea", ".git"}

EXTENSION_FILE_CLASS = {
    ".pdf": "STD", ".docx": "ART", ".doc": "ART", ".csv": "DAT", ".xlsx": "DAT",
    ".json": "DAT", ".md": "ART", ".txt": "ART", ".html": "OPS", ".htm": "OPS",
    ".eml": "OPS", ".jpg": "IMG", ".jpeg": "IMG", ".png": "IMG",
    ".java": "CODE", ".yaml": "API", ".yml": "API", ".sh": "CODE", ".mjs": "CODE",
    ".js": "CODE", ".ps1": "CODE", ".sql": "CODE", ".xml": "API", ".rdf": "STD",
    ".bpmn": "ART", ".gitignore": "CODE", ".gitattributes": "CODE",
    ".editorconfig": "CODE", ".maintainers": "ADM", ".gitmodules": "CODE",
    ".bin": "DAT",
}

EXTENSION_ARTEFACT_TYPE = {
    ".csv": "CSV-EXPORT", ".xlsx": "XLSX-EXPORT", ".eml": "EMAIL",
    ".java": "SCRIPT", ".sh": "SCRIPT", ".sql": "SCRIPT", ".mjs": "SCRIPT", ".js": "SCRIPT",
    ".bpmn": "DIAGRAM", ".jpg": "PHOTO", ".jpeg": "PHOTO", ".png": "SCREENSHOT",
}

TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".java", ".sh", ".mjs", ".js",
    ".css", ".xml", ".rdf", ".sql", ".bpmn", ".gitignore", ".gitattributes",
    ".editorconfig", ".gitmodules", ".maintainers", ".ps1",
}

# Mirrors schema_core.json -> properties.primary_entity_type.enum.
PRIMARY_ENTITY_TYPES = {
    "container", "booking", "job", "shipment", "vessel", "actor", "document", "event",
    "dataset", "framework_component", "meeting", "standard", "publication", "multi", "none", "unknown",
}


def load_cataloguing_rules(project_config: dict) -> dict:
    """Project-specific classification rules from instance/project_config.json.

    Keeps every folder name, organisation code, system code and domain
    identifier pattern out of this committed script - see templates/
    project_config.template.json for the (empty) shape a new project starts
    from, and README.md for how to populate it.
    """
    rules = project_config.get("cataloguing_rules", {})
    organisations = [o for o in project_config.get("organisations", []) if o and o != "NA"]
    systems = [s for s in project_config.get("systems", []) if s and s != "NA"]
    return {
        "known_repo_dirs": rules.get("known_repo_dirs", {}),
        "dir_file_class_overrides": [tuple(pair) for pair in rules.get("dir_file_class_overrides", [])],
        "dir_org_system": {k: tuple(v) for k, v in rules.get("dir_org_system", {}).items()},
        "dir_system_only": [tuple(pair) for pair in rules.get("dir_system_only", [])],
        "domain_identifier_patterns": {
            field: re.compile(pattern, re.IGNORECASE)
            for field, pattern in rules.get("domain_identifier_patterns", {}).items()
        },
        # Whole-word, case-insensitive: catches a controlled org/system name
        # mentioned in a filename or extracted content even when the file
        # isn't sitting inside one of the dir_org_system marker directories.
        # Boundaries use a manual lookaround rather than \b because \b treats
        # underscore as a word character, which would miss e.g. "interport"
        # inside "dump-interport_production-...".
        "org_name_patterns": [
            (name, re.compile(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", re.IGNORECASE))
            for name in organisations
        ],
        "system_name_patterns": [
            (name, re.compile(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", re.IGNORECASE))
            for name in systems
        ],
    }


# --------------------------------------------------------------------------
# Config / env loading (same convention as setup.py)
# --------------------------------------------------------------------------

def load_json(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"ERROR: required file missing: {path}. Run setup.py first.")
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def parse_env_file(path: Path) -> dict:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def load_env() -> dict:
    env_path = INSTANCE_DIR / ".env"
    if not env_path.exists():
        raise SystemExit(f"ERROR: {env_path} not found. Run setup.py first.")
    return parse_env_file(env_path)


# --------------------------------------------------------------------------
# SQLite schema
# --------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS catalogue (
    catalogue_id TEXT PRIMARY KEY,
    original_filename TEXT,
    source_path TEXT UNIQUE,
    proposed_filename TEXT,
    extension TEXT,
    mime_type TEXT,
    sha256 TEXT,
    file_size_bytes INTEGER,
    ingest_date TEXT,
    document_date TEXT,
    publication_year INTEGER,
    file_revision TEXT DEFAULT 'v01',
    source_version TEXT,
    file_class TEXT,
    source_organisation TEXT,
    source_system TEXT,
    source_actor_role TEXT,
    artefact_type TEXT,
    title TEXT,
    short_title TEXT,
    authors_json TEXT DEFAULT '[]',
    publisher_or_issuer TEXT,
    doi TEXT,
    source_url TEXT,
    apa7_citation TEXT,
    primary_entity_type TEXT,
    primary_entity_id TEXT,
    secondary_entity_ids_json TEXT DEFAULT '[]',
    domain_identifiers_json TEXT DEFAULT '{}',
    source_fields_json TEXT DEFAULT '[]',
    research_taxonomy_json TEXT DEFAULT '{}',
    document_sections_json TEXT DEFAULT '[]',
    claim_ids_json TEXT DEFAULT '[]',
    evidence_role TEXT,
    evidence_status TEXT,
    verification_basis TEXT,
    content_preview TEXT,
    summary TEXT,
    keywords_json TEXT DEFAULT '[]',
    access_classification TEXT,
    contains_personal_data INTEGER DEFAULT 0,
    contains_commercially_sensitive_data INTEGER DEFAULT 0,
    ethics_status TEXT DEFAULT 'not_assessed',
    redaction_status TEXT DEFAULT 'not_assessed',
    processing_status TEXT,
    parse_method TEXT,
    ocr_used INTEGER DEFAULT 0,
    rename_confidence REAL,
    metadata_confidence REAL,
    human_review_required INTEGER DEFAULT 1,
    review_notes TEXT,
    duplicate_status TEXT DEFAULT 'unresolved',
    duplicate_group_id TEXT,
    canonical_catalogue_id TEXT,
    supersedes_catalogue_id TEXT,
    source_group_id TEXT,
    schema_reference TEXT,
    evidence_source_type TEXT,
    near_duplicate_group_id TEXT,
    near_duplicate_canonical_id TEXT,
    near_duplicate_score REAL,
    use_decision TEXT DEFAULT 'undecided',
    reason_for_use_decision TEXT,
    retention_class TEXT DEFAULT 'review_required',
    project_id TEXT,
    is_repo_rollup INTEGER DEFAULT 0,
    repo_file_count INTEGER,
    repo_total_size_bytes INTEGER,
    repo_extension_breakdown_json TEXT,
    created_at TEXT,
    updated_at TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_catalogue_sha256 ON catalogue(sha256);
CREATE INDEX IF NOT EXISTS idx_catalogue_file_class ON catalogue(file_class);
CREATE INDEX IF NOT EXISTS idx_catalogue_processing_status ON catalogue(processing_status);

CREATE TABLE IF NOT EXISTS counters (
    file_class TEXT PRIMARY KEY,
    next_seq INTEGER NOT NULL DEFAULT 1
);
"""


def get_db() -> sqlite3.Connection:
    """CREATE TABLE IF NOT EXISTS only covers brand-new databases; existing
    ones need columns added explicitly here when the schema grows."""
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(catalogue)")}
    for column, coltype in (
        ("source_group_id", "TEXT"),
        ("schema_reference", "TEXT"),
        ("evidence_source_type", "TEXT"),
        ("near_duplicate_group_id", "TEXT"),
        ("near_duplicate_canonical_id", "TEXT"),
        ("near_duplicate_score", "REAL"),
    ):
        if column not in existing_cols:
            conn.execute(f"ALTER TABLE catalogue ADD COLUMN {column} {coltype}")
            conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_catalogue_source_group_id ON catalogue(source_group_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_catalogue_near_duplicate_group_id ON catalogue(near_duplicate_group_id)")
    conn.commit()
    return conn


def next_catalogue_id(conn: sqlite3.Connection, file_class: str, padding: int) -> str:
    cur = conn.execute("SELECT next_seq FROM counters WHERE file_class = ?", (file_class,))
    row = cur.fetchone()
    seq = row["next_seq"] if row else 1
    conn.execute(
        "INSERT INTO counters (file_class, next_seq) VALUES (?, ?) "
        "ON CONFLICT(file_class) DO UPDATE SET next_seq = ?",
        (file_class, seq + 1, seq + 1),
    )
    return f"{file_class}-{seq:0{padding}d}"


# --------------------------------------------------------------------------
# Pass 1: scan / inventory
# --------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_source_files(source_root: Path, known_repo_dirs: dict):
    for path in source_root.rglob("*"):
        if not path.is_file():
            continue
        # Path.is_file() follows symlinks, so a symlink whose target is a
        # file would otherwise be scanned as its own candidate. This project
        # has a ~600-symlink "review mirror" tree (00_RESEARCH_REVIEW/by_category/)
        # whose entries are named after their target's full relative path with
        # " __ " separators (e.g. "zotero unfiltered files __ My Library __
        # files __ 200 __ some-paper.pdf") - every target is also reachable via
        # its own real, direct path elsewhere in the tree, so nothing is lost
        # by skipping symlinks outright. Scanning them was actively harmful:
        # rglob()'s traversal order isn't guaranteed, so whichever path (the
        # real file or its flattened-name mirror alias) got visited first
        # became the permanent original_filename for that catalogue_id - on
        # one run that meant hundreds of records recording the mirror's
        # flattened alias name instead of the file's real name, caught by
        # `catalogue.py verify`. Skipping symlinks entirely removes the
        # ordering dependency: only the real, direct path can ever be scanned.
        if path.is_symlink():
            continue
        if path.suffix.lower() == ".zip":
            continue
        try:
            rel_parts = path.relative_to(source_root).parts
        except ValueError:
            continue
        if any(part in SKIP_NAMES for part in rel_parts):
            continue
        # skip files inside known repo dirs; those are handled by scan_repos()
        if rel_parts and rel_parts[0] in known_repo_dirs:
            continue
        yield path


def classify_file_class(path: Path, source_root: Path, dir_file_class_overrides: list) -> str:
    rel = str(path.relative_to(source_root))
    for marker, cls in dir_file_class_overrides:
        if marker in rel:
            if path.suffix.lower() == ".java":
                return "CODE"
            return cls
    return EXTENSION_FILE_CLASS.get(path.suffix.lower(), "OPS")


def classify_artefact_type(path: Path) -> str:
    name_lower = path.name.lower()
    if "postman_collection" in name_lower:
        return "POSTMAN-COLLECTION"
    return EXTENSION_ARTEFACT_TYPE.get(path.suffix.lower(), "OTHER")


def guess_access_classification(file_class: str) -> tuple[str, bool]:
    """Returns (access_classification, contains_commercially_sensitive_data)."""
    if file_class in ("LIT", "STD"):
        return "PUB", False
    if file_class in ("OPS", "DAT", "IMG"):
        return "CONF", True
    return "INTERNAL", False


def cmd_scan(project_config: dict, env: dict) -> None:
    conn = get_db()
    padding = project_config.get("catalogue_id_prefix_padding", 5)
    rules = load_cataloguing_rules(project_config)
    ingest_date = datetime.now(timezone.utc).date().isoformat()
    now = datetime.now(timezone.utc).isoformat()

    scanned = 0
    for root_str in env["SOURCE_DATA_ROOTS"].split(","):
        source_root = Path(root_str.strip())
        if not source_root.exists():
            print(f"WARNING: source root does not exist, skipping: {source_root}")
            continue

        for path in iter_source_files(source_root, rules["known_repo_dirs"]):
            source_path = str(path.resolve())
            existing = conn.execute(
                "SELECT catalogue_id FROM catalogue WHERE source_path = ?", (source_path,)
            ).fetchone()
            if existing:
                continue  # already inventoried; re-run 'extract' to refresh content

            file_class = classify_file_class(path, source_root, rules["dir_file_class_overrides"])
            catalogue_id = next_catalogue_id(conn, file_class, padding)
            stat = path.stat()
            sha256 = sha256_file(path)
            mime_type, _ = mimetypes.guess_type(path.name)

            conn.execute(
                """
                INSERT INTO catalogue (
                    catalogue_id, original_filename, source_path, extension, mime_type,
                    sha256, file_size_bytes, ingest_date, file_class, artefact_type,
                    access_classification, contains_commercially_sensitive_data,
                    processing_status, human_review_required, evidence_status,
                    retention_class, use_decision, project_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unprocessed', 1,
                          'raw_unreviewed', 'review_required', 'undecided', ?, ?, ?)
                """,
                (
                    catalogue_id, path.name, source_path, path.suffix.lower(), mime_type,
                    sha256, stat.st_size, ingest_date, file_class, classify_artefact_type(path),
                    *guess_access_classification(file_class),
                    project_config["project_id"], now, now,
                ),
            )
            scanned += 1
            if scanned % 200 == 0:
                conn.commit()
                print(f"  scanned {scanned} files...")

    conn.commit()
    print(f"Pass 1 (scan) complete: {scanned} new files inventoried.")
    scan_repos(conn, project_config, env, padding, ingest_date, now, rules["known_repo_dirs"])
    conn.commit()
    conn.close()


def scan_repos(conn, project_config, env, padding, ingest_date, now, known_repo_dirs: dict) -> None:
    for root_str in env["SOURCE_DATA_ROOTS"].split(","):
        source_root = Path(root_str.strip())
        for repo_name, org in known_repo_dirs.items():
            repo_path = source_root / repo_name
            if not repo_path.is_dir():
                continue
            source_path = str(repo_path.resolve())
            existing = conn.execute(
                "SELECT catalogue_id FROM catalogue WHERE source_path = ?", (source_path,)
            ).fetchone()
            if existing:
                continue

            files = [p for p in repo_path.rglob("*") if p.is_file() and p.name not in SKIP_NAMES]
            ext_counter = Counter(p.suffix.lower().lstrip(".") or "noext" for p in files)
            total_size = sum(p.stat().st_size for p in files)
            hash_pairs = sorted(
                (str(p.relative_to(repo_path)), sha256_file(p)) for p in files
            )
            aggregate_hash = hashlib.sha256(
                json.dumps(hash_pairs, sort_keys=True).encode("utf-8")
            ).hexdigest()

            readme_preview = None
            for candidate in ("README.md", "readme.md", "README.MD"):
                readme_path = repo_path / candidate
                if readme_path.exists():
                    readme_preview = " ".join(
                        readme_path.read_text(encoding="utf-8", errors="ignore").split()
                    )[:1500]
                    break

            catalogue_id = next_catalogue_id(conn, "STD", padding)
            summary = (
                f"Cloned specification/code repository snapshot. {len(files)} files. "
                f"Extension breakdown: {dict(ext_counter.most_common(8))}."
            )
            conn.execute(
                """
                INSERT INTO catalogue (
                    catalogue_id, original_filename, source_path, extension, sha256,
                    file_size_bytes, ingest_date, file_class, source_organisation,
                    artefact_type, title, content_preview, summary,
                    access_classification, contains_commercially_sensitive_data,
                    processing_status, human_review_required, evidence_status,
                    retention_class, use_decision, project_id, is_repo_rollup,
                    repo_file_count, repo_total_size_bytes, repo_extension_breakdown_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'DIR', ?, ?, ?, 'STD', ?, 'SOURCE-CODE-REPOSITORY',
                          ?, ?, ?, 'PUB', 0, 'catalogued', 1, 'raw_unreviewed',
                          'review_required', 'undecided', ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    catalogue_id, repo_name, source_path, aggregate_hash, total_size,
                    ingest_date, org, repo_name, readme_preview, summary,
                    project_config["project_id"], len(files), total_size,
                    json.dumps(dict(ext_counter)), now, now,
                ),
            )
            print(f"  repo rollup: {repo_name} -> {catalogue_id} ({len(files)} files)")


# --------------------------------------------------------------------------
# Pass 2: content extraction
# --------------------------------------------------------------------------

# Mirrors schema_core.json -> properties.content_preview.maxLength: a word
# cap alone doesn't bound character count (dot-leader tables of contents,
# CSV rows, and other punctuation-dense text can pack far more than typical
# prose into the same word count), so enforce a matching hard char ceiling
# here too rather than relying on schema validation to just catch it later.
CONTENT_PREVIEW_MAX_CHARS = 6000


def cap_words(text: str, max_words: int) -> str:
    words = text.split()
    return " ".join(words[:max_words])[:CONTENT_PREVIEW_MAX_CHARS]


def extract_pdf(path: Path) -> tuple[str | None, str, bool]:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout, "pdftotext", False
        return None, "pdftotext_empty", False
    except Exception as exc:
        return None, f"pdftotext_error:{exc}", False


def extract_docx(path: Path) -> tuple[str | None, str, bool]:
    try:
        result = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout, "textutil", False
        return None, "textutil_empty", False
    except Exception as exc:
        return None, f"textutil_error:{exc}", False


def extract_xlsx(path: Path) -> tuple[str | None, str, bool]:
    try:
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                parts.append(" ".join(str(c) for c in row if c is not None))
                if len(parts) > 500:
                    break
        return "\n".join(parts), "openpyxl", False
    except Exception as exc:
        return None, f"openpyxl_error:{exc}", False


def extract_eml(path: Path) -> tuple[str | None, str, bool]:
    try:
        from email import policy
        from email.parser import BytesParser

        with path.open("rb") as fh:
            msg = BytesParser(policy=policy.default).parse(fh)
        header = f"Subject: {msg.get('subject', '')}\nFrom: {msg.get('from', '')}\nDate: {msg.get('date', '')}\n\n"
        body = ""
        if msg.get_body(preferencelist=("plain",)):
            body = msg.get_body(preferencelist=("plain",)).get_content()
        return header + body, "email_stdlib", False
    except Exception as exc:
        return None, f"email_error:{exc}", False


def extract_image_ocr(path: Path) -> tuple[str | None, str, bool]:
    try:
        result = subprocess.run(
            ["tesseract", str(path), "stdout"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout, "tesseract_ocr", True
        return None, "tesseract_empty", True
    except Exception as exc:
        return None, f"tesseract_error:{exc}", True


def extract_html(path: Path) -> tuple[str | None, str, bool]:
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        text = re.sub(r"<script.*?</script>|<style.*?</style>", "", raw, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        return text, "html_strip_tags", False
    except Exception as exc:
        return None, f"html_error:{exc}", False


def extract_plain(path: Path) -> tuple[str | None, str, bool]:
    """UTF-16 files (common from Windows tooling - DB dump exports, Teams/
    Zoom transcripts) decode as UTF-8 without raising an error, since a NUL
    byte is itself valid UTF-8 (U+0000) - it just silently interleaves a NUL
    after every ASCII character instead of failing loudly. Detect via BOM
    first, then fall back to a NUL-density heuristic for BOM-less UTF-16."""
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
            return raw.decode("utf-16", errors="ignore"), "plain_read_utf16", False
        if raw.startswith(b"\xef\xbb\xbf"):
            return raw.decode("utf-8-sig", errors="ignore"), "plain_read", False
        text = raw.decode("utf-8", errors="ignore")
        if text and text.count("\x00") / len(text) > 0.2:
            retried = raw.decode("utf-16", errors="ignore")
            if retried.strip():
                return retried, "plain_read_utf16_no_bom", False
        return text, "plain_read", False
    except Exception as exc:
        return None, f"plain_read_error:{exc}", False


def extract_content(path: Path) -> tuple[str | None, str, bool]:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return extract_pdf(path)
    if ext in (".docx", ".doc"):
        return extract_docx(path)
    if ext == ".xlsx":
        return extract_xlsx(path)
    if ext == ".eml":
        return extract_eml(path)
    if ext in (".jpg", ".jpeg", ".png"):
        return extract_image_ocr(path)
    if ext in (".html", ".htm"):
        return extract_html(path)
    if ext in TEXT_EXTENSIONS:
        return extract_plain(path)
    return None, "no_extractor", False


def cmd_extract(project_config: dict) -> None:
    conn = get_db()
    max_words = project_config.get("preview_max_words", 500)
    now = datetime.now(timezone.utc).isoformat()

    rows = conn.execute(
        "SELECT catalogue_id, source_path, file_size_bytes, extension "
        "FROM catalogue WHERE processing_status = 'unprocessed' AND is_repo_rollup = 0"
    ).fetchall()

    processed = 0
    for row in rows:
        path = Path(row["source_path"])
        stat = path.stat() if path.exists() else None
        document_date = None
        review_note = None
        if stat:
            document_date = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).date().isoformat()
            review_note = "document_date is filesystem modified time, not an extracted/embedded date; verify against content."

        text, parse_method, ocr_used = extract_content(path)
        if text is None:
            status = "unreadable" if "error" in parse_method or parse_method == "no_extractor" else "partially_parsed"
            conn.execute(
                "UPDATE catalogue SET processing_status = ?, parse_method = ?, ocr_used = ?, "
                "document_date = ?, review_notes = ?, updated_at = ? WHERE catalogue_id = ?",
                (status, parse_method, int(ocr_used), document_date, review_note, now, row["catalogue_id"]),
            )
        else:
            preview = cap_words(re.sub(r"\s+", " ", text).strip(), max_words)
            conn.execute(
                "UPDATE catalogue SET processing_status = 'parsed', parse_method = ?, ocr_used = ?, "
                "content_preview = ?, document_date = ?, review_notes = ?, updated_at = ? "
                "WHERE catalogue_id = ?",
                (parse_method, int(ocr_used), preview, document_date, review_note, now, row["catalogue_id"]),
            )
        processed += 1
        if processed % 100 == 0:
            conn.commit()
            print(f"  extracted {processed}/{len(rows)}...")

    conn.commit()

    unreadable = conn.execute(
        "SELECT catalogue_id, original_filename, source_path, parse_method FROM catalogue "
        "WHERE processing_status IN ('unreadable', 'partially_parsed')"
    ).fetchall()
    with UNREADABLE_REPORT_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["catalogue_id", "original_filename", "source_path", "parse_method"])
        for r in unreadable:
            writer.writerow([r["catalogue_id"], r["original_filename"], r["source_path"], r["parse_method"]])

    print(f"Pass 2 (extract) complete: {processed} files processed. "
          f"{len(unreadable)} unreadable/partial -> {UNREADABLE_REPORT_PATH.name}")
    conn.close()


# --------------------------------------------------------------------------
# Pass 2.5: metadata enrichment (embedded document metadata, not guesses)
# --------------------------------------------------------------------------


def parse_pdf_date(raw: str | None) -> str | None:
    if not raw or not raw.startswith("D:"):
        return None
    digits = raw[2:10]
    try:
        return datetime.strptime(digits, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


try:
    import fitz
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False


def extract_pdf_metadata(path: Path) -> dict:
    """Without pymupdf (see requirements.txt) this always returns {}, which
    silently pushes every PDF record through the paid AI slug fallback in
    rename-plan instead of the free embedded-title path - cmd_enrich() warns
    once up front if PYMUPDF_AVAILABLE is False so that isn't invisible."""
    if not PYMUPDF_AVAILABLE:
        return {}
    try:
        doc = fitz.open(path)
        meta = dict(doc.metadata or {})
        doc.close()
        return meta
    except Exception:
        return {}


def extract_docx_metadata(path: Path) -> dict:
    import zipfile
    from xml.etree import ElementTree as ET

    try:
        with zipfile.ZipFile(path) as z:
            with z.open("docProps/core.xml") as f:
                root = ET.parse(f).getroot()
    except Exception:
        return {}

    ns = {
        "dc": "http://purl.org/dc/elements/1.1/",
        "dcterms": "http://purl.org/dc/terms/",
    }

    def find(prefix: str, tag: str) -> str | None:
        el = root.find(f"{{{ns[prefix]}}}{tag}")
        return el.text if el is not None else None

    return {"title": find("dc", "title"), "creator": find("dc", "creator"), "created": find("dcterms", "created")}


def find_domain_identifiers(patterns: dict, *texts: str | None) -> dict[str, list[str]]:
    """Runs each configured (field_name -> compiled regex) over the given texts."""
    found: dict[str, list[str]] = {}
    for field_name, pattern in patterns.items():
        matches: list[str] = []
        for text in texts:
            if not text:
                continue
            for match in pattern.findall(text.upper()):
                if match not in matches:
                    matches.append(match)
        if matches:
            found[field_name] = matches[:10]
    return found


def infer_org_system(source_root: Path, source_path: str, dir_org_system: dict, dir_system_only: list) -> tuple[str | None, str | None]:
    try:
        rel = str(Path(source_path).relative_to(source_root))
    except ValueError:
        rel = source_path
    for marker, (org, system) in dir_org_system.items():
        if marker in rel:
            return org, (system if system != "NA" else None)
    for marker, system in dir_system_only:
        if marker in rel:
            return None, system
    return None, None


def infer_org_system_from_text(text: str, org_name_patterns: list, system_name_patterns: list) -> tuple[str | None, str | None]:
    """Fallback for when infer_org_system() finds no directory marker: matches
    the project's controlled organisation/system names as whole words in the
    filename or extracted content (e.g. 'Interport' mentioned in a loose file
    at the source root, not inside an 'interport env' folder)."""
    if not text:
        return None, None
    org = next((name for name, pattern in org_name_patterns if pattern.search(text)), None)
    system = next((name for name, pattern in system_name_patterns if pattern.search(text)), None)
    return org, system


def cmd_enrich(project_config: dict, env: dict) -> None:
    if not PYMUPDF_AVAILABLE:
        print(
            "WARNING: pymupdf not installed - PDF title/author/creation-date metadata will not be "
            "extracted, so every PDF record will fall through to the paid AI slug fallback in "
            "rename-plan instead of the free embedded-title path. Run: pip install -r requirements.txt"
        )
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    rules = load_cataloguing_rules(project_config)
    source_roots = [Path(r.strip()) for r in env["SOURCE_DATA_ROOTS"].split(",") if r.strip()]

    rows = conn.execute(
        "SELECT catalogue_id, source_path, original_filename, extension, content_preview, "
        "document_date, review_notes, source_organisation, source_system "
        "FROM catalogue WHERE is_repo_rollup = 0"
    ).fetchall()

    enriched = 0
    for row in rows:
        path = Path(row["source_path"])
        ext = (row["extension"] or "").lower()
        title = None
        authors_json = None
        document_date = row["document_date"]
        review_notes = row["review_notes"]
        metadata_confidence = None

        if ext == ".pdf" and path.exists():
            meta = extract_pdf_metadata(path)
            title = html.unescape((meta.get("title") or "").strip()) or None
            author = html.unescape((meta.get("author") or "").strip()) or None
            if author:
                authors_json = json.dumps([author])
            embedded_date = parse_pdf_date(meta.get("creationDate"))
            if embedded_date:
                document_date = embedded_date
                review_notes = None
                metadata_confidence = 0.7
        elif ext == ".docx" and path.exists():
            meta = extract_docx_metadata(path)
            title = html.unescape((meta.get("title") or "").strip()) or None
            creator = html.unescape((meta.get("creator") or "").strip()) or None
            if creator:
                authors_json = json.dumps([creator])
            created = meta.get("created")
            if created:
                document_date = created[:10]
                review_notes = None
                metadata_confidence = 0.7

        identifiers = find_domain_identifiers(
            rules["domain_identifier_patterns"], row["original_filename"], row["content_preview"]
        )
        domain_identifiers_json = json.dumps(identifiers) if identifiers else None
        all_ids = [v for values in identifiers.values() for v in values]
        primary_entity_type = None
        primary_entity_id = None
        if len(all_ids) > 1:
            primary_entity_type, primary_entity_id = "multi", "MULTI"
        elif len(all_ids) == 1:
            (field_name, values), = identifiers.items()
            guessed_type = re.sub(r"_(numbers|ids)$", "", field_name)
            # schema_core.json's primary_entity_type enum is fixed; fall back to
            # "document" if a project's identifier field name doesn't map onto it.
            primary_entity_type = guessed_type if guessed_type in PRIMARY_ENTITY_TYPES else "document"
            primary_entity_id = values[0]

        source_organisation, source_system = row["source_organisation"], row["source_system"]
        if not source_organisation and not source_system:
            for source_root in source_roots:
                org, system = infer_org_system(
                    source_root, row["source_path"], rules["dir_org_system"], rules["dir_system_only"]
                )
                if org or system:
                    source_organisation, source_system = org, system
                    break

        if not source_organisation or not source_system:
            text_org, text_system = infer_org_system_from_text(
                f"{row['original_filename']} {row['content_preview'] or ''}",
                rules["org_name_patterns"], rules["system_name_patterns"],
            )
            source_organisation = source_organisation or text_org
            source_system = source_system or text_system

        conn.execute(
            """
            UPDATE catalogue SET
                title = COALESCE(?, title),
                authors_json = COALESCE(?, authors_json),
                document_date = COALESCE(?, document_date),
                review_notes = ?,
                domain_identifiers_json = COALESCE(?, domain_identifiers_json),
                primary_entity_type = COALESCE(?, primary_entity_type),
                primary_entity_id = COALESCE(?, primary_entity_id),
                source_organisation = COALESCE(?, source_organisation),
                source_system = COALESCE(?, source_system),
                metadata_confidence = COALESCE(?, metadata_confidence),
                updated_at = ?
            WHERE catalogue_id = ?
            """,
            (
                title, authors_json, document_date, review_notes, domain_identifiers_json,
                primary_entity_type, primary_entity_id, source_organisation, source_system,
                metadata_confidence, now, row["catalogue_id"],
            ),
        )
        enriched += 1
        if enriched % 200 == 0:
            conn.commit()
            print(f"  enriched {enriched}/{len(rows)}...")

    conn.commit()
    conn.close()
    print(f"Pass 2.5 (enrich) complete: {enriched} records checked for embedded metadata, "
          "container numbers, and directory-based organisation/system.")


# --------------------------------------------------------------------------
# Pass 2.6: source grouping (repeat report exports/downloads, by filename)
# --------------------------------------------------------------------------

def cmd_group_files() -> None:
    """Groups files that share a base filename once a trailing repeat-download
    suffix like ' (2)' is stripped - e.g. 'job-management (11).csv' and
    'job-management (20).csv' are the same report exported at different times.
    This is independent of duplicate_status: members of a source_group can
    have completely different content (later exports), so it is never used
    to resolve or exclude duplicates, only to show lineage."""
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()

    rows = conn.execute(
        "SELECT catalogue_id, original_filename FROM catalogue WHERE is_repo_rollup = 0"
    ).fetchall()

    groups: dict[str, list[str]] = {}
    for row in rows:
        stem = Path(row["original_filename"]).stem
        m = REPEAT_SUFFIX_RE.match(stem)
        base = m.group(1).strip() if m else stem
        slug = clean_filename_slug(base)
        if not slug:
            continue  # too short/generic (e.g. 'i', 'sys', a bare numeric id) to be a meaningful group key
        groups.setdefault(slug, []).append(row["catalogue_id"])

    conn.execute("UPDATE catalogue SET source_group_id = NULL WHERE is_repo_rollup = 0")
    assigned = 0
    for slug, members in groups.items():
        if len(members) < 2:
            continue
        conn.executemany(
            "UPDATE catalogue SET source_group_id = ?, updated_at = ? WHERE catalogue_id = ?",
            [(slug, now, catalogue_id) for catalogue_id in members],
        )
        assigned += len(members)

    conn.commit()
    conn.close()
    group_count = sum(1 for members in groups.values() if len(members) >= 2)
    print(f"Pass 2.6 (group) complete: {group_count} source groups, {assigned} member records.")


# --------------------------------------------------------------------------
# Duplicate detection
# --------------------------------------------------------------------------

def cmd_duplicates() -> None:
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()

    groups = conn.execute(
        "SELECT sha256, COUNT(*) as cnt FROM catalogue "
        "WHERE sha256 IS NOT NULL GROUP BY sha256 HAVING cnt > 1"
    ).fetchall()

    rows_written = 0
    with DUPLICATE_REPORT_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["duplicate_group_id", "sha256", "catalogue_id", "original_filename",
                         "source_path", "role", "is_repo_rollup"])

        for group in groups:
            members = conn.execute(
                "SELECT catalogue_id, original_filename, source_path, is_repo_rollup, ingest_date "
                "FROM catalogue WHERE sha256 = ? ORDER BY source_path", (group["sha256"],)
            ).fetchall()
            group_id = group["sha256"][:12]
            canonical = members[0]

            conn.execute(
                "UPDATE catalogue SET duplicate_status = 'unique', duplicate_group_id = ?, "
                "canonical_catalogue_id = ?, updated_at = ? WHERE catalogue_id = ?",
                (group_id, canonical["catalogue_id"], now, canonical["catalogue_id"]),
            )
            writer.writerow([group_id, group["sha256"], canonical["catalogue_id"],
                             canonical["original_filename"], canonical["source_path"], "canonical",
                             canonical["is_repo_rollup"]])
            rows_written += 1

            for dup in members[1:]:
                conn.execute(
                    "UPDATE catalogue SET duplicate_status = 'exact_duplicate', duplicate_group_id = ?, "
                    "canonical_catalogue_id = ?, use_decision = 'exclude', "
                    "reason_for_use_decision = ?, updated_at = ? WHERE catalogue_id = ?",
                    (group_id, canonical["catalogue_id"],
                     f"Exact duplicate (by hash) of {canonical['catalogue_id']}; flagged for deletion pending human approval.",
                     now, dup["catalogue_id"]),
                )
                writer.writerow([group_id, group["sha256"], dup["catalogue_id"],
                                 dup["original_filename"], dup["source_path"], "duplicate",
                                 dup["is_repo_rollup"]])
                rows_written += 1

    conn.commit()
    conn.close()
    print(f"Duplicate detection complete: {len(groups)} duplicate groups, "
          f"{rows_written} rows -> {DUPLICATE_REPORT_PATH.name}")


NEAR_DUPLICATE_SIMILARITY_THRESHOLD = 0.92
NEAR_DUPLICATE_MAX_SIZE_RATIO = 3.0  # skip pairs whose sizes differ by more than this before comparing content


def cmd_near_duplicates() -> None:
    """Exact SHA-256 matching (cmd_duplicates) only catches byte-identical
    files. This catches near-duplicates it can't: two files with almost the
    same content but not identical (re-exported, retyped, minor edits).
    Compares content_preview, which is already the full extracted text for
    short files and just the first preview_max_words for long ones - the
    same field naturally gives "full content" vs "initial content" behavior
    without a separate extraction path. Uses difflib's similarity ratio with
    a threshold below 1.0 as the margin for discrepancy, so near-identical
    (not just byte-identical) content still gets flagged.

    Fully additive to duplicate_status/duplicate_group_id/canonical_catalogue_id
    (the exact-hash fields) - uses its own near_duplicate_* columns so a
    record that's simultaneously the canonical of a hash group and a member
    of a near-duplicate group doesn't have one relationship overwrite the
    other. Only the non-canonical members of a near-duplicate group get
    duplicate_status='near_duplicate'; already-exact_duplicate records are
    excluded from consideration (that's already resolved, more precisely)."""
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()

    # Idempotent: clear prior results first, so a record that no longer
    # matches under updated logic doesn't keep a stale group assignment.
    conn.execute(
        "UPDATE catalogue SET near_duplicate_group_id = NULL, near_duplicate_canonical_id = NULL, "
        "near_duplicate_score = NULL WHERE near_duplicate_group_id IS NOT NULL"
    )
    conn.execute(
        "UPDATE catalogue SET duplicate_status = 'unresolved' WHERE duplicate_status = 'near_duplicate'"
    )
    conn.commit()

    rows = conn.execute(
        "SELECT catalogue_id, file_class, extension, file_size_bytes, content_preview, primary_entity_id "
        "FROM catalogue WHERE is_repo_rollup = 0 AND content_preview IS NOT NULL AND content_preview != '' "
        "AND duplicate_status != 'exact_duplicate'"
    ).fetchall()

    buckets: dict[tuple, list] = {}
    for row in rows:
        buckets.setdefault((row["file_class"], row["extension"]), []).append(row)

    # Bounded prefix, not the full (up to ~500-word) preview: two genuinely
    # near-duplicate files agree from the start (same title/header row/opening
    # text); merely same-topic documents diverge quickly. Keeps this "initial
    # content" comparison fast even for the largest bucket (LIT/.pdf, 453
    # records - ~102k pairs) without weakening the actual detection.
    compare_len = 600
    texts = {r["catalogue_id"]: (r["content_preview"] or "").lower()[:compare_len] for r in rows}

    groups_found, flagged = 0, 0
    for (file_class, ext), bucket_rows in buckets.items():
        n = len(bucket_rows)
        pairs = n * (n - 1) // 2
        print(f"  near-dup bucket {file_class}/{ext}: {n} records, {pairs} pairs to screen...")
        matched: set[str] = set()
        checked = 0
        for i in range(n):
            a = bucket_rows[i]
            if a["catalogue_id"] in matched:
                continue
            a_text = texts[a["catalogue_id"]]
            size_a = a["file_size_bytes"] or 0
            members = [(a, 1.0)]
            sm = difflib.SequenceMatcher(None, autojunk=False)
            sm.set_seq2(a_text)  # fixed side: difflib caches per-char position data for this one
            for j in range(i + 1, n):
                b = bucket_rows[j]
                checked += 1
                if checked % 20000 == 0:
                    print(f"    ...{checked}/{pairs} pairs screened")
                if b["catalogue_id"] in matched:
                    continue
                # A different confirmed domain entity (container/booking/job
                # id) means these are two different real-world things, no
                # matter how similar the template/boilerplate text is - e.g.
                # two different containers' pack-label PDFs, or two different
                # COBIT framework versions' seed scripts.
                if a["primary_entity_id"] and b["primary_entity_id"] and a["primary_entity_id"] != b["primary_entity_id"]:
                    continue
                size_b = b["file_size_bytes"] or 0
                if size_a and size_b and max(size_a, size_b) / max(min(size_a, size_b), 1) > NEAR_DUPLICATE_MAX_SIZE_RATIO:
                    continue
                sm.set_seq1(texts[b["catalogue_id"]])
                if sm.quick_ratio() < NEAR_DUPLICATE_SIMILARITY_THRESHOLD:
                    continue  # quick_ratio() is an upper bound on ratio(), safe to prune on
                score = sm.ratio()
                if score >= NEAR_DUPLICATE_SIMILARITY_THRESHOLD:
                    members.append((b, score))

            if len(members) > 1:
                groups_found += 1
                canonical = members[0][0]
                group_id = f"nd-{canonical['catalogue_id']}"
                for member, score in members:
                    matched.add(member["catalogue_id"])
                    conn.execute(
                        "UPDATE catalogue SET near_duplicate_group_id = ?, near_duplicate_canonical_id = ?, "
                        "near_duplicate_score = ?, updated_at = ? WHERE catalogue_id = ?",
                        (group_id, canonical["catalogue_id"], score, now, member["catalogue_id"]),
                    )
                    if member["catalogue_id"] != canonical["catalogue_id"]:
                        conn.execute(
                            "UPDATE catalogue SET duplicate_status = 'near_duplicate', updated_at = ? "
                            "WHERE catalogue_id = ? AND duplicate_status IN ('unresolved', 'unique')",
                            (now, member["catalogue_id"]),
                        )
                        flagged += 1

        conn.commit()  # per-bucket, so a kill mid-run doesn't lose finished buckets' work

    conn.commit()
    conn.close()
    print(f"Near-duplicate detection complete: {groups_found} groups, {flagged} records flagged "
          f"near_duplicate (threshold={NEAR_DUPLICATE_SIMILARITY_THRESHOLD}).")


# --------------------------------------------------------------------------
# Pass 3: rename proposal (no files touched)
# --------------------------------------------------------------------------

def safe_field(value: str | None, default: str = "UNKNOWN") -> str:
    if not value:
        return default
    value = re.sub(r"[^A-Za-z0-9\-]+", "-", value.strip())
    return value.strip("-") or default


def to_au_date_token(document_date: str | None) -> str:
    """document_date is stored ISO (YYYY-MM-DD/YYYY-MM/YYYY) for sorting and
    interop; the filename itself uses Australian day-month-year ordering."""
    if not document_date:
        return "UNDATED"
    parts = document_date[:10].split("-")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        year, month, day = parts
        return f"{day}_{month}_{year}"
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        year, month = parts
        return f"{month}_{year}"
    if len(parts) == 1 and parts[0].isdigit():
        return parts[0]
    return "UNDATED"


# Tokens that carry no human meaning and should be stripped from an original
# filename before it's considered as a naming source: UUIDs, long hex hashes,
# long pure-digit runs (epoch timestamps), and generic camera/export noise.
UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
LONG_DIGIT_RE = re.compile(r"\b\d{9,}\b")
GENERIC_NOISE_WORDS = {"img", "image", "copy", "final", "zip", "export", "untitled", "scan"}

# Embedded PDF/DOCX title metadata that's a default placeholder, not a real
# title - e.g. "Document" (common Adobe/Word export default). Treated as if
# no title were present at all, rather than used verbatim as the slug source.
GENERIC_TITLE_VALUES = {"document", "untitled", "untitled document", "new document", "presentation1"}

# Matches the " (2)", " (3)" etc. suffix a browser/OS appends when a file of
# the same name is downloaded/saved again - the signature of a repeat export.
REPEAT_SUFFIX_RE = re.compile(r"^(.*?)\s*\((\d+)\)$")


def insert_word_boundaries(text: str) -> str:
    """Splits concatenated camelCase/PascalCase runs and letter<->digit runs
    into separate words, e.g. "FreightTrackerDocumentations" -> "Freight
    Tracker Documentations" or "jobManagementReportWorkSchedule29Aug2025" ->
    "job Management Report Work Schedule 29 Aug 2025" - rather than one
    unreadable/undeduplicatable blob. Shared by slugify() and origin_segment()
    so directory-name tokens get the same word-level treatment as slugs do."""
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    text = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", text)
    text = re.sub(r"(?<=[0-9])(?=[A-Za-z])", " ", text)
    return text


def dedupe_across_fields(fields: list[list[str]], max_run: int = 4) -> list[list[str]]:
    """Given ordered word-lists (one per structural field of a name's
    descriptive tail, in the order they'll appear), drops any word - or
    contiguous run of words whose fused, no-separator concatenation matches
    something already seen - from a later field that already appeared in an
    earlier one, keeping the first occurrence. Regex-only, no AI cost.

    The fused-run matching is needed because org/system are atomic
    controlled-vocabulary values (e.g. "FREIGHTTRACKER", never split), while
    origin_segment()'s directory names go through the same word-boundary
    splitter as slugs do (so "FreightTrackerDocumentations" becomes
    "FREIGHT", "TRACKER", "DOCUMENTATIONS") - plain word-for-word comparison
    would never see that "FREIGHTTRACKER" and "FREIGHT"+"TRACKER" are the
    same name.

    Never dedupes words against others *within* the same field, since that
    can be a legitimate repeated term (e.g. "RO_RO" for roll-on/roll-off
    shipping, or "DATA" appearing in both "BIG_DATA" and "DATA_SCIENCE")."""
    seen: set[str] = set()

    def add_runs_to_seen(words: list[str]) -> None:
        for i in range(len(words)):
            joined = ""
            for j in range(i, min(i + max_run, len(words))):
                joined += words[j].upper()
                seen.add(joined)

    result = []
    for words in fields:
        kept = []
        i, n = 0, len(words)
        while i < n:
            joined = ""
            matched_end = None
            for j in range(i, min(i + max_run, n)):
                joined += words[j].upper()
                if joined in seen:
                    matched_end = j  # keep extending greedily; last hit wins (longest match)
            if matched_end is not None:
                i = matched_end + 1
            else:
                if words[i]:
                    kept.append(words[i])
                i += 1
        result.append(kept)
        add_runs_to_seen(words)
    return result


def slugify(text: str, max_words: int = 10, max_len: int = 70) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = insert_word_boundaries(text)
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    words = [w for w in text.split("-") if w][:max_words]
    slug = "-".join(words)
    return slug[:max_len].rstrip("-")


def clean_filename_slug(original_filename: str) -> str | None:
    """Strip noise tokens from an original filename; return None if what's
    left isn't meaningful enough to use as a naming source (falls through to
    AI/catalogue_id instead)."""
    stem = Path(original_filename).stem
    stem = UUID_RE.sub(" ", stem)
    stem = LONG_HEX_RE.sub(" ", stem)
    stem = LONG_DIGIT_RE.sub(" ", stem)
    slug = slugify(stem)
    words = [w for w in slug.split("-") if w and w not in GENERIC_NOISE_WORDS]
    if len(words) < 2 or sum(len(w) for w in words) < 6:
        return None
    return "-".join(words)[:70]


def ai_suggest_slug(api_key: str, original_filename: str, file_class: str, artefact_type: str,
                     content_preview: str | None, deterministic_hint: str | None = None) -> str | None:
    """Primary path for slug generation (when an api_key is configured and
    there's no trustworthy embedded title): asks an LLM to understand the
    *original* filename, not just mechanically split it - correcting typos,
    ignoring random system-generated ID prefixes, and recovering words a
    pure regex split gets wrong (e.g. an injected fragment breaking "booking"
    into "b" + "ooking"). deterministic_hint is the regex-only slug attempt,
    passed along for the AI to use or discard, not as ground truth. Returns
    None on any failure (missing key, network error, bad response) so the
    caller falls back to the deterministic hint or catalogue_id - this is
    never required for the pipeline to run."""
    context = (content_preview or "")[:1500]
    hint_line = (
        f"A purely mechanical split of the filename produced: '{deterministic_hint}' - this may be "
        "wrong, or preserve noise/typos/random ID prefixes from the original name. Use it as a "
        "hint only, not ground truth.\n"
        if deterministic_hint else ""
    )
    prompt = (
        f"Original filename: {original_filename}\n"
        f"File category: {file_class} / {artefact_type}\n"
        f"{hint_line}"
        f"Content preview: {context}\n\n"
        "Suggest a short (3-6 word) descriptive filename slug for this research file, based on "
        "what the file actually is. Recover the original filename's intended words even if it has "
        "typos, missing characters, or a mid-word insertion; ignore random system-generated ID "
        "prefixes that carry no meaning. Do not use generic filler words like 'file' or 'files' "
        "unless they're actually part of what the document is (e.g. it's a manifest/inventory that "
        "lists multiple files, or 'file' is part of an established document-type name like 'booking "
        "file' or 'freight file specification') - do not add them just because the word appeared "
        "somewhere in the original filename. Do not repeat the same word or abbreviation twice in a "
        "row (e.g. 'imds-imds' or 'imo-imo') - say it once, unless it's a genuine established "
        "two-word term where repetition is the term itself (e.g. 'ro-ro' for roll-on/roll-off "
        "shipping, or 'data-data' would be wrong but 'big-data-data-science' is two real, different "
        "terms that happen to share the word 'data'). Do not invent facts not supported by the "
        "filename/content. Reply with lowercase words separated by hyphens, no file extension, no punctuation besides "
        "hyphens, and nothing else."
    )
    payload = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 20,
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20, context=_ssl_context()) as response:
            result = json.loads(response.read())
        text = result["choices"][0]["message"]["content"].strip()
        return slugify(text, max_words=6, max_len=50) or None
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, TimeoutError):
        return None


def ai_suggest_context(api_key: str, original_filename: str, file_class: str, artefact_type: str,
                        source_organisation: str | None, source_system: str | None,
                        content_preview: str | None) -> str | None:
    """Optional: ask an LLM for a one-sentence, human-readable description of
    what a file is, which system likely produced it, and which client/
    counterparty/business it concerns - the kind of context a filename or
    controlled-vocab field alone can't carry (e.g. a freight quote generated
    by a named platform for a named client). Free text, not constrained to
    project_config.json's organisation/system list, and never written into
    proposed_filename. Returns None on any failure so the pipeline never
    depends on it."""
    context = (content_preview or "")[:2000]
    known = ", ".join(v for v in (source_organisation, source_system) if v) or "not determined"
    prompt = (
        f"Original filename: {original_filename}\n"
        f"File category: {file_class} / {artefact_type}\n"
        f"Already-known organisation/system (from a controlled vocabulary, may be incomplete): {known}\n"
        f"Content preview: {context}\n\n"
        "In one plain-English sentence (under 240 characters), describe: what kind of document "
        "this is (e.g. quote, gate pass, job report, dataset export), which system or platform "
        "most likely produced it (name it even if it isn't in the known list above; say 'unclear' "
        "if you can't tell), and which client, counterparty or business entity it concerns or was "
        "issued to, if identifiable. Only state what the filename/content actually supports - do "
        "not invent names or figures. Reply with only the sentence, no markdown, no preamble."
    )
    payload = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 100,
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20, context=_ssl_context()) as response:
            result = json.loads(response.read())
        text = result["choices"][0]["message"]["content"].strip()
        return text[:300] or None
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, TimeoutError):
        return None


def ai_classify_evidence_source_type(api_key: str, categories: list[str], original_filename: str,
                                      file_class: str, artefact_type: str, content_preview: str | None) -> str | None:
    """Optional: classify a record into one of project_config.json's
    evidence_source_types (e.g. industry_expert_feedback, change_request,
    survey_results, walkthrough_results) when it genuinely is one of those -
    most records are ordinary operational documents/exports and belong to
    none of them, which is the expected, common answer (returned as the
    literal string "none"). Returns Python None only when the API call
    itself fails, so the pipeline never depends on it and callers can tell
    "checked, matched nothing" apart from "not checked yet"."""
    if not categories:
        return None
    context = (content_preview or "")[:2000]
    category_list = ", ".join(categories)
    prompt = (
        f"Original filename: {original_filename}\n"
        f"File category: {file_class} / {artefact_type}\n"
        f"Content preview: {context}\n\n"
        f"Categories: {category_list}, none\n\n"
        "Does this file genuinely belong to one of the categories above? Most files are ordinary "
        "operational documents/exports and belong to none of them - only answer with a category "
        "name if the content clearly and specifically matches it (e.g. it's literally feedback "
        "from a named industry expert/practitioner, a formal request to change something, results "
        "from a survey/questionnaire, or results from a walkthrough/demo session). Do not guess or "
        "force a fit. Reply with exactly one of: {category_list}, none - nothing else."
    )
    payload = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 12,
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20, context=_ssl_context()) as response:
            result = json.loads(response.read())
        text = result["choices"][0]["message"]["content"].strip().lower().strip(".")
        # "none" is a legitimate, expected answer (most files match nothing)
        # and is returned as the literal string, distinct from Python None,
        # which means the API call itself failed - callers use that
        # distinction to persist "none" but retry a genuine failure later.
        return text if text in categories or text == "none" else None
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, TimeoutError):
        return None


def _ssl_context():
    """Uses certifi's CA bundle when available - the stock python.org macOS
    build doesn't install root certificates, which otherwise breaks HTTPS
    verification for this one outbound call."""
    try:
        import certifi
        import ssl

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return None


def origin_segment(source_roots: list[Path], source_path: str) -> str:
    """Sanitized last 1-2 original subdirectory names, so a flat output
    directory still records where the file came from. Word-boundary split
    (same as slugify()) so a proper-noun directory name like
    "FreightTrackerDocumentations" becomes separate words rather than one
    fused token - both readable on its own, and necessary for the
    cross-field word dedup in cmd_rename_plan to actually see the words in
    it and compare them against the rest of the name."""
    path = Path(source_path)
    for root in source_roots:
        try:
            rel_parts = path.parent.relative_to(root).parts
            break
        except ValueError:
            continue
    else:
        return "ROOT"
    if not rel_parts:
        return "ROOT"
    kept = rel_parts[-2:] if len(rel_parts) > 1 else rel_parts
    cleaned = [safe_field(insert_word_boundaries(part), "") for part in kept]
    cleaned = [c for c in cleaned if c]
    return "-".join(cleaned)[:50] if cleaned else "ROOT"


def cmd_rename_plan(env: dict) -> None:
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    source_roots = [Path(r.strip()) for r in env["SOURCE_DATA_ROOTS"].split(",") if r.strip()]
    api_key = env.get("OPENAI_API_KEY")

    rows = conn.execute(
        "SELECT * FROM catalogue WHERE is_repo_rollup = 0 ORDER BY catalogue_id"
    ).fetchall()

    plan_rows = []
    ai_used = 0
    slug_sources = Counter()
    for row in rows:
        date_part = to_au_date_token(row["document_date"])
        cls = row["file_class"] or "OPS"
        org = safe_field(row["source_organisation"])
        system = safe_field(row["source_system"], "NA")
        artefact = safe_field(row["artefact_type"], "OTHER")
        # catalogue_id is a DB primary key, globally unique by construction -
        # always include it (not just when there's no domain entity id) so
        # proposed_filename can never collide, e.g. two different repeat-export
        # files for the same container/date that the AI happens to describe
        # with the same slug (domain entity id + date + slug alone isn't
        # guaranteed unique).
        domain_id = row["primary_entity_id"] if row["primary_entity_id"] and row["primary_entity_id"] != "MULTI" else None
        primary_id = f"{domain_id}_{row['catalogue_id']}" if domain_id else row["catalogue_id"]
        ext = (row["extension"] or "").lstrip(".")
        status = "RAW"
        access = row["access_classification"] or "INTERNAL"
        origin = origin_segment(source_roots, row["source_path"])

        slug, confidence, source = None, 0.3, "catalogue_id"
        title = (row["title"] or "").strip()
        if title and title.lower() not in GENERIC_TITLE_VALUES:
            title_slug = slugify(title)
            title_words = title_slug.split("-")
            # A real human-written title never repeats a word back-to-back;
            # that pattern means the embedded "title" metadata is actually a
            # mangled citation/reference code (e.g. "Emerald_IMDS_IMDS613954
            # 1498..1509"), not trustworthy - fall through to AI instead.
            has_adjacent_repeat = any(title_words[i] == title_words[i + 1] for i in range(len(title_words) - 1))
            if not has_adjacent_repeat:
                slug = title_slug
                confidence, source = 0.75, "embedded_title"

        # AI-primary: a trustworthy embedded title wins outright (real
        # document metadata), but otherwise every record with an api_key
        # gets an AI pass rather than only the ones the deterministic regex
        # split fails on - regex can't tell a corrupted/noisy filename from
        # a good one, only that it produced *some* words.
        deterministic_hint = clean_filename_slug(row["original_filename"])
        if source != "embedded_title" and api_key:
            if row["short_title"]:
                slug = row["short_title"]  # cached from a previous rename-plan run, no API call needed
                confidence, source = 0.45, "ai_suggested"
            else:
                ai_slug = ai_suggest_slug(
                    api_key, row["original_filename"], cls, artefact, row["content_preview"], deterministic_hint,
                )
                ai_used += 1 if ai_slug else 0
                if ai_slug:
                    slug, confidence, source = ai_slug, 0.45, "ai_suggested"

        if not slug and deterministic_hint:
            slug, confidence, source = deterministic_hint, 0.5, "original_filename"
        slug_sources[source] += 1
        # slugify() joins words with "-" internally (used elsewhere, e.g. the
        # cached short_title column); only at this final assembly point do we
        # convert to "_" so the whole filename has one separator character.
        slug_part = (slug or row["catalogue_id"]).replace("-", "_")

        # Class-led: standardized/controlled-vocabulary tokens first (quick to
        # scan and group in a flat folder), descriptive/contextual tokens
        # (what it's about, company, source directory) last. Single "_" between
        # tokens, whole name upper-cased - only the extension stays as-is.
        #
        # The descriptive tail (slug, org/system, origin) can independently
        # each mention the same real-world name - e.g. an AI slug containing
        # "freighttracker", the org/system field also FREIGHTTRACKER, and the
        # origin directory "FreightTrackerDocumentations" - so dedupe across
        # those fields in sequence, keeping the first occurrence and dropping
        # the word from every later field. Never dedupe *within* one field
        # (that's how a real repeated term like "RO_RO" for roll-on/roll-off
        # shipping, or "DATA" in both "BIG_DATA" and "DATA_SCIENCE", survives).
        org_system_candidates = [p for p in ([org] if org == system else [org, system]) if p]
        slug_words = slug_part.split("_") if slug_part else []
        origin_words = origin.split("-") if origin else []
        deduped_slug, deduped_org_system, deduped_origin = dedupe_across_fields(
            [slug_words, org_system_candidates, origin_words]
        )
        slug_part = "_".join(deduped_slug) if deduped_slug else slug_part
        org_system = "_".join(deduped_org_system)
        origin_final = "_".join(deduped_origin) if deduped_origin else "ROOT"

        # Citation-safe reference: the structural/controlled-vocabulary
        # prefix only (class, artefact type, catalogue/entity id, date,
        # version, status, access) - none of the descriptive tail (slug,
        # org, system, origin), which is effectively the original filename
        # converted into words and can carry business-sensitive detail
        # (client names, commercial context) not meant for a public thesis.
        schema_reference = "_".join([cls, artefact, primary_id, date_part, "v01", status, access]).upper()

        base_parts = [cls, artefact, primary_id, date_part, "v01", status, access, slug_part]
        if org_system:
            base_parts.append(org_system)
        base_parts.append(origin_final)
        base = "_".join(base_parts).upper()
        candidate = f"{base}.{ext}" if ext else base

        # Only genuine SHA-256-verified duplicates get a duplicate marker in the
        # filename; this is never used just to resolve name collisions.
        if row["duplicate_status"] == "exact_duplicate" and row["canonical_catalogue_id"]:
            marker = f"_DUPOF-{row['canonical_catalogue_id']}".upper()
            candidate = f"{base}{marker}.{ext}" if ext else f"{base}{marker}"

        slug_note = None if source in ("embedded_title",) else (
            f"proposed_filename slug source: {source}" + (" (AI-suggested, verify)" if source == "ai_suggested" else "")
        )
        # Idempotent: drop any slug-source note from a previous rename-plan run
        # before appending the current one, so reruns don't pile up duplicates.
        existing_parts = [
            p for p in (row["review_notes"] or "").split(" | ")
            if p and not p.startswith("proposed_filename slug source:")
        ]
        if slug_note:
            existing_parts.append(slug_note)
        review_note = " | ".join(existing_parts) or None

        short_title = slug if source == "ai_suggested" else row["short_title"]

        conn.execute(
            "UPDATE catalogue SET proposed_filename = ?, rename_confidence = ?, "
            "review_notes = ?, short_title = ?, schema_reference = ?, updated_at = ? WHERE catalogue_id = ?",
            (candidate, confidence, review_note, short_title, schema_reference, now, row["catalogue_id"]),
        )
        evidence_source_type = row["evidence_source_type"] if row["evidence_source_type"] not in (None, "none") else ""
        plan_rows.append((row["catalogue_id"], row["summary"] or "", schema_reference, row["original_filename"],
                          candidate, evidence_source_type, row["source_path"], source, row["source_group_id"]))

    conn.commit()
    conn.close()

    with RENAME_PLAN_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        # proposed_filename sits right next to original_filename (not after
        # the long source_path) so before/after is easy to compare at a glance.
        # summary and schema_reference sit right next to catalogue_id, the
        # other stable identifier column: summary gives a human a one-line
        # sense of the file without opening it; schema_reference (structural
        # prefix only, no slug/org/system/origin - citable in a paper without
        # exposing filename-derived detail) is the citation-safe id.
        # evidence_source_type ("" not "none", for a cleaner blank-if-unflagged
        # look when opened in a spreadsheet) comes from `classify-evidence`.
        writer.writerow(["catalogue_id", "summary", "schema_reference", "original_filename", "proposed_filename",
                         "evidence_source_type", "source_path", "slug_source", "source_group_id"])
        writer.writerows(plan_rows)

    print(
        f"Pass 3 (rename plan) complete: {len(plan_rows)} proposed names -> {RENAME_PLAN_PATH.name}. "
        "No files renamed, moved or copied.\n"
        f"Slug sources: {dict(slug_sources)}"
        + (f" (AI calls made: {ai_used})" if api_key else " (no OPENAI_API_KEY set, AI fallback skipped)")
    )
REVIEW_QUEUE_PATH = CATALOGUE_DIR / "human_review_queue.csv"


def cmd_review_queue() -> None:
    """Triage report: human_review_required is 1 by default for every record
    (nothing in this engine clears it), so it alone isn't a useful filter.
    Instead this ranks records by *why* they need a look - exact/near
    duplicates, low-confidence proposed filenames or metadata, an existing
    review_notes flag, or a use_decision that's still undecided - so the top
    of the CSV is what actually needs a human's time first, not just a full
    dump of every record in catalogue_id order."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM catalogue WHERE is_repo_rollup = 0 AND human_review_required = 1 ORDER BY catalogue_id"
    ).fetchall()

    queue_rows = []
    for row in rows:
        reasons = []
        if row["duplicate_status"] == "exact_duplicate":
            reasons.append(f"exact duplicate of {row['canonical_catalogue_id'] or 'unresolved canonical'}")
        elif row["duplicate_status"] == "near_duplicate":
            score = row["near_duplicate_score"]
            score_txt = f"{score:.2f}" if score is not None else "unscored"
            reasons.append(f"near duplicate (score {score_txt}) of {row['near_duplicate_canonical_id'] or 'unresolved canonical'}")
        if row["rename_confidence"] is not None and row["rename_confidence"] < 0.6:
            reasons.append(f"low-confidence proposed filename ({row['rename_confidence']:.2f})")
        if row["metadata_confidence"] is not None and row["metadata_confidence"] < 0.6:
            reasons.append(f"low-confidence metadata ({row['metadata_confidence']:.2f})")
        if row["review_notes"]:
            reasons.append(row["review_notes"])
        # Weakest signal last: every unprocessed record starts 'undecided', so
        # this only tips the sort order, never the only reason shown alone
        # unless nothing else flagged the record.
        if row["use_decision"] == "undecided":
            reasons.append("use decision not yet made")

        queue_rows.append((
            len(reasons), row["catalogue_id"], " | ".join(reasons), row["file_class"], row["artefact_type"],
            row["short_title"] or "", row["summary"] or "", row["duplicate_status"], row["rename_confidence"],
            row["use_decision"], row["original_filename"], row["proposed_filename"] or "", row["source_path"],
        ))

    # Most reasons first (most urgent triage), catalogue_id as tiebreaker.
    queue_rows.sort(key=lambda r: (-r[0], r[1]))

    conn.close()

    with REVIEW_QUEUE_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["catalogue_id", "review_reasons", "file_class", "artefact_type", "short_title", "summary",
                         "duplicate_status", "rename_confidence", "use_decision", "original_filename",
                         "proposed_filename", "source_path"])
        writer.writerows(r[1:] for r in queue_rows)

    print(f"Review queue: {len(queue_rows)} records -> {REVIEW_QUEUE_PATH.name}")


# --------------------------------------------------------------------------
# Export JSONL + stats
# --------------------------------------------------------------------------

ARRAY_FIELDS = ["authors", "secondary_entity_ids", "source_fields", "document_sections", "claim_ids", "keywords"]
OBJECT_FIELDS = ["domain_identifiers", "research_taxonomy"]


def row_to_record(row: sqlite3.Row) -> dict:
    """Shared by export-jsonl and validate-schema: expand a raw DB row into
    the same JSON-shaped record catalogue_master.jsonl writes (arrays/objects
    parsed out of their _json columns, booleans as real bools)."""
    record = dict(row)
    for field in ARRAY_FIELDS:
        record[field] = json.loads(record.pop(f"{field}_json", "[]") or "[]")
    for field in OBJECT_FIELDS:
        record[field] = json.loads(record.pop(f"{field}_json", "{}") or "{}")
    record.pop("repo_extension_breakdown_json", None) if not record.get("is_repo_rollup") else None
    if record.get("repo_extension_breakdown_json"):
        record["repo_extension_breakdown"] = json.loads(record.pop("repo_extension_breakdown_json"))
    for bool_field in ("contains_personal_data", "contains_commercially_sensitive_data",
                       "ocr_used", "human_review_required", "is_repo_rollup"):
        record[bool_field] = bool(record[bool_field])
    return record


def cmd_export_jsonl() -> None:
    conn = get_db()
    rows = conn.execute("SELECT * FROM catalogue ORDER BY catalogue_id").fetchall()
    with JSONL_PATH.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row_to_record(row), ensure_ascii=False) + "\n")
    conn.close()
    print(f"Exported {len(rows)} records -> {display_path(JSONL_PATH)}")


SCHEMA_VALIDATION_REPORT_PATH = CATALOGUE_DIR / "schema_validation_report.csv"


def cmd_validate_schema() -> None:
    """Validates every non-rollup record against instance/schema.generated.json
    (produced by setup.py). Optional: needs the `jsonschema` package
    (see requirements.txt); skips with a clear message rather than failing
    if it isn't installed, since nothing else in this engine has a hard
    dependency beyond the standard library.

    Repo-rollup records (is_repo_rollup=1) and DB-only bookkeeping columns
    (is_repo_rollup, repo_file_count, repo_total_size_bytes,
    repo_extension_breakdown) are out of scope: schema.generated.json models
    one citable file record, not the repo-aggregate bookkeeping this engine
    also stores."""
    try:
        import jsonschema
    except ImportError:
        print(
            "jsonschema not installed - skipping schema validation. "
            "Run: pip install -r requirements.txt (or `pip install jsonschema`)."
        )
        return

    schema_path = INSTANCE_DIR / "schema.generated.json"
    if not schema_path.exists():
        print(f"{schema_path.relative_to(ROOT_DIR)} not found - run `python3 setup.py` first.")
        return
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema_props = set(schema.get("properties", {}).keys())
    validator = jsonschema.Draft202012Validator(schema)

    conn = get_db()
    rows = conn.execute("SELECT * FROM catalogue WHERE is_repo_rollup = 0 ORDER BY catalogue_id").fetchall()
    conn.close()

    violations = []
    for row in rows:
        record = row_to_record(row)
        # Only the fields schema.generated.json actually models - repo/DB
        # bookkeeping columns aren't part of the published record shape.
        record = {k: v for k, v in record.items() if k in schema_props}
        for error in validator.iter_errors(record):
            path = "/".join(str(p) for p in error.path) or "(root)"
            violations.append((row["catalogue_id"], path, error.message))

    with SCHEMA_VALIDATION_REPORT_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["catalogue_id", "field", "error"])
        writer.writerows(violations)

    if violations:
        by_field = Counter(v[1] for v in violations)
        print(
            f"Schema validation: {len(violations)} violations across "
            f"{len(set(v[0] for v in violations))}/{len(rows)} records -> "
            f"{SCHEMA_VALIDATION_REPORT_PATH.name}. Top fields: {dict(by_field.most_common(5))}"
        )
    else:
        print(f"Schema validation: {len(rows)} records all conform to {schema_path.name}. No violations.")


def cmd_verify() -> None:
    """Data-integrity regression guard. Catches classes of bug like the one
    found 2026-07-16: original_filename silently drifting from the real
    basename of source_path across rewrites of the scan logic, because scan()
    skips already-inventoried rows and never re-derives already-stored fields."""
    conn = get_db()
    rows = conn.execute(
        "SELECT catalogue_id, original_filename, source_path, proposed_filename "
        "FROM catalogue WHERE is_repo_rollup = 0"
    ).fetchall()

    problems = []
    seen_names: dict[str, str] = {}
    for row in rows:
        real_name = Path(row["source_path"]).name
        if real_name != row["original_filename"]:
            problems.append(f"{row['catalogue_id']}: original_filename != basename(source_path)")
        if not Path(row["source_path"]).exists():
            problems.append(f"{row['catalogue_id']}: source_path no longer exists on disk")
        if row["proposed_filename"]:
            prior = seen_names.get(row["proposed_filename"])
            if prior:
                problems.append(f"{row['catalogue_id']}: proposed_filename collides with {prior}")
            seen_names[row["proposed_filename"]] = row["catalogue_id"]

    # Sanity check on top of hash-based duplicate detection: two files with the
    # same SHA-256 are mathematically guaranteed to be the same size, so any
    # mismatch here means a stored hash or size drifted (stale scan data),
    # not a real hash collision.
    dup_groups = conn.execute(
        "SELECT duplicate_group_id, catalogue_id, file_size_bytes FROM catalogue "
        "WHERE is_repo_rollup = 0 AND duplicate_group_id IS NOT NULL "
        "ORDER BY duplicate_group_id"
    ).fetchall()
    sizes_by_group: dict[str, set] = {}
    ids_by_group: dict[str, list] = {}
    for row in dup_groups:
        sizes_by_group.setdefault(row["duplicate_group_id"], set()).add(row["file_size_bytes"])
        ids_by_group.setdefault(row["duplicate_group_id"], []).append(row["catalogue_id"])
    for group_id, sizes in sizes_by_group.items():
        if len(sizes) > 1:
            problems.append(
                f"duplicate_group {group_id}: members {ids_by_group[group_id]} share a hash "
                f"but have different file_size_bytes {sizes} - stale hash or size, re-run scan"
            )

    conn.close()
    if problems:
        print(f"verify FAILED: {len(problems)} problem(s) found:")
        for p in problems[:50]:
            print(f"  - {p}")
        if len(problems) > 50:
            print(f"  ... and {len(problems) - 50} more")
    else:
        print(f"verify passed: {len(rows)} records, no filename/path integrity problems found.")


def cmd_add_context(env: dict, limit: int | None = None) -> None:
    """AI-assisted, opt-in (never part of `all`, since it costs one API call
    per record): fills the free-text `summary` column with a one-sentence
    description of document purpose / producing system / counterparty -
    context that the controlled-vocabulary source_organisation/source_system
    fields and the naming convention deliberately don't carry. Skips records
    that already have a summary, so reruns only cover new/uncovered rows."""
    api_key = env.get("OPENAI_API_KEY")
    if not api_key:
        print("No OPENAI_API_KEY set in instance/.env - skipping (context summaries require it).")
        return

    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    query = (
        "SELECT catalogue_id, original_filename, file_class, artefact_type, "
        "source_organisation, source_system, content_preview FROM catalogue "
        "WHERE is_repo_rollup = 0 AND (summary IS NULL OR summary = '') "
        "ORDER BY catalogue_id"
    )
    rows = conn.execute(query).fetchall()
    if limit:
        rows = rows[:limit]

    filled = 0
    for row in rows:
        summary = ai_suggest_context(
            api_key, row["original_filename"], row["file_class"] or "", row["artefact_type"] or "",
            row["source_organisation"], row["source_system"], row["content_preview"],
        )
        if summary:
            conn.execute(
                "UPDATE catalogue SET summary = ?, updated_at = ? WHERE catalogue_id = ?",
                (summary, now, row["catalogue_id"]),
            )
            filled += 1
        if filled % 50 == 0 and filled:
            conn.commit()
            print(f"  context filled {filled}/{len(rows)}...")

    conn.commit()
    conn.close()
    print(f"Pass (context) complete: {filled}/{len(rows)} records given an AI-generated summary "
          f"(skipped {len(rows) - filled} on API failure).")


def cmd_classify_evidence(env: dict, project_config: dict, limit: int | None = None) -> None:
    """AI-assisted, opt-in (never part of `all`, one API call per record):
    flags records that are genuinely industry expert feedback, change
    requests, survey results, walkthrough results, etc. - categories
    defined in project_config.json -> evidence_source_types, so a different
    project's set is a config edit, not a code change. Most records match
    none of them; that's stored as the literal "none" (not left blank) so
    a rerun only classifies genuinely new/unprocessed records, rather than
    re-spending API calls on every ordinary file every time."""
    api_key = env.get("OPENAI_API_KEY")
    if not api_key:
        print("No OPENAI_API_KEY set in instance/.env - skipping (evidence classification requires it).")
        return
    categories = [c for c in project_config.get("evidence_source_types", []) if c]
    if not categories:
        print("No evidence_source_types configured in project_config.json -> evidence_source_types - skipping.")
        return

    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    query = (
        "SELECT catalogue_id, original_filename, file_class, artefact_type, content_preview "
        "FROM catalogue WHERE is_repo_rollup = 0 AND evidence_source_type IS NULL ORDER BY catalogue_id"
    )
    rows = conn.execute(query).fetchall()
    if limit:
        rows = rows[:limit]

    checked, matched = 0, Counter()
    for row in rows:
        result = ai_classify_evidence_source_type(
            api_key, categories, row["original_filename"], row["file_class"] or "", row["artefact_type"] or "",
            row["content_preview"],
        )
        if result is not None:  # "none" is a real, persisted answer; only a hard API failure leaves it NULL to retry
            conn.execute(
                "UPDATE catalogue SET evidence_source_type = ?, updated_at = ? WHERE catalogue_id = ?",
                (result, now, row["catalogue_id"]),
            )
            checked += 1
            matched[result] += 1
        if checked % 50 == 0 and checked:
            conn.commit()
            print(f"  evidence classification checked {checked}/{len(rows)}...")

    conn.commit()
    conn.close()
    flagged = {k: v for k, v in matched.items() if k != "none"}
    print(f"Pass (classify-evidence) complete: {checked}/{len(rows)} records checked "
          f"(skipped {len(rows) - checked} on API failure). Flagged: {dict(flagged)}")


def apply_rename_dest_dir(row: sqlite3.Row, nested: bool, group_literature: bool, source_roots: list[Path]) -> Path:
    """Where a given record's copy lands under CATALOGUE_DOCUMENTS_DIR,
    depending on the chosen layout. group_literature takes priority over
    nested - LIT always lands in literature/ regardless of the other layout,
    since the point is pulling it out of whatever the main layout would
    otherwise be."""
    if group_literature and row["file_class"] == "LIT":
        return CATALOGUE_DOCUMENTS_DIR / "literature"
    if nested:
        source_path = Path(row["source_path"])
        for root in source_roots:
            try:
                rel_dir = source_path.parent.relative_to(root)
                return CATALOGUE_DOCUMENTS_DIR / rel_dir if str(rel_dir) != "." else CATALOGUE_DOCUMENTS_DIR
            except ValueError:
                continue
    return CATALOGUE_DOCUMENTS_DIR


def cmd_apply_rename(env: dict, skip_duplicates: bool, nested: bool, group_literature: bool, execute: bool) -> None:
    """Pass 4 (approved rename): copies each source file into
    instance/catalogued_files/documents/ under its proposed_filename. Never
    renames, moves, or deletes the source - copy only. Always a conscious,
    explicit action: not part of `all`, and dry-run (prints what it would
    do) unless --execute is passed, so a plan can be reviewed before
    anything is written to disk.

    Copies land in documents/, one level below catalog.html/catalogue_master.*/
    *_report.csv - those are the pipeline's own tooling/report output and stay
    directly under catalogued_files/, so neither directory listing mixes the
    two: catalogued_files/ is "the tools", catalogued_files/documents/ is "the
    research files".

    No per-file metadata sidecar is written next to each copy - that would
    mean one extra .json file per research file sitting in the same folder
    (previously literally doubled the file count in documents/). The single
    catalogue_master.jsonl at the catalogued_files/ root already carries every
    record's full metadata, and catalog.html (also at that root) already gives
    per-file lookup for it through a web interface - click any row to expand
    its full record. That single file is the reference; nothing per-copy is
    needed alongside it.

    Layout is flat by default (everything directly under documents/).
    --nested mirrors each file's original source subdirectory instead.
    --group-literature carves LIT records out into documents/literature/
    regardless of the other layout, so the ~450 literature files don't
    dominate/crowd out everything else."""
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    source_roots = [Path(r.strip()) for r in env["SOURCE_DATA_ROOTS"].split(",") if r.strip()]
    query = "SELECT * FROM catalogue WHERE is_repo_rollup = 0 AND proposed_filename IS NOT NULL AND proposed_filename != ''"
    if skip_duplicates:
        query += " AND duplicate_status != 'exact_duplicate'"
    rows = conn.execute(query + " ORDER BY catalogue_id").fetchall()

    excluded_dupes = 0
    if skip_duplicates:
        excluded_dupes = conn.execute(
            "SELECT COUNT(*) c FROM catalogue WHERE is_repo_rollup = 0 AND duplicate_status = 'exact_duplicate'"
        ).fetchone()["c"]

    layout_desc = ("literature/ split out, " if group_literature else "") + ("nested" if nested else "flat")
    if not execute:
        print(f"DRY RUN (no files written - pass --execute to actually copy): "
              f"{len(rows)} files would be copied to {CATALOGUE_DOCUMENTS_DIR.relative_to(ROOT_DIR)}/ ({layout_desc} layout)"
              + (f", {excluded_dupes} exact duplicates skipped" if skip_duplicates else "") + ".")
        for row in rows[:10]:
            dest_dir = apply_rename_dest_dir(row, nested, group_literature, source_roots)
            rel = dest_dir.relative_to(CATALOGUE_DOCUMENTS_DIR)
            prefix = f"{rel}/" if str(rel) != "." else ""
            print(f"  {row['catalogue_id']}: {Path(row['source_path']).name} -> "
                  f"{prefix}{row['proposed_filename']}")
        if len(rows) > 10:
            print(f"  ... and {len(rows) - 10} more")
        conn.close()
        return

    copied, already_present = 0, 0
    for row in rows:
        dest_dir = apply_rename_dest_dir(row, nested, group_literature, source_roots)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / row["proposed_filename"]
        if dest.exists():
            already_present += 1
            continue
        shutil.copy2(row["source_path"], dest)
        conn.execute(
            "UPDATE catalogue SET processing_status = 'renamed', updated_at = ? WHERE catalogue_id = ?",
            (now, row["catalogue_id"]),
        )
        copied += 1

    conn.commit()
    conn.close()
    print(f"Pass 4 (apply-rename) complete: {copied} files copied ({layout_desc} layout), "
          f"{already_present} already present, "
          + (f"{excluded_dupes} exact duplicates skipped. " if skip_duplicates else "")
          + "Source files untouched.")


def cmd_stats() -> None:
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) c FROM catalogue").fetchone()["c"]
    by_class = conn.execute(
        "SELECT file_class, COUNT(*) c FROM catalogue GROUP BY file_class ORDER BY c DESC"
    ).fetchall()
    by_status = conn.execute(
        "SELECT processing_status, COUNT(*) c FROM catalogue GROUP BY processing_status ORDER BY c DESC"
    ).fetchall()
    dup = conn.execute(
        "SELECT COUNT(*) c FROM catalogue WHERE duplicate_status = 'exact_duplicate'"
    ).fetchone()["c"]
    review = conn.execute(
        "SELECT COUNT(*) c FROM catalogue WHERE human_review_required = 1"
    ).fetchone()["c"]
    repos = conn.execute("SELECT COUNT(*) c FROM catalogue WHERE is_repo_rollup = 1").fetchone()["c"]

    print(f"Total catalogue records: {total} (including {repos} repo rollups)")
    print("By file_class:")
    for r in by_class:
        print(f"  {r['file_class']:6s} {r['c']}")
    print("By processing_status:")
    for r in by_status:
        print(f"  {r['processing_status']:16s} {r['c']}")
    print(f"Exact duplicates flagged: {dup}")
    print(f"Records requiring human review: {review} / {total}")
    conn.close()


def cmd_all(project_config: dict, env: dict, dry_run: bool = False, limit: int | None = None) -> None:
    """Runs the full non-AI, non-apply-rename pipeline in order.

    --dry-run: every cmd_* below manages its own sqlite3 connection and
    commits independently (scan commits every 200 rows, extract/enrich/etc.
    commit and close at the end of each pass), so a single rolled-back
    transaction can't span the whole chain - closing a connection with a
    "no-op'd" commit only rolls back that one pass, and the next pass would
    open a fresh connection to a DB that never actually received it. The
    only way to preview the *whole chain* faithfully is to run it for real
    against a disposable copy of catalogue.db, then discard the copy.
    instance/catalogue.db and instance/catalogued_files/ are never opened in
    this mode - every path a cmd_* function writes through is redirected to
    a temp directory for the duration of the call.

    --limit N: scan runs first, unrestricted, against the full real source
    tree (so "0 new files found" still means what it normally means); only
    after that does the copy get trimmed down to its first N catalogue_ids,
    so every later pass - extract, enrich, dedup, rename-plan, review-queue,
    export, verify, stats - operates on exactly that N-record sample."""
    global DB_PATH, JSONL_PATH, DUPLICATE_REPORT_PATH, RENAME_PLAN_PATH, UNREADABLE_REPORT_PATH, \
        REVIEW_QUEUE_PATH, SCHEMA_VALIDATION_REPORT_PATH

    if not dry_run:
        cmd_scan(project_config, env)
        cmd_extract(project_config)
        cmd_enrich(project_config, env)
        cmd_duplicates()
        cmd_near_duplicates()
        cmd_group_files()
        cmd_rename_plan(env)
        cmd_review_queue()
        cmd_export_jsonl()
        cmd_verify()
        cmd_stats()
        return

    real_db_path = DB_PATH
    real_output_paths = (JSONL_PATH, DUPLICATE_REPORT_PATH, RENAME_PLAN_PATH, UNREADABLE_REPORT_PATH,
                          REVIEW_QUEUE_PATH, SCHEMA_VALIDATION_REPORT_PATH)
    tmp_dir = Path(tempfile.mkdtemp(prefix="catalogue_dry_run_"))
    tmp_db_path = tmp_dir / "catalogue.db"
    if real_db_path.exists():
        shutil.copy2(real_db_path, tmp_db_path)

    DB_PATH = tmp_db_path
    JSONL_PATH = tmp_dir / JSONL_PATH.name
    DUPLICATE_REPORT_PATH = tmp_dir / DUPLICATE_REPORT_PATH.name
    RENAME_PLAN_PATH = tmp_dir / RENAME_PLAN_PATH.name
    UNREADABLE_REPORT_PATH = tmp_dir / UNREADABLE_REPORT_PATH.name
    REVIEW_QUEUE_PATH = tmp_dir / REVIEW_QUEUE_PATH.name
    SCHEMA_VALIDATION_REPORT_PATH = tmp_dir / SCHEMA_VALIDATION_REPORT_PATH.name

    print(f"DRY RUN: working copy at {tmp_db_path} - {real_db_path.relative_to(ROOT_DIR)} "
          f"will not be opened again until this finishes.")

    try:
        cmd_scan(project_config, env)

        if limit is not None:
            conn = get_db()
            kept = conn.execute("SELECT catalogue_id FROM catalogue ORDER BY catalogue_id LIMIT ?", (limit,)).fetchall()
            kept_ids = [r["catalogue_id"] for r in kept]
            conn.execute(
                f"DELETE FROM catalogue WHERE catalogue_id NOT IN "
                f"({','.join('?' for _ in kept_ids)})", kept_ids
            )
            conn.commit()
            conn.close()
            print(f"Trimmed working copy to {len(kept_ids)} records for the rest of the pipeline.")

        cmd_extract(project_config)
        cmd_enrich(project_config, env)
        cmd_duplicates()
        cmd_near_duplicates()
        cmd_group_files()
        cmd_rename_plan(env)
        cmd_review_queue()
        cmd_export_jsonl()
        cmd_verify()
        cmd_stats()

        print(f"\nDRY RUN complete. Nothing written to {real_db_path.relative_to(ROOT_DIR)} or "
              f"{CATALOGUE_DIR.relative_to(ROOT_DIR)}/. Preview output left at {tmp_dir} for inspection "
              "(not auto-deleted).")
        for name in ("rename_plan.csv", "human_review_queue.csv"):
            preview_path = tmp_dir / name
            if preview_path.exists():
                lines = preview_path.read_text(encoding="utf-8").splitlines()
                print(f"\n{name} ({len(lines) - 1 if lines else 0} rows), first 4 lines:")
                for line in lines[:4]:
                    print(f"  {line[:200]}")
    finally:
        DB_PATH = real_db_path
        (JSONL_PATH, DUPLICATE_REPORT_PATH, RENAME_PLAN_PATH, UNREADABLE_REPORT_PATH,
         REVIEW_QUEUE_PATH, SCHEMA_VALIDATION_REPORT_PATH) = real_output_paths


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    command = sys.argv[1]

    project_config = load_json(INSTANCE_DIR / "project_config.json")
    env = load_env()
    CATALOGUE_DIR.mkdir(parents=True, exist_ok=True)

    if command == "scan":
        cmd_scan(project_config, env)
    elif command == "extract":
        cmd_extract(project_config)
    elif command == "enrich":
        cmd_enrich(project_config, env)
    elif command == "duplicates":
        cmd_duplicates()
    elif command == "near-duplicates":
        cmd_near_duplicates()
    elif command == "group":
        cmd_group_files()
    elif command == "context":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
        cmd_add_context(env, limit)
    elif command == "classify-evidence":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
        cmd_classify_evidence(env, project_config, limit)
    elif command == "rename-plan":
        cmd_rename_plan(env)
    elif command == "review-queue":
        cmd_review_queue()
    elif command == "apply-rename":
        args = sys.argv[2:]
        skip_duplicates = "--skip-duplicates" in args
        nested = "--nested" in args
        group_literature = "--group-literature" in args
        execute = "--execute" in args
        cmd_apply_rename(env, skip_duplicates, nested, group_literature, execute)
    elif command == "export-jsonl":
        cmd_export_jsonl()
    elif command == "validate-schema":
        cmd_validate_schema()
    elif command == "verify":
        cmd_verify()
    elif command == "stats":
        cmd_stats()
    elif command == "all":
        args = sys.argv[2:]
        dry_run = "--dry-run" in args
        limit = int(args[args.index("--limit") + 1]) if "--limit" in args else None
        cmd_all(project_config, env, dry_run=dry_run, limit=limit)
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
