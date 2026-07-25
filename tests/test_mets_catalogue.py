#!/usr/bin/env python3
"""Tests for mets_catalogue.py (the --mets mode).

Stdlib unittest only. Disposable tempdirs and monkeypatched module-level
DB/output-path constants throughout - never reads or writes this checkout's
real instance/.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mets_catalogue as mets  # noqa: E402


class StructTreeTests(unittest.TestCase):
    def test_flat_files_grouped_at_root(self):
        records = [
            {"relative_path": "a.txt", "file_name": "a.txt", "file_id": "FILE_00001", "label": "a.txt"},
            {"relative_path": "b.txt", "file_name": "b.txt", "file_id": "FILE_00002", "label": "b.txt"},
        ]
        tree = mets._build_struct_tree(records)
        self.assertEqual(len(tree.get("__files__", [])), 2)
        self.assertEqual(len([k for k in tree if k != "__files__"]), 0)

    def test_nested_files_grouped_by_directory(self):
        records = [
            {"relative_path": "docs/report.pdf", "file_name": "report.pdf", "file_id": "FILE_00001", "label": "report.pdf"},
            {"relative_path": "docs/sub/appendix.pdf", "file_name": "appendix.pdf", "file_id": "FILE_00002", "label": "appendix.pdf"},
            {"relative_path": "data.csv", "file_name": "data.csv", "file_id": "FILE_00003", "label": "data.csv"},
        ]
        tree = mets._build_struct_tree(records)
        self.assertEqual(len(tree.get("__files__", [])), 1)  # data.csv at root
        self.assertIn("docs", tree)
        self.assertEqual(len(tree["docs"].get("__files__", [])), 1)  # report.pdf
        self.assertIn("sub", tree["docs"])
        self.assertEqual(len(tree["docs"]["sub"].get("__files__", [])), 1)  # appendix.pdf

    def test_render_struct_div_produces_nested_fptr(self):
        records = [{"relative_path": "docs/report.pdf", "file_name": "report.pdf", "file_id": "FILE_00001", "label": "report.pdf"}]
        tree = mets._build_struct_tree(records)
        lines = mets._render_struct_div(tree, "root", "")
        text = "\n".join(lines)
        self.assertIn('<div TYPE="directory" LABEL="docs">', text)
        self.assertIn('<fptr FILEID="FILE_00001"/>', text)


class BuildRecordTests(unittest.TestCase):
    def test_file_id_is_valid_xml_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            record = mets.build_record(f, root, 1)
            self.assertEqual(record["file_id"], "FILE_00001")

    def test_checksum_type_is_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            record = mets.build_record(f, root, 1)
            self.assertEqual(record["checksum_type"], "SHA-256")
            self.assertEqual(record["checksum"], mets.common.sha256_file(f))

    def test_description_defaults_to_unknown_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            record = mets.build_record(f, root, 1)
            self.assertEqual(record["description"], mets.UNKNOWN)


class ScanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source_root = self.tmp / "source"
        (self.source_root / "docs").mkdir(parents=True)
        (self.source_root / "docs" / "report.pdf").write_text("body", encoding="utf-8")
        (self.source_root / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (self.source_root / "__pycache__").mkdir()
        (self.source_root / "__pycache__" / "x.pyc").write_text("skip", encoding="utf-8")

        self._orig_db_path = mets.DB_PATH
        self._orig_output_dir = mets.OUTPUT_DIR
        self._orig_paths = {
            name: getattr(mets, name)
            for name in ("CATALOGUE_CSV", "CATALOGUE_JSON", "METS_XML_DIR", "SCHEMA_JSON",
                         "MANUAL_REVIEW_CSV", "MIGRATION_LOG_CSV")
        }
        mets.DB_PATH = self.tmp / "catalogue_mets.db"
        mets.OUTPUT_DIR = self.tmp / "mets_out"
        mets.CATALOGUE_CSV = mets.OUTPUT_DIR / "mets_catalogue.csv"
        mets.CATALOGUE_JSON = mets.OUTPUT_DIR / "mets_catalogue.json"
        mets.METS_XML_DIR = mets.OUTPUT_DIR / "packages"
        mets.SCHEMA_JSON = mets.OUTPUT_DIR / "catalogue_schema.json"
        mets.MANUAL_REVIEW_CSV = mets.OUTPUT_DIR / "catalogue_manual_review.csv"
        mets.MIGRATION_LOG_CSV = mets.OUTPUT_DIR / "catalogue_migration_log.csv"

        self.env = {"SOURCE_DATA_ROOTS": str(self.source_root)}
        self.project_config = {"project_id": "test_project"}

    def tearDown(self):
        mets.DB_PATH = self._orig_db_path
        mets.OUTPUT_DIR = self._orig_output_dir
        for name, value in self._orig_paths.items():
            setattr(mets, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_does_not_create_real_db(self):
        mets.cmd_scan(self.project_config, self.env, dry_run=True, apply=False)
        self.assertFalse(mets.DB_PATH.exists())

    def test_apply_scan_catalogues_two_files_and_skips_pycache(self):
        mets.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = mets.get_db()
        rows = conn.execute("SELECT * FROM mets_catalogue").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)

    def test_scan_is_idempotent(self):
        mets.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = mets.get_db()
        first_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM mets_catalogue").fetchall()}
        conn.close()
        mets.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = mets.get_db()
        second_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM mets_catalogue").fetchall()}
        conn.close()
        self.assertEqual(first_ids, second_ids)

    def test_export_writes_mets_xml_with_nested_structmap(self):
        mets.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        mets.cmd_export(self.project_config, self.env)
        package_dirs = list(mets.METS_XML_DIR.iterdir())
        self.assertEqual(len(package_dirs), 1)
        mets_xml_path = package_dirs[0] / "mets.xml"
        self.assertTrue(mets_xml_path.exists())
        xml_text = mets_xml_path.read_text(encoding="utf-8")
        self.assertIn(f'xmlns="{mets.METS_NAMESPACE}"', xml_text)
        self.assertIn("<fileSec>", xml_text)
        self.assertIn('<structMap TYPE="physical">', xml_text)
        self.assertIn('<div TYPE="directory" LABEL="docs">', xml_text)
        self.assertIn("CHECKSUMTYPE=\"SHA-256\"", xml_text)

    def test_validate_passes_with_no_structural_issues(self):
        mets.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        mets.cmd_validate(self.project_config, self.env)  # should not raise


if __name__ == "__main__":
    unittest.main()
