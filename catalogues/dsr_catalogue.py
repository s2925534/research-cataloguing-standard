#!/usr/bin/env python3
"""Design Science Research (DSR) catalogue mode - activated by --dsr.

This module is a fully separate, additive cataloguing pipeline. It never
opens instance/catalogue.db or writes into instance/catalogued_files/ (the
legacy engine's tables/files) - it has its own database
(instance/catalogue_dsr.db), its own counters, and its own output directory
(instance/catalogued_files/dsr/). Nothing here can corrupt or reclassify an
existing legacy catalogue record. catalogue.py only calls into this module
when --dsr is present on the command line; without --dsr, this module is
never imported at call time and has zero effect.

Engine-generic by design: everything here is standards/file-type mapping
logic that applies to any DSR project, not this specific research project's
content. Project-specific overrides (a custom project code, extra directory
markers, reference-update roots) come from instance/project_config.json,
never hardcoded here - see templates/dsr_classification_rules.template.json.

Implements the deterministic decision order specified for --dsr:
  1. exclusion rules            (is_excluded)
  2. existing catalogue identity (identity carried by source_path)
  3. checksum comparison         (duplicate-content detection)
  4. explicit sidecar metadata   (<file>.dsrmeta.json)
  5. file extension mapping      (DEFAULT_EXTENSION_MAP + PDF/image/token refinement)
  6. directory mapping           (overrides extension mapping)
  7. DSR artefact determination  (semantic filename-token rules, ART class only)
  8. version determination       (filename token > sidecar > default V0.1)
  9. stable ID assignment        (per class/subtype counter, never reused)
  10. ambiguity handling          (Requires Review / Not Assigned, never guessed)

Nothing here invents metadata: any field this engine cannot deterministically
derive from the file itself, its path, or explicit sidecar/project_config
data is recorded as "Unknown", "Not Assigned", or "Requires Review".
"""
from __future__ import annotations

import csv
import hashlib
import json
import mimetypes
import re
import shutil
import sqlite3
import tempfile
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# dsr_catalogue.py lives in catalogues/, one level below the project root
# where instance/ actually is - parent.parent, not parent.
ROOT_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = ROOT_DIR / "instance"
CATALOGUE_DIR = INSTANCE_DIR / "catalogued_files"
DSR_OUTPUT_DIR = CATALOGUE_DIR / "dsr"
DSR_DB_PATH = INSTANCE_DIR / "catalogue_dsr.db"

DSR_RESEARCH_CATALOGUE_CSV = DSR_OUTPUT_DIR / "research_catalogue.csv"
DSR_RESEARCH_CATALOGUE_JSON = DSR_OUTPUT_DIR / "research_catalogue.json"
DSR_RESEARCH_CATALOGUE_MD = DSR_OUTPUT_DIR / "research_catalogue.md"
DSR_RESEARCH_CATALOGUE_SQLITE = DSR_OUTPUT_DIR / "research_catalogue.sqlite"
DSR_RELATIONSHIPS_CSV = DSR_OUTPUT_DIR / "catalogue_relationships.csv"
DSR_SCHEMA_JSON = DSR_OUTPUT_DIR / "catalogue_schema.json"
DSR_CONTROLLED_VOCAB_JSON = DSR_OUTPUT_DIR / "catalogue_controlled_vocabulary.json"
DSR_MIGRATION_LOG_CSV = DSR_OUTPUT_DIR / "catalogue_migration_log.csv"
DSR_MANUAL_REVIEW_CSV = DSR_OUTPUT_DIR / "catalogue_manual_review.csv"
DSR_CLASSIFICATION_RULES_JSON = DSR_OUTPUT_DIR / "catalogue_classification_rules.json"


def display_path(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT_DIR))
    except ValueError:
        return str(p)


# --------------------------------------------------------------------------
# Controlled vocabulary (fixed by the DSR standard itself, not per-project)
# --------------------------------------------------------------------------

MAIN_CLASSES = ["ART", "DOC", "DAT", "IMG", "REF", "COD", "REC", "VAL", "ADM", "ARC"]

ARTEFACT_SUBTYPES = {
    "CON": "Construct", "MOD": "Model", "MET": "Method", "INS": "Instantiation",
    "FRM": "Integrated Framework", "PRI": "Design Principle", "RUL": "Rule Set",
    "TAX": "Taxonomy or Typology", "ONT": "Ontology or Meta-model",
}

DSR_ARTEFACT_CLASSIFICATIONS = [
    "Construct", "Model", "Method", "Instantiation", "Integrated Framework",
    "Not Applicable", "Requires Review",
]

KNOWLEDGE_CONTRIBUTION_CLASSIFICATIONS = [
    "Exaptation", "Improvement", "Invention", "Routine Design",
    "Not Assigned", "Requires Review",
]

REQUIRES_REVIEW = "Requires Review"
NOT_ASSIGNED = "Not Assigned"
NOT_APPLICABLE = "Not Applicable"
UNKNOWN = "Unknown"

# Subtype used only when class_code == ART but no deterministic artefact rule
# matched (Step 10: never guess a real subtype). Not part of the ART subtype
# vocabulary above - flagged Requires Review everywhere it appears.
ART_UNRESOLVED_SUBTYPE = "UNK"


# --------------------------------------------------------------------------
# Step 1: exclusion rules
# --------------------------------------------------------------------------

EXCLUDED_DIR_NAMES = {
    ".git", ".idea", ".vscode", "node_modules", "__pycache__", ".pytest_cache",
    "venv", ".venv", "env", ".env_dir", "build", "dist", ".mypy_cache", ".tox",
}
EXCLUDED_FILENAME_PREFIXES = ("~$",)
EXCLUDED_FILENAMES = {".DS_Store"}
# .exe/.dmg: application installers, never a research artefact regardless of
# project - excluded at Step 1 so a scan never catalogues one in the first
# place, rather than relying on a post-hoc Requires Review + manual exclude.
EXCLUDED_SUFFIXES = (".tmp", ".bak", ".swp", ".exe", ".dmg")


