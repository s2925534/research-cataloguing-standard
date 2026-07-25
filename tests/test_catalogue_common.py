#!/usr/bin/env python3
"""Tests for catalogue_common.py - shared plumbing reused by every
standard-specific catalogue module (dublin_core_catalogue.py, datacite_
catalogue.py, etc). Stdlib unittest only, disposable tempdirs only."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from catalogues import catalogue_common as common  # noqa: E402


class ExclusionTests(unittest.TestCase):
    def test_git_dir_excluded(self):
        root = Path("/proj")
        self.assertTrue(common.is_excluded(root / ".git" / "HEAD", root))

    def test_node_modules_excluded(self):
        root = Path("/proj")
        self.assertTrue(common.is_excluded(root / "node_modules" / "pkg" / "index.js", root))

    def test_normal_file_not_excluded(self):
        root = Path("/proj")
        self.assertFalse(common.is_excluded(root / "docs" / "report.docx", root))


class IterSourceFilesTests(unittest.TestCase):
    def test_skips_excluded_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "keep.txt").write_text("a", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "skip.js").write_text("b", encoding="utf-8")
            found = {p.name for p in common.iter_source_files(root)}
            self.assertEqual(found, {"keep.txt"})


class Sha256Tests(unittest.TestCase):
    def test_known_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.txt"
            path.write_bytes(b"hello")
            import hashlib
            expected = hashlib.sha256(b"hello").hexdigest()
            self.assertEqual(common.sha256_file(path), expected)


class SqliteDbTests(unittest.TestCase):
    def test_creates_db_and_applies_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sub" / "test.db"
            conn = common.get_sqlite_db(db_path, "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY);")
            self.assertTrue(db_path.exists())
            conn.execute("INSERT INTO t (id) VALUES (1)")
            conn.commit()
            row = conn.execute("SELECT id FROM t").fetchone()
            self.assertEqual(row["id"], 1)
            conn.close()


class DryRunDbCopyTests(unittest.TestCase):
    def test_copies_existing_db_into_tempdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            real_db = Path(tmp) / "real.db"
            conn = common.get_sqlite_db(real_db, "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY);")
            conn.execute("INSERT INTO t (id) VALUES (42)")
            conn.commit()
            conn.close()

            tmp_dir, tmp_db_path = common.dry_run_db_copy(real_db, prefix="test_dry_run_")
            self.assertTrue(tmp_db_path.exists())
            self.assertNotEqual(tmp_db_path, real_db)
            check_conn = common.get_sqlite_db(tmp_db_path, "")
            row = check_conn.execute("SELECT id FROM t").fetchone()
            self.assertEqual(row["id"], 42)
            check_conn.close()

    def test_missing_real_db_yields_fresh_tempdir(self):
        nonexistent = Path("/tmp/definitely-does-not-exist-catalogue-common-test.db")
        tmp_dir, tmp_db_path = common.dry_run_db_copy(nonexistent, prefix="test_dry_run_")
        self.assertFalse(tmp_db_path.exists())


class SourceRootsFromEnvTests(unittest.TestCase):
    def test_parses_comma_separated_roots(self):
        roots = common.source_roots_from_env({"SOURCE_DATA_ROOTS": "/a, /b ,/c"})
        self.assertEqual(roots, [Path("/a"), Path("/b"), Path("/c")])

    def test_missing_key_yields_empty_list(self):
        self.assertEqual(common.source_roots_from_env({}), [])


if __name__ == "__main__":
    unittest.main()
