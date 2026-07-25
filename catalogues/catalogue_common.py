#!/usr/bin/env python3
"""Shared helpers for the standard-specific catalogue modules (dublin_core_
catalogue.py, datacite_catalogue.py, crossref_catalogue.py, cerif_catalogue.py,
ro_crate_catalogue.py, dcat_catalogue.py, mods_catalogue.py, marc21_catalogue.py,
mets_catalogue.py, premis_catalogue.py).

Each of those modules implements one external metadata/cataloguing standard as
a fully isolated pipeline - its own SQLite database, its own output directory,
gated by its own CLI flag - following the same isolation pattern established
by dsr_catalogue.py. This module holds only the generic, standard-agnostic
plumbing every one of them needs (file walking, exclusion rules, hashing,
dry-run tempdir scaffolding); dsr_catalogue.py predates this module and keeps
its own copies rather than being refactored to depend on it, so already-shipped,
tested behaviour is never put at risk by a later module's changes.

Nothing here ever opens instance/catalogue.db, instance/catalogue_dsr.db, or
any other standard's database - callers pass in their own db_path/output_dir.
"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tempfile
from pathlib import Path

# catalogue_common.py lives in catalogues/, one level below the project
# root where instance/ actually is - parent.parent, not parent.
ROOT_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = ROOT_DIR / "instance"
CATALOGUE_DIR = INSTANCE_DIR / "catalogued_files"


def display_path(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT_DIR))
    except ValueError:
        return str(p)


# --------------------------------------------------------------------------
# Exclusion rules (Step 1 in every module's decision order) - generated or
# temporary files that should never be catalogued unless explicitly configured
# otherwise. Shared verbatim across every standard - none of them define their
# own; the underlying source tree doesn't change per output profile.
# --------------------------------------------------------------------------

EXCLUDED_DIR_NAMES = {
    ".git", ".idea", ".vscode", "node_modules", "__pycache__", ".pytest_cache",
    "venv", ".venv", "env", "build", "dist", ".mypy_cache", ".tox",
}
EXCLUDED_FILENAME_PREFIXES = ("~$",)
EXCLUDED_FILENAMES = {".DS_Store"}
EXCLUDED_SUFFIXES = (".tmp", ".bak", ".swp")


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


def iter_source_files(source_root: Path):
    for path in source_root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if is_excluded(path, source_root):
            continue
        yield path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def get_sqlite_db(db_path: Path, schema_sql: str) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(schema_sql)
    conn.commit()
    return conn


def dry_run_db_copy(real_db_path: Path, prefix: str) -> tuple[Path, Path]:
    """Copies real_db_path (if it exists) into a fresh tempdir so a --dry-run
    pass can run the real code path against a disposable copy, then be
    discarded - the real database is never opened in dry-run mode. Returns
    (tmp_dir, tmp_db_path); caller is responsible for cleanup (or leaves it
    for inspection, matching cmd_all's existing --dry-run convention)."""
    tmp_dir = Path(tempfile.mkdtemp(prefix=prefix))
    tmp_db_path = tmp_dir / real_db_path.name
    if real_db_path.exists():
        shutil.copy2(real_db_path, tmp_db_path)
    return tmp_dir, tmp_db_path


def source_roots_from_env(env: dict) -> list[Path]:
    roots = []
    for root_str in env.get("SOURCE_DATA_ROOTS", "").split(","):
        root_str = root_str.strip()
        if root_str:
            roots.append(Path(root_str))
    return roots