def is_excluded(path: Path, source_root: Path) -> bool:
    try:
        rel_parts = path.relative_to(source_root).parts
    except ValueError:
        rel_parts = path.parts
    if any(part in EXCLUDED_DIR_NAMES for part in rel_parts[:-1]):
        return True
    name = path.name
    if name in EXCLUDED_FILENAMES:
        return True
    if any(name.startswith(prefix) for prefix in EXCLUDED_FILENAME_PREFIXES):
        return True
    if any(name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
        return True
    return False


# --------------------------------------------------------------------------
# Step 5: file-extension mapping -> default (class, subtype)
# --------------------------------------------------------------------------

DEFAULT_EXTENSION_MAP = {
    # Documents
    ".docx": ("DOC", "WRK"), ".doc": ("DOC", "WRK"), ".odt": ("DOC", "WRK"),
    ".rtf": ("DOC", "WRK"), ".md": ("DOC", "WRK"), ".txt": ("DOC", "WRK"),
    ".html": ("DOC", "WRK"), ".htm": ("DOC", "WRK"),
    ".tex": ("DOC", "THS"),
    # Structured data
    ".csv": ("DAT", "CSV"), ".tsv": ("DAT", "CSV"),
    ".xlsx": ("DAT", "XLS"), ".xls": ("DAT", "XLS"), ".ods": ("DAT", "XLS"),
    ".json": ("DAT", "JSE"), ".jsonl": ("DAT", "JSE"),
    ".xml": ("DAT", "XML"), ".xes": ("DAT", "LOG"),
    ".parquet": ("DAT", "DER"), ".feather": ("DAT", "DER"), ".avro": ("DAT", "DER"),
    # Databases
    ".sqlite": ("DAT", "DBS"), ".sqlite3": ("DAT", "DBS"), ".db": ("DAT", "DBS"),
    ".duckdb": ("DAT", "DBS"), ".mdb": ("DAT", "DBS"), ".accdb": ("DAT", "DBS"),
    # Images and diagrams (subtype refined further by _refine_image_subtype)
    ".png": ("IMG", "DIA"), ".jpg": ("IMG", "PHO"), ".jpeg": ("IMG", "PHO"),
    ".tif": ("IMG", "PHO"), ".tiff": ("IMG", "PHO"), ".gif": ("IMG", "DIA"),
    ".svg": ("IMG", "DIA"), ".webp": ("IMG", "DIA"), ".drawio": ("IMG", "DIA"),
    ".mmd": ("IMG", "DIA"), ".puml": ("IMG", "DIA"), ".plantuml": ("IMG", "DIA"),
    ".bpmn": ("IMG", "DIA"), ".archimate": ("IMG", "ARC"),
    # Source code
    ".py": ("COD", "PYT"), ".ipynb": ("COD", "NBK"),
    ".js": ("COD", "APP"), ".ts": ("COD", "APP"), ".java": ("COD", "APP"),
    ".cs": ("COD", "APP"), ".php": ("COD", "APP"), ".go": ("COD", "APP"),
    ".rs": ("COD", "APP"), ".rb": ("COD", "APP"), ".cpp": ("COD", "APP"),
    ".c": ("COD", "APP"),
}

# .sql is context-dependent per the spec (dump vs executable query/migration);
# resolved in _classify_extension rather than the flat map above.
SQL_DUMP_TOKENS = ("dump", "backup", "export")
SQL_MIGRATION_TOKENS = ("migration", "migrate")

# Filename-token overrides for source code files that would otherwise fall
# through to COD-APP/COD-PYT (config/test/CLI/API-spec/migration files).
CODE_TOKEN_SUBTYPE = [
    ("migration", "MIG"), ("migrate", "MIG"),
    ("test_", "TST"), ("_test", "TST"), ("spec_", "TST"), ("_spec", "TST"),
    ("config", "CFG"), ("settings", "CFG"),
    ("cli", "CLI"), ("__main__", "CLI"),
    ("openapi", "API"), ("swagger", "API"), ("api-spec", "API"), ("apispec", "API"),
]

IMAGE_TOKEN_SUBTYPE = [
    ("system-architecture", "ARC"), ("architecture", "ARC"),
    ("state-transition", "STA"), ("state-machine", "STA"),
    ("dependency", "DEP"),
    ("lifecycle", "LIF"),
    ("screenshot", "SCR"),
]

REC_TOKEN_SUBTYPE = [
    ("meeting", "MTG"), ("transcript", "TRN"), ("decision", "DEC"),
    ("feedback", "FDB"), ("review-comments", "FDB"),
    ("research-journal", "JRN"), ("research-log", "JRN"),
    ("observation", "OBS"), ("correspondence", "COR"), ("email", "COR"),
]

VAL_TOKEN_SUBTYPE = [
    ("experiment", "EXP"), ("simulation", "SIM"), ("walkthrough", "WKT"),
    ("survey", "SUR"), ("interview", "INT"),
    ("case-study", "CAS"), ("case-application", "CAS"),
    ("deterministic-test", "TST"), ("validation-review", "REV"), ("benchmark", "BEN"),
]

ADM_TOKEN_SUBTYPE = [
    ("annual-progress", "APR"), ("apr", "APR"), ("seminar", "SEM"),
    ("ethics", "ETH"), ("risk-management", "RMP"), ("rmp", "RMP"),
    ("milestone", "MIL"), ("approval", "APP"), ("application", "APP"),
    ("independent-review", "REV"),
]

# Priority order for the ART subtype (Step 7): first token match wins.
ARTEFACT_TOKEN_SUBTYPE = [
    ("framework", "FRM"), ("integrated-framework", "FRM"), ("reference-framework", "FRM"),
    ("prototype", "INS"), ("implementation", "INS"), ("application", "INS"),
    ("rule-engine", "INS"), ("service", "INS"), ("dashboard", "INS"), ("executable", "INS"),
    ("method", "MET"), ("methodology", "MET"), ("procedure", "MET"), ("algorithm", "MET"),
    ("evaluation-process", "MET"), ("calculation", "MET"), ("workflow-method", "MET"),
    ("model", "MOD"), ("architecture", "MOD"), ("lifecycle", "MOD"), ("state-machine", "MOD"),
    ("state-transition", "MOD"), ("dependency", "MOD"), ("meta-model", "MOD"),
    ("construct", "CON"), ("concept", "CON"), ("conceptual-definition", "CON"),
    ("vocabulary", "CON"), ("terminology", "CON"), ("data-construct", "CON"),
    ("design-principle", "PRI"), ("rule-set", "RUL"), ("taxonomy", "TAX"),
    ("typology", "TAX"), ("ontology", "ONT"),
]

ARTEFACT_SUBTYPE_TO_CLASSIFICATION = {
    "FRM": "Integrated Framework", "INS": "Instantiation", "MET": "Method",
    "MOD": "Model", "CON": "Construct",
    "PRI": "Requires Review", "RUL": "Requires Review", "TAX": "Requires Review",
    "ONT": "Requires Review",
}

# Directory-name substrings -> main class (Step 6, overrides extension mapping).
DEFAULT_DIRECTORY_CLASS_MAP = [
    ("/artefacts/", "ART"), ("/documents/", "DOC"),
    ("/data/", "DAT"), ("/datasets/", "DAT"),
    ("/images/", "IMG"), ("/figures/", "IMG"), ("/diagrams/", "IMG"),
    ("/references/", "REF"), ("/literature/", "REF"),
    ("/code/", "COD"), ("/src/", "COD"), ("/scripts/", "COD"),
    ("/meetings/", "REC"), ("/records/", "REC"), ("/transcripts/", "REC"),
    ("/validation/", "VAL"), ("/evaluation/", "VAL"), ("/experiments/", "VAL"),
    ("/administration/", "ADM"), ("/milestones/", "ADM"),
    ("/archive/", "ARC"),
]

VERSION_TOKEN_RE = re.compile(r"(?:^|[_\-. ])[vV](\d+)(?:[._](\d+))?(?:[_\-. ]|$)")


def _known_subtypes_by_class() -> dict[str, set[str]]:
    """Every (class_code, subtype_code) pair this engine's own deterministic
    rules can ever produce - the closed vocabulary --ai-decide-review must
    validate an AI-proposed classification against. Built from the rule
    tables themselves (not hand-maintained separately) so it can never drift
    out of sync with what the deterministic rules actually assign."""
    by_class: dict[str, set[str]] = {}

    def add(cls: str, sub: str) -> None:
        by_class.setdefault(cls, set()).add(sub)

    for cls, sub in DEFAULT_EXTENSION_MAP.values():
        add(cls, sub)
    for _, sub in IMAGE_TOKEN_SUBTYPE:
        add("IMG", sub)
    for _, sub in CODE_TOKEN_SUBTYPE:
        add("COD", sub)
    for _, sub in REC_TOKEN_SUBTYPE:
        add("REC", sub)
    for _, sub in VAL_TOKEN_SUBTYPE:
        add("VAL", sub)
    for _, sub in ADM_TOKEN_SUBTYPE:
        add("ADM", sub)
    for sub in ARTEFACT_SUBTYPES:
        add("ART", sub)
    for sub in ("JRN", "CNF", "BOK", "STD", "GOV", "GRY"):  # PDF-branch reference subtypes
        add("REF", sub)
    add("DOC", "RPT")
    add("DAT", "SQL")
    add("COD", "SQL")
    return by_class


KNOWN_SUBTYPES_BY_CLASS = _known_subtypes_by_class()
AI_ASSIGNED = "AI-Assigned"


def default_dsr_rules() -> dict:
    """The generic engine defaults, in the same shape catalogue_classification_rules.json
    is written in. A project can extend (never replace) directory markers and
    the project code via instance/project_config.json -> dsr_catalogue_rules."""
    return {
        "excluded_dir_names": sorted(EXCLUDED_DIR_NAMES),
        "excluded_filename_prefixes": list(EXCLUDED_FILENAME_PREFIXES),
        "excluded_filenames": sorted(EXCLUDED_FILENAMES),
        "excluded_suffixes": list(EXCLUDED_SUFFIXES),
        "extension_map": {ext: f"{c}-{s}" for ext, (c, s) in DEFAULT_EXTENSION_MAP.items()},
        "sql_dump_tokens": list(SQL_DUMP_TOKENS),
        "sql_migration_tokens": list(SQL_MIGRATION_TOKENS),
        "code_token_subtype": dict(CODE_TOKEN_SUBTYPE),
        "image_token_subtype": dict(IMAGE_TOKEN_SUBTYPE),
        "research_record_token_subtype": dict(REC_TOKEN_SUBTYPE),
        "validation_token_subtype": dict(VAL_TOKEN_SUBTYPE),
        "administrative_token_subtype": dict(ADM_TOKEN_SUBTYPE),
        "artefact_token_subtype_priority": list(ARTEFACT_TOKEN_SUBTYPE),
        "directory_class_map": list(DEFAULT_DIRECTORY_CLASS_MAP),
        "artefact_subtypes": ARTEFACT_SUBTYPES,
        "dsr_artefact_classifications": DSR_ARTEFACT_CLASSIFICATIONS,
        "knowledge_contribution_classifications": KNOWLEDGE_CONTRIBUTION_CLASSIFICATIONS,
        "classification_priority": [
            "existing_catalogue_metadata", "validated_sidecar_metadata",
            "explicit_directory_mapping", "filename_token_mapping",
            "file_extension_mapping", "requires_review",
        ],
        "artefact_type_priority": [
            "Integrated Framework", "Instantiation", "Method", "Model", "Construct",
            "specialist_subtype (Design Principle / Rule Set / Taxonomy / Ontology)",
            "Requires Review",
        ],
    }


def load_dsr_rules(project_config: dict) -> dict:
    """Merges project_config.json -> dsr_catalogue_rules (optional, project-
    specific directory markers / project code) over the generic engine
    defaults. Never removes or overrides the fixed DSR vocabulary itself."""
    rules = default_dsr_rules()
    overrides = project_config.get("dsr_catalogue_rules", {})
    extra_dirs = overrides.get("extra_directory_class_map", [])
    if extra_dirs:
        rules["directory_class_map"] = [tuple(pair) for pair in extra_dirs] + rules["directory_class_map"]
    rules["directory_class_map"] = [tuple(pair) for pair in rules["directory_class_map"]]
    rules["project_code"] = overrides.get("project_code", project_config.get("dsr_project_code", "DSR"))
    rules["id_padding"] = int(overrides.get("id_padding", project_config.get("dsr_catalogue_id_prefix_padding", 4)))
    return rules


# --------------------------------------------------------------------------
# SQLite schema (fully separate database - never shares a file, table, or
# counter sequence with the legacy engine's instance/catalogue.db)
# --------------------------------------------------------------------------

DSR_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS dsr_catalogue (
    catalogue_id TEXT PRIMARY KEY,
    stable_id TEXT NOT NULL,
    version TEXT NOT NULL,
    title TEXT,
    description TEXT,
    file_name TEXT,
    relative_path TEXT,
    source_path TEXT,
    extension TEXT,
    mime_type TEXT,
    status TEXT DEFAULT 'active',
    author TEXT DEFAULT 'Unknown',
    created_date TEXT,
    modified_date TEXT,
    provenance TEXT DEFAULT 'Unknown',
    class_code TEXT,
    subtype_code TEXT,
    dsr_artefact_type TEXT DEFAULT 'Not Applicable',
    knowledge_contribution TEXT DEFAULT 'Not Assigned',
    research_phase TEXT DEFAULT 'Unknown',
    purpose TEXT DEFAULT 'Unknown',
    parent_id TEXT,
    child_ids_json TEXT DEFAULT '[]',
    derived_from_json TEXT DEFAULT '[]',
    supporting_json TEXT DEFAULT '[]',
    supersedes_id TEXT,
    superseded_by_id TEXT,
    evaluation_status TEXT DEFAULT 'Not Assigned',
    evaluation_method TEXT DEFAULT 'Unknown',
    evaluation_evidence TEXT DEFAULT 'Unknown',
    sha256 TEXT,
    file_size_bytes INTEGER,
    legacy_ids_json TEXT DEFAULT '[]',
    classification_rule TEXT,
    classification_evidence TEXT,
    confidence_status TEXT DEFAULT 'Requires Review',
    notes TEXT,
    problem_addressed TEXT DEFAULT 'Unknown',
    objective TEXT DEFAULT 'Unknown',
    scope TEXT DEFAULT 'Unknown',
    boundaries TEXT DEFAULT 'Unknown',
    components_json TEXT DEFAULT '[]',
    design_principles_json TEXT DEFAULT '[]',
    kernel_theories_json TEXT DEFAULT '[]',
    justificatory_knowledge TEXT DEFAULT 'Unknown',
    previous_version_id TEXT,
    change_summary TEXT DEFAULT 'Unknown',
    evolution_description TEXT DEFAULT 'Unknown',
    evaluation_strategy TEXT DEFAULT 'Unknown',
    expected_utility TEXT DEFAULT 'Unknown',
    observed_utility TEXT DEFAULT 'Unknown',
    limitations TEXT DEFAULT 'Unknown',
    duplicate_status TEXT DEFAULT 'unresolved',
    duplicate_group_id TEXT,
    excluded_reason TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_dsr_stable_id ON dsr_catalogue(stable_id);
CREATE INDEX IF NOT EXISTS idx_dsr_sha256 ON dsr_catalogue(sha256);
CREATE UNIQUE INDEX IF NOT EXISTS idx_dsr_source_path ON dsr_catalogue(source_path);

CREATE TABLE IF NOT EXISTS dsr_counters (
    class_code TEXT NOT NULL,
    subtype_code TEXT NOT NULL,
    next_seq INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (class_code, subtype_code)
);

CREATE TABLE IF NOT EXISTS dsr_relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_stable_id TEXT NOT NULL,
    target_stable_id TEXT,
    target_reference TEXT,
    relationship_type TEXT NOT NULL,
    evidence TEXT,
    created_at TEXT
);
"""


def get_dsr_db(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DSR_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(DSR_SCHEMA_SQL)
    conn.commit()
    return conn


def next_dsr_seq(conn: sqlite3.Connection, class_code: str, subtype_code: str) -> int:
    row = conn.execute(
        "SELECT next_seq FROM dsr_counters WHERE class_code = ? AND subtype_code = ?",
        (class_code, subtype_code),
    ).fetchone()
    seq = row["next_seq"] if row else 1
    conn.execute(
        "INSERT INTO dsr_counters (class_code, subtype_code, next_seq) VALUES (?, ?, ?) "
        "ON CONFLICT(class_code, subtype_code) DO UPDATE SET next_seq = ?",
        (class_code, subtype_code, seq + 1, seq + 1),
    )
    return seq


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_source_files(source_root: Path):
    for path in source_root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if is_excluded(path, source_root):
            continue
        yield path


# --------------------------------------------------------------------------
# Classification pipeline (steps 4-8)
# --------------------------------------------------------------------------

def _find_token(name_lower: str, token_subtype: list[tuple[str, str]]) -> str | None:
    for token, subtype in token_subtype:
        if token in name_lower:
            return subtype
    return None


def _sniff_extensionless_content(path: Path, rel_lower: str) -> tuple[str, str, str, str] | None:
    """Step 5 continuation for files with no extension at all: a missing
    extension isn't the same as unrecognisable content, and a cheap magic-
    byte/prefix check on the first 512 bytes resolves a real, common case
    (files that lost their extension somewhere along the way - a browser
    "save as", an email export, a raw API response dump) deterministically,
    the same way the extension map does for files that kept theirs. Returns
    None (falls through to Requires Review, same as any other unmapped
    case) rather than guessing when nothing recognisable matches."""
    try:
        with path.open("rb") as fh:
            head = fh.read(512)
    except OSError:
        return None

    if head.startswith(b"%PDF-"):
        if any(marker in rel_lower for marker in ("/references/", "/literature/")):
            return "REF", "GRY", "content_sniff:pdf_no_extension", "no extension, content is PDF (%PDF- magic bytes), under references/literature dir"
        if any(marker in rel_lower for marker in ("/documents/", "/reports/")):
            return "DOC", "RPT", "content_sniff:pdf_no_extension", "no extension, content is PDF (%PDF- magic bytes), under documents/reports dir"
        return "REF", "GRY", "content_sniff:pdf_no_extension", "no extension, content is PDF (%PDF- magic bytes), no directory signal"

    stripped = head.lstrip()
    if stripped[:15].lower().startswith(b"<!doctype html") or stripped[:5].lower().startswith(b"<html"):
        return "DOC", "WRK", "content_sniff:html_no_extension", "no extension, content starts with an HTML doctype/tag"

    if stripped[:1] in (b"{", b"["):
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            pass
        else:
            return "DAT", "JSE", "content_sniff:json_no_extension", "no extension, content parses as valid JSON"

    mail_headers = (b"From:", b"Return-Path:", b"Delivered-To:", b"Received:", b"Message-ID:")
    if any(head.startswith(h) for h in mail_headers):
        return "REC", "COR", "content_sniff:email_no_extension", "no extension, content starts with an RFC 822 mail header"

    return None


def _classify_extension(path: Path, rel_lower: str, name_lower: str) -> tuple[str, str, str, str]:
    """Returns (class_code, subtype_code, rule, evidence) purely from
    extension + filename tokens (Step 5), before any directory override."""
    ext = path.suffix.lower()

    if not ext:
        sniffed = _sniff_extensionless_content(path, rel_lower)
        if sniffed:
            return sniffed

    if ext == ".sql":
        if any(t in name_lower for t in SQL_DUMP_TOKENS):
            return "DAT", "SQL", "extension:sql_dump_token", f"filename contains dump token, ext={ext}"
        return "COD", "SQL", "extension:sql_default", f"ext={ext} (no dump token, treated as executable query)"

    if ext == ".pdf":
        if any(marker in rel_lower for marker in ("/references/", "/literature/")):
            for tok, subtype in (("journal", "JRN"), ("conference", "CNF"), ("book", "BOK"), ("standard", "STD"),
                                  ("government", "GOV"), ("institutional", "GOV")):
                if tok in name_lower:
                    return "REF", subtype, "directory+filename_token:pdf_reference", f"marker=/references|literature/, token={tok}"
            return "REF", "GRY", "fallback:unresolved_pdf", "PDF under references/literature dir, no type token matched"
        if any(marker in rel_lower for marker in ("/documents/", "/reports/")):
            return "DOC", "RPT", "directory:pdf_authored_report", "marker=/documents|reports/"
        if any(marker in rel_lower for marker in ("/diagrams/", "/figures/")):
            return "IMG", "DIA", "directory:pdf_exported_diagram", "marker=/diagrams|figures/"
        return "REF", "GRY", "fallback:unresolved_pdf", "no directory/filename signal to resolve PDF origin"

    if ext in DEFAULT_EXTENSION_MAP:
        cls, sub = DEFAULT_EXTENSION_MAP[ext]
        if cls == "IMG":
            refined = _find_token(name_lower, IMAGE_TOKEN_SUBTYPE)
            if refined:
                return "IMG", refined, "filename_token:image_subtype", f"token matched in {IMAGE_TOKEN_SUBTYPE}"
            if "screenshot" in rel_lower:
                return "IMG", "SCR", "directory:screenshots", "path contains screenshots dir"
            return "IMG", sub, "extension:default_map", f"ext={ext}"
        if cls == "COD":
            refined = _find_token(name_lower, CODE_TOKEN_SUBTYPE)
            if refined:
                return "COD", refined, "filename_token:code_subtype", "token matched code subtype rules"
            return "COD", sub, "extension:default_map", f"ext={ext}"
        return cls, sub, "extension:default_map", f"ext={ext}"

    return REQUIRES_REVIEW, ART_UNRESOLVED_SUBTYPE, "fallback:unmapped_extension", f"ext={ext or '(none)'} has no mapping"


def _apply_filename_token_class(name_lower: str) -> tuple[str, str, str, str] | None:
    """Step 5 continuation: REC/VAL/ADM filename tokens can select the class
    itself, independent of extension (e.g. a .docx meeting note -> REC-MTG)."""
    for token_map, cls in ((REC_TOKEN_SUBTYPE, "REC"), (VAL_TOKEN_SUBTYPE, "VAL"), (ADM_TOKEN_SUBTYPE, "ADM")):
        subtype = _find_token(name_lower, token_map)
        if subtype:
            return cls, subtype, f"filename_token:{cls.lower()}", f"token matched {cls} rules"
    return None


def _apply_directory_class(rel_lower: str, directory_class_map: list) -> str | None:
    for marker, cls in directory_class_map:
        if marker in rel_lower:
            return cls
    return None


def _determine_artefact_type(name_lower: str) -> tuple[str, str]:
    """Step 7: only called once class_code has resolved to ART. Returns
    (subtype_code, dsr_artefact_type)."""
    for token, subtype in ARTEFACT_TOKEN_SUBTYPE:
        if token in name_lower:
            return subtype, ARTEFACT_SUBTYPE_TO_CLASSIFICATION.get(subtype, REQUIRES_REVIEW)
    return ART_UNRESOLVED_SUBTYPE, REQUIRES_REVIEW


def _determine_version(name_stem: str) -> tuple[str, bool]:
    """Step 8. Returns (version, requires_review). Never defaults to V1.0."""
    m = VERSION_TOKEN_RE.search(name_stem + " ")
    if m:
        major = int(m.group(1))
        minor = int(m.group(2)) if m.group(2) else 0
        return f"V{major}.{minor}", False
    return "V0.1", True


def load_sidecar_metadata(path: Path) -> dict | None:
    """Step 4: <file>.dsrmeta.json next to the source file. Only trusted if
    it names a valid class/subtype from the fixed vocabulary - otherwise
    ignored (falls through to extension/directory rules), never trusted
    blindly."""
    sidecar = path.with_name(path.name + ".dsrmeta.json")
    if not sidecar.exists():
        return None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    cls = data.get("class_code")
    sub = data.get("subtype_code")
    if cls not in MAIN_CLASSES:
        return None
    if cls == "ART" and sub not in ARTEFACT_SUBTYPES and sub != ART_UNRESOLVED_SUBTYPE:
        return None
    return data


def classify_file(path: Path, source_root: Path, rules: dict) -> dict:
    """Runs steps 4-8 for a single file. Returns a dict of classification
    fields (class_code, subtype_code, dsr_artefact_type, version,
    requires_review, classification_rule, classification_evidence)."""
    rel = str(path.relative_to(source_root))
    # Leading slash so a "/artefacts/" style marker matches a file directly
    # under that top-level directory (rel itself never starts with "/").
    rel_lower = "/" + rel.lower().replace("\\", "/")
    name_lower = path.name.lower()

    sidecar = load_sidecar_metadata(path)
    if sidecar:
        cls, sub = sidecar["class_code"], sidecar["subtype_code"]
        rule, evidence = "explicit_sidecar_metadata", f"sidecar={path.name}.dsrmeta.json"
    else:
        dir_class = _apply_directory_class(rel_lower, rules["directory_class_map"])
        token_result = _apply_filename_token_class(name_lower)
        if dir_class:
            cls = dir_class
            if cls == "ART":
                sub, _ = _determine_artefact_type(name_lower)
                rule, evidence = "directory_mapping:ART", f"directory marker resolved class=ART, subtype from filename tokens"
            elif token_result and token_result[0] == cls:
                sub, rule, evidence = token_result[1], token_result[2], token_result[3]
            else:
                ext_cls, ext_sub, ext_rule, ext_evidence = _classify_extension(path, rel_lower, name_lower)
                sub = ext_sub if ext_cls == cls else ART_UNRESOLVED_SUBTYPE
                rule, evidence = "directory_mapping", f"directory marker resolved class={cls}; subtype from extension where compatible"
        elif token_result:
            cls, sub, rule, evidence = token_result
        else:
            cls, sub, rule, evidence = _classify_extension(path, rel_lower, name_lower)
            if cls == "ART":
                sub, _ = _determine_artefact_type(name_lower)

    if cls == "ART":
        dsr_artefact_type = ARTEFACT_SUBTYPE_TO_CLASSIFICATION.get(sub, REQUIRES_REVIEW)
    else:
        dsr_artefact_type = NOT_APPLICABLE

    version, version_requires_review = _determine_version(path.stem)

    requires_review = (
        cls == REQUIRES_REVIEW
        or sub == ART_UNRESOLVED_SUBTYPE
        or dsr_artefact_type == REQUIRES_REVIEW
        or version_requires_review
    )
    confidence_status = REQUIRES_REVIEW if requires_review else "Confident"

    return {
        "class_code": cls,
        "subtype_code": sub,
        "dsr_artefact_type": dsr_artefact_type,
        "knowledge_contribution": NOT_ASSIGNED,
        "version": version,
        "confidence_status": confidence_status,
        "classification_rule": rule,
        "classification_evidence": evidence,
    }


# --------------------------------------------------------------------------
# Scan
# --------------------------------------------------------------------------

def _run_scan(conn: sqlite3.Connection, project_config: dict, env: dict, rules: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    padding = rules["id_padding"]
    project_code = rules["project_code"]

    scanned = 0
    excluded_count = 0
    ambiguous = 0
    by_class: Counter = Counter()

    for root_str in env.get("SOURCE_DATA_ROOTS", "").split(","):
        root_str = root_str.strip()
        if not root_str:
            continue
        source_root = Path(root_str)
        if not source_root.exists():
            print(f"WARNING: source root does not exist, skipping: {source_root}")
            continue

        for path in source_root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            if path.name.endswith(".dsrmeta.json"):
                continue
            if is_excluded(path, source_root):
                excluded_count += 1
                continue

            source_path = str(path.resolve())
            existing = conn.execute(
                "SELECT catalogue_id, stable_id, sha256, version FROM dsr_catalogue WHERE source_path = ?",
                (source_path,),
            ).fetchone()

            sha256 = sha256_file(path)
            stat = path.stat()

            if existing:
                # Step 2/3: identity already known by source_path. A checksum
                # change never mints a new stable ID here - only migrate
                # re-evaluates whether a version bump is warranted.
                if existing["sha256"] != sha256:
                    conn.execute(
                        "UPDATE dsr_catalogue SET sha256 = ?, file_size_bytes = ?, modified_date = ?, "
                        "notes = COALESCE(notes || ' | ', '') || 'content changed since last scan - run migrate --dsr to re-evaluate version', "
                        "updated_at = ? WHERE catalogue_id = ?",
                        (sha256, stat.st_size, now, now, existing["catalogue_id"]),
                    )
                continue

            duplicate_of = conn.execute(
                "SELECT stable_id FROM dsr_catalogue WHERE sha256 = ? LIMIT 1", (sha256,)
            ).fetchone()

            classification = classify_file(path, source_root, rules)
            class_code, subtype_code = classification["class_code"], classification["subtype_code"]
            seq = next_dsr_seq(conn, class_code, subtype_code)
            stable_id = f"{project_code}-{class_code}-{subtype_code}-{seq:0{padding}d}"
            catalogue_id = f"{stable_id}-{classification['version']}"
            mime_type, _ = mimetypes.guess_type(path.name)

            notes = None
            if duplicate_of:
                notes = f"sha256 matches existing stable_id={duplicate_of['stable_id']} - flagged duplicate, not auto-merged"

            conn.execute(
                """
                INSERT INTO dsr_catalogue (
                    catalogue_id, stable_id, version, title, file_name, relative_path,
                    source_path, extension, mime_type, class_code, subtype_code,
                    dsr_artefact_type, knowledge_contribution, sha256, file_size_bytes,
                    classification_rule, classification_evidence, confidence_status,
                    duplicate_status, duplicate_group_id, notes,
                    created_date, modified_date, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    catalogue_id, stable_id, classification["version"], path.stem, path.name,
                    str(path.relative_to(source_root)), source_path, path.suffix.lower(), mime_type,
                    class_code, subtype_code, classification["dsr_artefact_type"],
                    classification["knowledge_contribution"], sha256, stat.st_size,
                    classification["classification_rule"], classification["classification_evidence"],
                    classification["confidence_status"],
                    "possible_duplicate" if duplicate_of else "unresolved",
                    duplicate_of["stable_id"] if duplicate_of else None, notes,
                    datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).date().isoformat(),
                    datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).date().isoformat(), now, now,
                ),
            )
            if duplicate_of:
                conn.execute(
                    "INSERT INTO dsr_relationships (source_stable_id, target_stable_id, relationship_type, evidence, created_at) "
                    "VALUES (?, ?, 'possible_duplicate_content', ?, ?)",
                    (stable_id, duplicate_of["stable_id"], f"sha256={sha256}", now),
                )

            scanned += 1
            by_class[f"{class_code}-{subtype_code}"] += 1
            if classification["confidence_status"] == REQUIRES_REVIEW:
                ambiguous += 1
            if scanned % 200 == 0:
                conn.commit()

    conn.commit()
    return {"scanned": scanned, "excluded": excluded_count, "ambiguous": ambiguous, "by_class": dict(by_class)}


