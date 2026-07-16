#!/usr/bin/env python3
"""Research file cataloguing engine (Pass 1-3).

Reads instance/project_config.json + instance/.env, walks SOURCE_DATA_ROOTS,
and produces:
  - instance/catalogue.db               (SQLite, primary queryable store)
  - instance/catalogued_files/catalogue_master.jsonl  (append-only audit log)
  - instance/catalogued_files/duplicate_report.csv
  - instance/catalogued_files/rename_plan.csv          (PROPOSAL ONLY)

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
    python3 catalogue.py group         # group by base filename (e.g. repeat report exports/downloads)
    python3 catalogue.py context [N]   # AI: fill `summary` with document purpose/producing-system/
                                        # counterparty context (costs 1 API call/record; not part of
                                        # `all` - run explicitly; optional N caps it to a pilot batch)
    python3 catalogue.py rename-plan   # Pass 3: propose filenames, write rename_plan.csv (no renaming)
    python3 catalogue.py apply-rename [--skip-duplicates] [--execute]
                                        # Pass 4: copy sources -> instance/catalogued_files/ under their
                                        # proposed_filename + a .meta.json sidecar. Dry-run by default
                                        # (prints the plan); nothing is written until --execute is passed.
                                        # --skip-duplicates omits files flagged duplicate_status=exact_duplicate.
    python3 catalogue.py export-jsonl  # refresh catalogue_master.jsonl from the DB
    python3 catalogue.py verify        # data-integrity regression check (filename/path consistency)
    python3 catalogue.py all           # scan + extract + enrich + duplicates + group + rename-plan + export + verify + stats
    python3 catalogue.py stats         # summary counts
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import mimetypes
import re
import shutil
import sqlite3
import subprocess
import sys
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
DB_PATH = INSTANCE_DIR / "catalogue.db"
JSONL_PATH = CATALOGUE_DIR / "catalogue_master.jsonl"
DUPLICATE_REPORT_PATH = CATALOGUE_DIR / "duplicate_report.csv"
RENAME_PLAN_PATH = CATALOGUE_DIR / "rename_plan.csv"
UNREADABLE_REPORT_PATH = CATALOGUE_DIR / "unreadable_or_encrypted_report.csv"

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
    if "source_group_id" not in existing_cols:
        conn.execute("ALTER TABLE catalogue ADD COLUMN source_group_id TEXT")
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_catalogue_source_group_id ON catalogue(source_group_id)")
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

def cap_words(text: str, max_words: int) -> str:
    words = text.split()
    return " ".join(words[:max_words])


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
    try:
        return path.read_text(encoding="utf-8", errors="ignore"), "plain_read", False
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


def extract_pdf_metadata(path: Path) -> dict:
    try:
        import fitz

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


def slugify(text: str, max_words: int = 10, max_len: int = 70) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    # Split concatenated camelCase/PascalCase runs and letter<->digit runs into
    # separate words before collapsing everything else to hyphens, e.g.
    # "jobManagementReportWorkSchedule29Aug2025" -> "job management report
    # work schedule 29 aug 2025" rather than one unreadable blob.
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    text = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", text)
    text = re.sub(r"(?<=[0-9])(?=[A-Za-z])", " ", text)
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
        "prefixes that carry no meaning. Do not invent facts not supported by the filename/content. "
        "Reply with lowercase words separated by hyphens, no file extension, no punctuation besides "
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
    directory still records where the file came from."""
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
    cleaned = [safe_field(part, "") for part in kept]
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
        primary_id = row["primary_entity_id"] if row["primary_entity_id"] and row["primary_entity_id"] != "MULTI" else row["catalogue_id"]
        ext = (row["extension"] or "").lstrip(".")
        status = "RAW"
        access = row["access_classification"] or "INTERNAL"
        origin = origin_segment(source_roots, row["source_path"])

        slug, confidence, source = None, 0.3, "catalogue_id"
        title = (row["title"] or "").strip()
        if title and title.lower() not in GENERIC_TITLE_VALUES:
            slug = slugify(title)
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
        # org/system collapse to one token when identical (e.g. dir_org_system
        # mapping FreightTracker's directory to both org=system=FREIGHTTRACKER)
        # rather than repeating the same word twice.
        org_system = org if org == system else f"{org}_{system}"
        base = f"{cls}_{artefact}_{primary_id}_{date_part}_v01_{status}_{access}_{slug_part}_{org_system}_{origin}".upper()
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
            "review_notes = ?, short_title = ?, updated_at = ? WHERE catalogue_id = ?",
            (candidate, confidence, review_note, short_title, now, row["catalogue_id"]),
        )
        plan_rows.append((row["catalogue_id"], row["original_filename"], row["source_path"], candidate, source,
                          row["source_group_id"]))

    conn.commit()
    conn.close()

    with RENAME_PLAN_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["catalogue_id", "original_filename", "source_path", "proposed_filename", "slug_source",
                         "source_group_id"])
        writer.writerows(plan_rows)

    print(
        f"Pass 3 (rename plan) complete: {len(plan_rows)} proposed names -> {RENAME_PLAN_PATH.name}. "
        "No files renamed, moved or copied.\n"
        f"Slug sources: {dict(slug_sources)}"
        + (f" (AI calls made: {ai_used})" if api_key else " (no OPENAI_API_KEY set, AI fallback skipped)")
    )


