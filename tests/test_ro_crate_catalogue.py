#!/usr/bin/env python3
"""Tests for ro_crate_catalogue.py (the --ro-crate mode).

Stdlib unittest only. Disposable tempdirs and monkeypatched module-level
DB/output-path constants throughout - never reads or writes this checkout's
real instance/.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from catalogues import ro_crate_catalogue as rocrate  # noqa: E402


class BuildRecordTests(unittest.TestCase):
    def test_ro_crate_id_is_relative_with_dot_slash_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sub = root / "data"
            sub.mkdir()
            f = sub / "note.txt"
            f.write_text("hello", encoding="utf-8")
            record = rocrate.build_record(f, root, 1, {})
            self.assertEqual(record["ro_crate_id"], "./data/note.txt")

    def test_name_defaults_to_filename_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            record = rocrate.build_record(f, root, 1, {})
            self.assertEqual(record["name"], "note.txt")
            self.assertEqual(record["description"], rocrate.UNKNOWN)
            self.assertEqual(record["license"], rocrate.UNKNOWN)

    def test_author_relation_from_configured_researcher_guards_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            record = rocrate.build_record(f, root, 1, {"researcher": "REPLACE_ME"})
            self.assertEqual(record["author_relations"], [])
            record2 = rocrate.build_record(f, root, 1, {"researcher": "Pedro Veloso"})
            self.assertEqual(record2["author_relations"], [{"name": "Pedro Veloso"}])


class ScanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source_root = self.tmp / "source"
        self.source_root.mkdir(parents=True)
        (self.source_root / "note.txt").write_text("hello", encoding="utf-8")
        (self.source_root / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (self.source_root / ".git").mkdir()
        (self.source_root / ".git" / "HEAD").write_text("skip", encoding="utf-8")

        self._orig_db_path = rocrate.DB_PATH
        self._orig_output_dir = rocrate.OUTPUT_DIR
        self._orig_paths = {
            name: getattr(rocrate, name)
            for name in ("CATALOGUE_CSV", "CATALOGUE_JSON", "RO_CRATE_METADATA_DIR", "SCHEMA_JSON",
                         "MANUAL_REVIEW_CSV", "MIGRATION_LOG_CSV")
        }
        rocrate.DB_PATH = self.tmp / "catalogue_ro_crate.db"
        rocrate.OUTPUT_DIR = self.tmp / "ro_crate_out"
        rocrate.CATALOGUE_CSV = rocrate.OUTPUT_DIR / "ro_crate_catalogue.csv"
        rocrate.CATALOGUE_JSON = rocrate.OUTPUT_DIR / "ro_crate_catalogue.json"
        rocrate.RO_CRATE_METADATA_DIR = rocrate.OUTPUT_DIR / "crates"
        rocrate.SCHEMA_JSON = rocrate.OUTPUT_DIR / "catalogue_schema.json"
        rocrate.MANUAL_REVIEW_CSV = rocrate.OUTPUT_DIR / "catalogue_manual_review.csv"
        rocrate.MIGRATION_LOG_CSV = rocrate.OUTPUT_DIR / "catalogue_migration_log.csv"

        self.env = {"SOURCE_DATA_ROOTS": str(self.source_root)}
        self.project_config = {"project_id": "test_project", "researcher": "Pedro Veloso"}

    def tearDown(self):
        rocrate.DB_PATH = self._orig_db_path
        rocrate.OUTPUT_DIR = self._orig_output_dir
        for name, value in self._orig_paths.items():
            setattr(rocrate, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_does_not_create_real_db(self):
        rocrate.cmd_scan(self.project_config, self.env, dry_run=True, apply=False)
        self.assertFalse(rocrate.DB_PATH.exists())

    def test_apply_scan_catalogues_two_files_and_skips_git(self):
        rocrate.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = rocrate.get_db()
        rows = conn.execute("SELECT * FROM ro_crate_catalogue").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)

    def test_scan_is_idempotent(self):
        rocrate.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = rocrate.get_db()
        first_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM ro_crate_catalogue").fetchall()}
        conn.close()
        rocrate.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = rocrate.get_db()
        second_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM ro_crate_catalogue").fetchall()}
        conn.close()
        self.assertEqual(first_ids, second_ids)

    def test_export_writes_valid_ro_crate_metadata_json(self):
        rocrate.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        rocrate.cmd_export(self.project_config, self.env)
        crate_dirs = list(rocrate.RO_CRATE_METADATA_DIR.iterdir())
        self.assertEqual(len(crate_dirs), 1)
        manifest_path = crate_dirs[0] / "ro-crate-metadata.json"
        self.assertTrue(manifest_path.exists())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["@context"], rocrate.RO_CRATE_CONTEXT)
        ids = {entity["@id"] for entity in manifest["@graph"]}
        self.assertIn("./", ids)
        self.assertIn("ro-crate-metadata.json", ids)
        self.assertIn("./note.txt", ids)
        self.assertIn("./data.csv", ids)
        root_dataset = next(e for e in manifest["@graph"] if e["@id"] == "./")
        has_part_ids = {p["@id"] for p in root_dataset["hasPart"]}
        self.assertEqual(has_part_ids, {"./note.txt", "./data.csv"})
        person_entities = [e for e in manifest["@graph"] if e.get("@type") == "Person"]
        self.assertEqual(len(person_entities), 1)
        self.assertEqual(person_entities[0]["name"], "Pedro Veloso")

    def test_validate_passes_with_no_structural_issues(self):
        rocrate.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        rocrate.cmd_validate(self.project_config, self.env)  # should not raise


if __name__ == "__main__":
    unittest.main()