def cmd_scan(project_config: dict, env: dict, dry_run: bool, apply: bool) -> None:
    if dry_run == apply:
        raise SystemExit("scan --dsr requires exactly one of --dry-run or --apply")
    rules = load_dsr_rules(project_config)

    if apply:
        conn = get_dsr_db()
        report = _run_scan(conn, project_config, env, rules)
        conn.close()
        print(f"DSR scan (applied): {report['scanned']} new files catalogued, "
              f"{report['excluded']} excluded, {report['ambiguous']} flagged Requires Review.")
        print(f"By class-subtype: {report['by_class']}")
        return

    tmp_dir = Path(tempfile.mkdtemp(prefix="dsr_scan_dry_run_"))
    tmp_db_path = tmp_dir / "catalogue_dsr.db"
    if DSR_DB_PATH.exists():
        shutil.copy2(DSR_DB_PATH, tmp_db_path)
    conn = get_dsr_db(tmp_db_path)
    report = _run_scan(conn, project_config, env, rules)
    conn.close()
    print(f"DSR scan (--dry-run): {report['scanned']} new files WOULD be catalogued, "
          f"{report['excluded']} excluded, {report['ambiguous']} would be flagged Requires Review.")
    print(f"By class-subtype: {report['by_class']}")
    print(f"Nothing written to {display_path(DSR_DB_PATH)}. Working copy left at {tmp_db_path} for inspection.")