# --------------------------------------------------------------------------
# Export JSONL + stats
# --------------------------------------------------------------------------

ARRAY_FIELDS = ["authors", "secondary_entity_ids", "source_fields", "document_sections", "claim_ids", "keywords"]
OBJECT_FIELDS = ["domain_identifiers", "research_taxonomy"]


def cmd_export_jsonl() -> None:
    conn = get_db()
    rows = conn.execute("SELECT * FROM catalogue ORDER BY catalogue_id").fetchall()
    with JSONL_PATH.open("w", encoding="utf-8") as fh:
        for row in rows:
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
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    conn.close()
    print(f"Exported {len(rows)} records -> {JSONL_PATH.relative_to(ROOT_DIR)}")


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


def cmd_apply_rename(skip_duplicates: bool, execute: bool) -> None:
    """Pass 4 (approved rename): copies each source file into
    instance/catalogued_files/ under its proposed_filename, with a
    <name>.meta.json sidecar carrying the full catalogue record. Never
    renames, moves, or deletes the source - copy only. Always a conscious,
    explicit action: not part of `all`, and dry-run (prints what it would do)
    unless --execute is passed, so a plan can be reviewed before anything is
    written to disk."""
    conn = get_db()
    now = datetime.now(timezone.utc).isoformat()
    query = "SELECT * FROM catalogue WHERE is_repo_rollup = 0 AND proposed_filename IS NOT NULL AND proposed_filename != ''"
    if skip_duplicates:
        query += " AND duplicate_status != 'exact_duplicate'"
    rows = conn.execute(query + " ORDER BY catalogue_id").fetchall()

    excluded_dupes = 0
    if skip_duplicates:
        excluded_dupes = conn.execute(
            "SELECT COUNT(*) c FROM catalogue WHERE is_repo_rollup = 0 AND duplicate_status = 'exact_duplicate'"
        ).fetchone()["c"]

    if not execute:
        print(f"DRY RUN (no files written - pass --execute to actually copy): "
              f"{len(rows)} files would be copied to {CATALOGUE_DIR.relative_to(ROOT_DIR)}/"
              + (f", {excluded_dupes} exact duplicates skipped" if skip_duplicates else "") + ".")
        for row in rows[:10]:
            print(f"  {row['catalogue_id']}: {Path(row['source_path']).name} -> {row['proposed_filename']}")
        if len(rows) > 10:
            print(f"  ... and {len(rows) - 10} more")
        conn.close()
        return

    copied, already_present = 0, 0
    for row in rows:
        dest = CATALOGUE_DIR / row["proposed_filename"]
        if dest.exists():
            already_present += 1
            continue
        shutil.copy2(row["source_path"], dest)
        meta = {k: row[k] for k in row.keys()}
        (CATALOGUE_DIR / f"{row['proposed_filename']}.meta.json").write_text(
            json.dumps(meta, indent=2, default=str, ensure_ascii=False), encoding="utf-8"
        )
        conn.execute(
            "UPDATE catalogue SET processing_status = 'renamed', updated_at = ? WHERE catalogue_id = ?",
            (now, row["catalogue_id"]),
        )
        copied += 1

    conn.commit()
    conn.close()
    print(f"Pass 4 (apply-rename) complete: {copied} files copied, {already_present} already present, "
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
    elif command == "group":
        cmd_group_files()
    elif command == "context":
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else None
        cmd_add_context(env, limit)
    elif command == "rename-plan":
        cmd_rename_plan(env)
    elif command == "apply-rename":
        skip_duplicates = "--skip-duplicates" in sys.argv[2:]
        execute = "--execute" in sys.argv[2:]
        cmd_apply_rename(skip_duplicates, execute)
    elif command == "export-jsonl":
        cmd_export_jsonl()
    elif command == "verify":
        cmd_verify()
    elif command == "stats":
        cmd_stats()
    elif command == "all":
        cmd_scan(project_config, env)
        cmd_extract(project_config)
        cmd_enrich(project_config, env)
        cmd_duplicates()
        cmd_group_files()
        cmd_rename_plan(env)
        cmd_export_jsonl()
        cmd_verify()
        cmd_stats()
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
