#!/usr/bin/env python3
"""Tests for dublin_core_catalogue.py (the --dublin-core mode).

Stdlib unittest only. Disposable tempdirs and monkeypatched module-level
DB/output-path constants throughout - never reads or writes this checkout's
real instance/.

Run with:
    python3 -m unittest discover tests
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from catalogues import dublin_core_catalogue as dc  # noqa: E402


class ClassifyTypeTests(unittest.TestCase):
    def test_docx_is_text(self):
        self.assertEqual(dc.classify_type(Path("report.docx")), "Text")

    def test_csv_is_dataset(self):
        self.assertEqual(dc.classify_type(Path("data.csv")), "Dataset")

    def test_png_is_still_image(self):
        self.assertEqual(dc.classify_type(Path("figure.png")), "StillImage")

    def test_py_is_software(self):
        self.assertEqual(dc.classify_type(Path("script.py")), "Software")

    def test_unmapped_extension_is_unknown(self):
        self.assertEqual(dc.classify_type(Path("weird.xyz123")), dc.UNKNOWN)


class BuildRecordTests(unittest.TestCase):
    def test_identifier_is_content_addressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            record = dc.build_record(f, root, 1)
            expected_hash = dc.common.sha256_file(f)
            self.assertEqual(record["dc_identifier"], f"urn:sha256:{expected_hash}")

    def test_title_defaults_to_filename_stem(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "my-report.txt"
            f.write_text("hello", encoding="utf-8")
            record = dc.build_record(f, root, 1)
            self.assertEqual(record["dc_title"], "my-report")

    def test_unresolved_fields_default_to_unknown_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            record = dc.build_record(f, root, 1)
            self.assertEqual(record["dc_creator"], dc.UNKNOWN)
            self.assertEqual(record["dc_description"], dc.UNKNOWN)
            self.assertEqual(record["dc_rights"], dc.UNKNOWN)
            self.assertEqual(record["dc_subject"], [])

    def test_extent_is_file_size_in_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello world", encoding="utf-8")
            record = dc.build_record(f, root, 1)
            self.assertEqual(record["dcterms_extent"], f"{len('hello world')} bytes")

    def test_sidecar_overrides_defaults_without_inventing_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            sidecar = f.with_name(f.name + ".dcmeta.json")
            sidecar.write_text(json.dumps({
                "dc_creator": "Jane Doe",
                "dc_rights": "CC-BY-4.0",
            }), encoding="utf-8")
            record = dc.build_record(f, root, 1)
            self.assertEqual(record["dc_creator"], "Jane Doe")
            self.assertEqual(record["dc_rights"], "CC-BY-4.0")
            self.assertEqual(record["dc_description"], dc.UNKNOWN)
            self.assertEqual(record["explicit_metadata_applied"], 1)


class ScanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source_root = self.tmp / "source"
        self.source_root.mkdir(parents=True)
        (self.source_root / "report.docx").write_text("report body", encoding="utf-8")
        (self.source_root / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (self.source_root / "node_modules").mkdir()
        (self.source_root / "node_modules" / "index.js").write_text("skip", encoding="utf-8")

        self._orig_db_path = dc.DB_PATH
        self._orig_output_dir = dc.OUTPUT_DIR
        self._orig_paths = {
            name: getattr(dc, name)
            for name in ("CATALOGUE_CSV", "CATALOGUE_JSON", "CATALOGUE_XML", "SCHEMA_JSON",
                         "MANUAL_REVIEW_CSV", "MIGRATION_LOG_CSV")
        }
        dc.DB_PATH = self.tmp / "catalogue_dublin_core.db"
        dc.OUTPUT_DIR = self.tmp / "dc_out"
        dc.CATALOGUE_CSV = dc.OUTPUT_DIR / "dublin_core_catalogue.csv"
        dc.CATALOGUE_JSON = dc.OUTPUT_DIR / "dublin_core_catalogue.json"
        dc.CATALOGUE_XML = dc.OUTPUT_DIR / "dublin_core_catalogue.xml"
        dc.SCHEMA_JSON = dc.OUTPUT_DIR / "catalogue_schema.json"
        dc.MANUAL_REVIEW_CSV = dc.OUTPUT_DIR / "catalogue_manual_review.csv"
        dc.MIGRATION_LOG_CSV = dc.OUTPUT_DIR / "catalogue_migration_log.csv"

        self.env = {"SOURCE_DATA_ROOTS": str(self.source_root)}
        self.project_config = {"project_id": "test_project"}

    def tearDown(self):
        dc.DB_PATH = self._orig_db_path
        dc.OUTPUT_DIR = self._orig_output_dir
        for name, value in self._orig_paths.items():
            setattr(dc, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_does_not_create_real_db(self):
        dc.cmd_scan(self.project_config, self.env, dry_run=True, apply=False)
        self.assertFalse(dc.DB_PATH.exists())

    def test_apply_scan_catalogues_two_files_and_skips_node_modules(self):
        dc.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = dc.get_db()
        rows = conn.execute("SELECT * FROM dublin_core_catalogue").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        by_name = {r["file_name"]: r for r in rows}
        self.assertEqual(by_name["report.docx"]["dc_type"], "Text")
        self.assertEqual(by_name["data.csv"]["dc_type"], "Dataset")

    def test_scan_is_idempotent(self):
        dc.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = dc.get_db()
        first_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM dublin_core_catalogue").fetchall()}
        conn.close()

        dc.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = dc.get_db()
        second_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM dublin_core_catalogue").fetchall()}
        conn.close()
        self.assertEqual(first_ids, second_ids)

    def test_export_writes_all_required_outputs(self):
        dc.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        dc.cmd_export(self.project_config, self.env)
        for path in (dc.CATALOGUE_CSV, dc.CATALOGUE_JSON, dc.CATALOGUE_XML, dc.SCHEMA_JSON,
                     dc.MANUAL_REVIEW_CSV, dc.MIGRATION_LOG_CSV):
            self.assertTrue(path.exists(), f"missing output: {path}")
        xml_text = dc.CATALOGUE_XML.read_text(encoding="utf-8")
        self.assertIn("<dc:title>", xml_text)
        self.assertIn("<dc:identifier>urn:sha256:", xml_text)

    def test_validate_flags_unresolved_recommended_elements(self):
        dc.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        dc.cmd_validate(self.project_config, self.env)
        review_rows = dc.MANUAL_REVIEW_CSV.read_text(encoding="utf-8")
        self.assertIn("unresolved_dc_creator", review_rows)


if __name__ == "__main__":
    unittest.main()