# --------------------------------------------------------------------------
# Migrate: re-run classification over already-scanned rows (no filesystem
# walk), e.g. after a classification-rule change. Records every changed
# field in catalogue_migration_log.csv. Stable IDs and prior versions are
# never altered by migrate - only classification_* fields, dsr_artefact_type,
# and confidence_status can change; a version bump on real content change is
# logged but requires a human to confirm via sidecar metadata (Step 8 never
# auto-assigns a bumped version).
# --------------------------------------------------------------------------

def _run_migrate(conn: sqlite3.Connection, rules: dict) -> list[dict]:
    changes = []
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute("SELECT * FROM dsr_catalogue ORDER BY catalogue_id").fetchall()
    for row in rows:
        source_path = Path(row["source_path"])
        # A directory (a legacy_dsr_migration.py repo-rollup entry) isn't a
        # classifiable file - classify_file() would fall through to the
        # unmapped-extension case and clobber the rollup's real COD
        # classification with the generic "Requires Review" fallback.
        if not source_path.exists() or source_path.is_dir():
            continue
        # Re-derive source_root as the longest known ancestor isn't stored;
        # relative_path + source_path together are enough to reconstruct it.
        source_root = source_path
        for _ in range(len(Path(row["relative_path"]).parts)):
            source_root = source_root.parent
        classification = classify_file(source_path, source_root, rules)

        changed_fields = {}
        for field in ("class_code", "subtype_code", "dsr_artefact_type", "classification_rule",
                       "classification_evidence", "confidence_status"):
            new_val = classification[field]
            if row[field] != new_val:
                changed_fields[field] = (row[field], new_val)

        if changed_fields:
            conn.execute(
                "UPDATE dsr_catalogue SET class_code=?, subtype_code=?, dsr_artefact_type=?, "
                "classification_rule=?, classification_evidence=?, confidence_status=?, updated_at=? "
                "WHERE catalogue_id=?",
                (classification["class_code"], classification["subtype_code"],
                 classification["dsr_artefact_type"], classification["classification_rule"],
                 classification["classification_evidence"], classification["confidence_status"],
                 now, row["catalogue_id"]),
            )
            changes.append({"catalogue_id": row["catalogue_id"], "stable_id": row["stable_id"],
                             "changed_fields": changed_fields})
    conn.commit()
    return changes


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


def _ai_decide_classification(api_key: str, file_name: str, content_preview: str,
                               current_evidence: str) -> dict | None:
    """--ai-decide-review: for a record classify_file() already tried and
    left in Requires Review, ask an LLM to pick a class/subtype from this
    engine's own closed vocabulary (KNOWN_SUBTYPES_BY_CLASS) - never an
    invented one - using the file's actual content. Returns None on any
    failure, timeout, or a response that doesn't validate against that
    vocabulary; the caller leaves the record in Requires Review rather than
    accept an unvalidated guess, matching this engine's "never guess" rule
    for every other classification step."""
    if not api_key:
        return None
    vocab_lines = [
        f"{cls}: {', '.join(sorted(KNOWN_SUBTYPES_BY_CLASS.get(cls, set()))) or '(no fixed subtype list - leave subtype_code empty)'}"
        for cls in MAIN_CLASSES
    ]
    prompt = (
        f"File name: {file_name}\n"
        f"Why deterministic (extension/directory/filename-token) classification couldn't resolve it: {current_evidence}\n"
        f"Content preview (first ~2000 characters):\n{content_preview[:2000]}\n\n"
        "Classify this file using ONLY the DSR main classes and subtype codes listed below - "
        "never invent a class or subtype code not on this list:\n" + "\n".join(vocab_lines) +
        "\n\nReply with strict JSON only, no other text, in this exact shape: "
        '{"class_code": "<one class code from the list>", '
        '"subtype_code": "<one subtype code listed for that class, or empty string if that class has no fixed list>", '
        '"reasoning": "<one sentence, based only on the content shown above>"}'
    )
    payload = json.dumps({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 250,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30, context=_ssl_context()) as response:
            result = json.loads(response.read())
        parsed = json.loads(result["choices"][0]["message"]["content"])
        cls, sub = parsed.get("class_code"), parsed.get("subtype_code") or ""
        reasoning = parsed.get("reasoning", "")
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, TimeoutError, OSError):
        return None

    known_subs = KNOWN_SUBTYPES_BY_CLASS.get(cls)
    if cls not in MAIN_CLASSES:
        return None
    if known_subs and sub not in known_subs:
        return None
    if not known_subs:
        sub = sub or ART_UNRESOLVED_SUBTYPE
    return {"class_code": cls, "subtype_code": sub, "reasoning": reasoning}


def _run_ai_decide_review(conn: sqlite3.Connection, api_key: str) -> list[dict]:
    """Second pass, after _run_migrate: for whatever's still Requires Review
    and not excluded, and has a real (non-directory) file behind it, ask
    _ai_decide_classification to resolve it. Marked confidence_status =
    AI_ASSIGNED (not "Confident") so an AI-derived classification is never
    silently indistinguishable from a deterministically-derived one -
    consistent with this project's standing rule that AI output is never
    treated as equivalent to verified/deterministic evidence."""
    changes = []
    now = datetime.now(timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT * FROM dsr_catalogue WHERE confidence_status = ? AND status != 'excluded' ORDER BY catalogue_id",
        (REQUIRES_REVIEW,),
    ).fetchall()
    for row in rows:
        source_path = Path(row["source_path"])
        if not source_path.exists() or source_path.is_dir():
            continue
        try:
            content_preview = source_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            content_preview = ""
        if not content_preview.strip():
            continue

        decision = _ai_decide_classification(api_key, row["file_name"], content_preview,
                                              row["classification_evidence"] or "")
        if decision is None:
            continue

        dsr_artefact_type = (
            ARTEFACT_SUBTYPE_TO_CLASSIFICATION.get(decision["subtype_code"], NOT_ASSIGNED)
            if decision["class_code"] == "ART" else NOT_APPLICABLE
        )
        conn.execute(
            "UPDATE dsr_catalogue SET class_code=?, subtype_code=?, dsr_artefact_type=?, "
            "classification_rule=?, classification_evidence=?, confidence_status=?, updated_at=? "
            "WHERE catalogue_id=?",
            (
                decision["class_code"], decision["subtype_code"], dsr_artefact_type,
                "ai_decided:--ai-decide-review", f"AI-assigned: {decision['reasoning']}",
                AI_ASSIGNED, now, row["catalogue_id"],
            ),
        )
        changes.append({
            "catalogue_id": row["catalogue_id"], "stable_id": row["stable_id"],
            "changed_fields": {
                "class_code": (row["class_code"], decision["class_code"]),
                "subtype_code": (row["subtype_code"], decision["subtype_code"]),
                "confidence_status": (row["confidence_status"], AI_ASSIGNED),
            },
        })
    conn.commit()
    return changes


def cmd_migrate(project_config: dict, env: dict, dry_run: bool, apply: bool,
                 ai_decide_review: bool = False) -> None:
    if dry_run == apply:
        raise SystemExit("migrate --dsr requires exactly one of --dry-run or --apply")
    rules = load_dsr_rules(project_config)
    api_key = env.get("OPENAI_API_KEY") if ai_decide_review else None
    if ai_decide_review and not api_key:
        raise SystemExit("migrate --dsr --ai-decide-review requires OPENAI_API_KEY in instance/.env")

    if apply:
        conn = get_dsr_db()
        changes = _run_migrate(conn, rules)
        ai_changes = _run_ai_decide_review(conn, api_key) if ai_decide_review else []
        conn.close()
        DSR_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        _write_migration_log(changes + ai_changes)
        print(f"DSR migrate (applied): {len(changes)} records reclassified deterministically"
              + (f", {len(ai_changes)} AI-assigned via --ai-decide-review" if ai_decide_review else "")
              + f" -> {display_path(DSR_MIGRATION_LOG_CSV)}")
        return

    tmp_dir = Path(tempfile.mkdtemp(prefix="dsr_migrate_dry_run_"))
    tmp_db_path = tmp_dir / "catalogue_dsr.db"
    if DSR_DB_PATH.exists():
        shutil.copy2(DSR_DB_PATH, tmp_db_path)
    conn = get_dsr_db(tmp_db_path)
    changes = _run_migrate(conn, rules)
    ai_changes = _run_ai_decide_review(conn, api_key) if ai_decide_review else []
    conn.close()
    print(f"DSR migrate (--dry-run): {len(changes)} records WOULD be reclassified deterministically"
          + (f", {len(ai_changes)} WOULD be AI-assigned via --ai-decide-review" if ai_decide_review else "") + ".")
    for change in changes[:20]:
        print(f"  {change['catalogue_id']}: {change['changed_fields']}")
    for change in ai_changes[:20]:
        print(f"  [AI] {change['catalogue_id']}: {change['changed_fields']}")
    print(f"Nothing written to {display_path(DSR_DB_PATH)}. This still made real API calls (--ai-decide-review previews cost the same as applying).")


def _write_migration_log(changes: list[dict]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    write_header = not DSR_MIGRATION_LOG_CSV.exists()
    with DSR_MIGRATION_LOG_CSV.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if write_header:
            writer.writerow(["timestamp", "catalogue_id", "stable_id", "field", "old_value", "new_value"])
        for change in changes:
            for field, (old, new) in change["changed_fields"].items():
                writer.writerow([now, change["catalogue_id"], change["stable_id"], field, old, new])


# --------------------------------------------------------------------------
# Validate
# --------------------------------------------------------------------------

def cmd_validate(project_config: dict, env: dict) -> None:
    if not DSR_DB_PATH.exists():
        print(f"{display_path(DSR_DB_PATH)} does not exist yet - run `scan --dsr --apply` first.")
        return
    conn = get_dsr_db()
    rows = conn.execute("SELECT * FROM dsr_catalogue ORDER BY catalogue_id").fetchall()

    issues: list[tuple[str, str, str]] = []  # (catalogue_id, category, detail)

    stable_ids = {row["stable_id"] for row in rows}
    version_re = re.compile(r"^V\d+\.\d+$")

    for row in rows:
        if not Path(row["source_path"]).exists():
            issues.append((row["catalogue_id"], "missing_file", row["source_path"]))
        if not version_re.match(row["version"] or ""):
            issues.append((row["catalogue_id"], "invalid_version", row["version"]))
        if row["class_code"] not in MAIN_CLASSES and row["class_code"] != REQUIRES_REVIEW:
            issues.append((row["catalogue_id"], "invalid_class_code", row["class_code"]))
        if row["class_code"] == "ART" and row["subtype_code"] not in ARTEFACT_SUBTYPES and row["subtype_code"] != ART_UNRESOLVED_SUBTYPE:
            issues.append((row["catalogue_id"], "invalid_artefact_subtype", row["subtype_code"]))
        if row["dsr_artefact_type"] not in DSR_ARTEFACT_CLASSIFICATIONS:
            issues.append((row["catalogue_id"], "invalid_dsr_artefact_type", row["dsr_artefact_type"]))
        if row["knowledge_contribution"] not in KNOWLEDGE_CONTRIBUTION_CLASSIFICATIONS:
            issues.append((row["catalogue_id"], "invalid_knowledge_contribution", row["knowledge_contribution"]))
        for id_field in ("supersedes_id", "superseded_by_id", "parent_id"):
            target = row[id_field]
            if target and target not in stable_ids:
                issues.append((row["catalogue_id"], "broken_relationship", f"{id_field}={target}"))
        if row["duplicate_group_id"] and row["duplicate_group_id"] not in stable_ids:
            issues.append((row["catalogue_id"], "orphaned_duplicate_group", row["duplicate_group_id"]))

    rel_rows = conn.execute("SELECT * FROM dsr_relationships").fetchall()
    for rel in rel_rows:
        if rel["target_stable_id"] and rel["target_stable_id"] not in stable_ids:
            issues.append((rel["source_stable_id"], "orphaned_relationship_target", rel["target_stable_id"]))

    schema_props = None
    try:
        if DSR_SCHEMA_JSON.exists():
            schema_props = json.loads(DSR_SCHEMA_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        schema_props = None

    conn.close()

    DSR_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with DSR_MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["catalogue_id", "category", "detail"])
        for row in rows:
            if row["confidence_status"] == REQUIRES_REVIEW:
                writer.writerow([row["catalogue_id"], "requires_review", row["classification_evidence"]])
        for issue in issues:
            writer.writerow(issue)

    if issues:
        by_category = Counter(i[1] for i in issues)
        print(f"DSR validate: {len(issues)} issues across {len(rows)} records -> {display_path(DSR_MANUAL_REVIEW_CSV)}")
        print(f"By category: {dict(by_category)}")
    else:
        print(f"DSR validate: {len(rows)} records, no structural issues found -> {display_path(DSR_MANUAL_REVIEW_CSV)} (review-flagged rows only, if any).")


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

DSR_JSON_ARRAY_FIELDS = ("child_ids", "derived_from", "supporting", "legacy_ids", "components", "design_principles", "kernel_theories")


def _dsr_row_to_record(row: sqlite3.Row) -> dict:
    record = dict(row)
    for field in DSR_JSON_ARRAY_FIELDS:
        record[field] = json.loads(record.pop(f"{field}_json", "[]") or "[]")
    return record


def cmd_export(project_config: dict, env: dict) -> None:
    if not DSR_DB_PATH.exists():
        print(f"{display_path(DSR_DB_PATH)} does not exist yet - run `scan --dsr --apply` first.")
        return
    DSR_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_dsr_db()
    rows = conn.execute("SELECT * FROM dsr_catalogue ORDER BY catalogue_id").fetchall()
    records = [_dsr_row_to_record(r) for r in rows]

    if records:
        fieldnames = list(records[0].keys())
        with DSR_RESEARCH_CATALOGUE_CSV.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for record in records:
                writer.writerow({k: (json.dumps(v) if isinstance(v, (list, dict)) else v) for k, v in record.items()})
    else:
        DSR_RESEARCH_CATALOGUE_CSV.write_text("", encoding="utf-8")

    DSR_RESEARCH_CATALOGUE_JSON.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    md_lines = ["# DSR Research Catalogue", "", f"{len(records)} records.", ""]
    for record in records:
        md_lines.append(f"## {record['catalogue_id']}")
        md_lines.append(f"- Stable ID: {record['stable_id']}")
        md_lines.append(f"- Title: {record.get('title') or UNKNOWN}")
        md_lines.append(f"- Class/Subtype: {record['class_code']}-{record['subtype_code']}")
        md_lines.append(f"- DSR artefact type: {record['dsr_artefact_type']}")
        md_lines.append(f"- Knowledge contribution: {record['knowledge_contribution']}")
        md_lines.append(f"- Confidence: {record['confidence_status']}")
        md_lines.append("")
    DSR_RESEARCH_CATALOGUE_MD.write_text("\n".join(md_lines), encoding="utf-8")

    shutil.copy2(DSR_DB_PATH, DSR_RESEARCH_CATALOGUE_SQLITE)

    rel_rows = conn.execute("SELECT * FROM dsr_relationships ORDER BY id").fetchall()
    with DSR_RELATIONSHIPS_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["source_stable_id", "target_stable_id", "target_reference", "relationship_type", "evidence", "created_at"])
        for rel in rel_rows:
            writer.writerow([rel["source_stable_id"], rel["target_stable_id"], rel["target_reference"],
                              rel["relationship_type"], rel["evidence"], rel["created_at"]])
    conn.close()

    DSR_SCHEMA_JSON.write_text(json.dumps(_dsr_json_schema(), ensure_ascii=False, indent=2), encoding="utf-8")
    DSR_CONTROLLED_VOCAB_JSON.write_text(json.dumps({
        "main_classes": MAIN_CLASSES,
        "artefact_subtypes": ARTEFACT_SUBTYPES,
        "dsr_artefact_classifications": DSR_ARTEFACT_CLASSIFICATIONS,
        "knowledge_contribution_classifications": KNOWLEDGE_CONTRIBUTION_CLASSIFICATIONS,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    rules = load_dsr_rules(project_config)
    DSR_CLASSIFICATION_RULES_JSON.write_text(json.dumps(rules, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    if not DSR_MIGRATION_LOG_CSV.exists():
        with DSR_MIGRATION_LOG_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["timestamp", "catalogue_id", "stable_id", "field", "old_value", "new_value"])
    if not DSR_MANUAL_REVIEW_CSV.exists():
        with DSR_MANUAL_REVIEW_CSV.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(["catalogue_id", "category", "detail"])

    print(f"DSR export: {len(records)} records -> {display_path(DSR_OUTPUT_DIR)}/ "
          f"(research_catalogue.csv/json/md/sqlite, catalogue_relationships.csv, catalogue_schema.json, "
          f"catalogue_controlled_vocabulary.json, catalogue_migration_log.csv, catalogue_manual_review.csv, "
          f"catalogue_classification_rules.json)")


def _dsr_json_schema() -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "DSR catalogue record",
        "type": "object",
        "required": ["catalogue_id", "stable_id", "version", "class_code", "sha256"],
        "properties": {
            "catalogue_id": {"type": "string", "pattern": r"^[A-Z0-9]+-[A-Z]{3}-[A-Z0-9]{2,3}-\d+-V\d+\.\d+$"},
            "stable_id": {"type": "string", "pattern": r"^[A-Z0-9]+-[A-Z]{3}-[A-Z0-9]{2,3}-\d+$"},
            "version": {"type": "string", "pattern": r"^V\d+\.\d+$"},
            "class_code": {"type": "string", "enum": MAIN_CLASSES + [REQUIRES_REVIEW]},
            "subtype_code": {"type": "string"},
            "dsr_artefact_type": {"type": "string", "enum": DSR_ARTEFACT_CLASSIFICATIONS},
            "knowledge_contribution": {"type": "string", "enum": KNOWLEDGE_CONTRIBUTION_CLASSIFICATIONS},
            "confidence_status": {"type": "string", "enum": ["Confident", REQUIRES_REVIEW]},
            "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        },
    }


# --------------------------------------------------------------------------
# update-references: mechanical, marker-driven only. Never infers which
# prose sentence "means" a given artefact - that would be inventing an
# evidentiary link the anti-hallucination policy for this repo's research
# forbids. Only replaces an author-inserted {{dsr-ref:KEY}} token, where KEY
# is a stable_id or a source-relative path already in the DSR catalogue.
# No-ops entirely unless project_config.json -> dsr_reference_roots is set.
# --------------------------------------------------------------------------

DSR_REF_TOKEN_RE = re.compile(r"\{\{dsr-ref:([^}]+)\}\}")


def _format_reference(row: sqlite3.Row) -> str:
    title = row["title"] or row["file_name"]
    if row["class_code"] == "IMG":
        return f"Source: {row['catalogue_id']}."
    return f"{title} ({row['catalogue_id']})"


def cmd_update_references(project_config: dict, env: dict, dry_run: bool, apply: bool) -> None:
    if dry_run == apply:
        raise SystemExit("update-references --dsr requires exactly one of --dry-run or --apply")
    roots = project_config.get("dsr_reference_roots", [])
    if not roots:
        print("No dsr_reference_roots configured in instance/project_config.json - nothing to do. "
              "Add paths there (and insert {{dsr-ref:<stable_id or relative_path>}} tokens in your docs) to use this command.")
        return
    if not DSR_DB_PATH.exists():
        print(f"{display_path(DSR_DB_PATH)} does not exist yet - run `scan --dsr --apply` first.")
        return

    conn = get_dsr_db()
    rows = conn.execute("SELECT * FROM dsr_catalogue").fetchall()
    conn.close()
    by_stable_id = {row["stable_id"]: row for row in rows}
    by_relative_path = {row["relative_path"]: row for row in rows}

    changed_files = []
    unresolved_keys = []
    for root_str in roots:
        root = Path(root_str)
        if not root.exists():
            print(f"WARNING: dsr_reference_roots entry does not exist, skipping: {root}")
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in (".md", ".txt"):
                continue
            text = path.read_text(encoding="utf-8")

            def _sub(match: re.Match) -> str:
                key = match.group(1).strip()
                row = by_stable_id.get(key) or by_relative_path.get(key)
                if row is None:
                    unresolved_keys.append((str(path), key))
                    return match.group(0)
                return _format_reference(row)

            new_text = DSR_REF_TOKEN_RE.sub(_sub, text)
            if new_text != text:
                changed_files.append(str(path))
                if apply:
                    path.write_text(new_text, encoding="utf-8")

    verb = "Updated" if apply else "Would update"
    print(f"update-references --dsr ({'--apply' if apply else '--dry-run'}): {verb} {len(changed_files)} file(s).")
    for f in changed_files:
        print(f"  {f}")
    if unresolved_keys:
        print(f"{len(unresolved_keys)} unresolved {{{{dsr-ref:...}}}} token(s) left untouched (no matching catalogue entry):")
        for f, key in unresolved_keys[:20]:
            print(f"  {f}: {key}")
