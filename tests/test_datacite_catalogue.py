#!/usr/bin/env python3
"""Tests for datacite_catalogue.py (the --datacite mode).

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

import datacite_catalogue as datacite  # noqa: E402


class ClassifyResourceTypeTests(unittest.TestCase):
    def test_csv_is_dataset(self):
        self.assertEqual(datacite.classify_resource_type(Path("data.csv")), "Dataset")

    def test_py_is_software(self):
        self.assertEqual(datacite.classify_resource_type(Path("script.py")), "Software")

    def test_ipynb_is_computational_notebook(self):
        self.assertEqual(datacite.classify_resource_type(Path("analysis.ipynb")), "ComputationalNotebook")

    def test_unmapped_extension_is_other(self):
        self.assertEqual(datacite.classify_resource_type(Path("weird.xyz123")), "Other")


class BuildRecordTests(unittest.TestCase):
    def test_identifier_defaults_to_local_content_addressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            record = datacite.build_record(f, root, 1, {})
            self.assertEqual(record["identifier_type"], "Local")
            expected_hash = datacite.common.sha256_file(f)
            self.assertEqual(record["identifier_value"], expected_hash)

    def test_never_fabricates_a_doi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            record = datacite.build_record(f, root, 1, {})
            self.assertNotEqual(record["identifier_type"], "DOI")

    def test_publisher_uses_project_config_institution_when_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            record = datacite.build_record(f, root, 1, {"institution": "QUT"})
            self.assertEqual(record["publisher"], "QUT")

    def test_publisher_defaults_unknown_without_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            record = datacite.build_record(f, root, 1, {})
            self.assertEqual(record["publisher"], datacite.UNKNOWN)

    def test_unresolved_fields_default_to_unknown_or_empty_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            record = datacite.build_record(f, root, 1, {})
            self.assertEqual(record["creators"], [])
            self.assertEqual(record["subjects"], [])
            self.assertEqual(record["rights"], datacite.UNKNOWN)
            self.assertIsNone(record["version"])

    def test_sidecar_can_supply_real_doi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            sidecar = f.with_name(f.name + ".datacite.json")
            sidecar.write_text(json.dumps({
                "identifier_type": "DOI",
                "identifier_value": "10.5281/zenodo.1234567",
            }), encoding="utf-8")
            record = datacite.build_record(f, root, 1, {})
            self.assertEqual(record["identifier_type"], "DOI")
            self.assertEqual(record["identifier_value"], "10.5281/zenodo.1234567")
            self.assertEqual(record["explicit_metadata_applied"], 1)


class ScanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source_root = self.tmp / "source"
        self.source_root.mkdir(parents=True)
        (self.source_root / "dataset.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (self.source_root / "script.py").write_text("print(1)", encoding="utf-8")
        (self.source_root / "__pycache__").mkdir()
        (self.source_root / "__pycache__" / "x.pyc").write_text("skip", encoding="utf-8")

        self._orig_db_path = datacite.DB_PATH
        self._orig_output_dir = datacite.OUTPUT_DIR
        self._orig_paths = {
            name: getattr(datacite, name)
            for name in ("CATALOGUE_CSV", "CATALOGUE_JSON", "CATALOGUE_XML_DIR", "SCHEMA_JSON",
                         "MANUAL_REVIEW_CSV", "MIGRATION_LOG_CSV")
        }
        datacite.DB_PATH = self.tmp / "catalogue_datacite.db"
        datacite.OUTPUT_DIR = self.tmp / "datacite_out"
        datacite.CATALOGUE_CSV = datacite.OUTPUT_DIR / "datacite_catalogue.csv"
        datacite.CATALOGUE_JSON = datacite.OUTPUT_DIR / "datacite_catalogue.json"
        datacite.CATALOGUE_XML_DIR = datacite.OUTPUT_DIR / "datacite_xml"
        datacite.SCHEMA_JSON = datacite.OUTPUT_DIR / "catalogue_schema.json"
        datacite.MANUAL_REVIEW_CSV = datacite.OUTPUT_DIR / "catalogue_manual_review.csv"
        datacite.MIGRATION_LOG_CSV = datacite.OUTPUT_DIR / "catalogue_migration_log.csv"

        self.env = {"SOURCE_DATA_ROOTS": str(self.source_root)}
        self.project_config = {"project_id": "test_project", "institution": "Test University"}

    def tearDown(self):
        datacite.DB_PATH = self._orig_db_path
        datacite.OUTPUT_DIR = self._orig_output_dir
        for name, value in self._orig_paths.items():
            setattr(datacite, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_does_not_create_real_db(self):
        datacite.cmd_scan(self.project_config, self.env, dry_run=True, apply=False)
        self.assertFalse(datacite.DB_PATH.exists())

    def test_apply_scan_catalogues_two_files_and_skips_pycache(self):
        datacite.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = datacite.get_db()
        rows = conn.execute("SELECT * FROM datacite_catalogue").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        by_name = {r["file_name"]: r for r in rows}
        self.assertEqual(by_name["dataset.csv"]["resource_type_general"], "Dataset")
        self.assertEqual(by_name["script.py"]["resource_type_general"], "Software")
        self.assertEqual(by_name["dataset.csv"]["publisher"], "Test University")

    def test_scan_is_idempotent(self):
        datacite.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = datacite.get_db()
        first_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM datacite_catalogue").fetchall()}
        conn.close()

        datacite.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = datacite.get_db()
        second_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM datacite_catalogue").fetchall()}
        conn.close()
        self.assertEqual(first_ids, second_ids)

    def test_export_writes_all_required_outputs_including_per_record_xml(self):
        datacite.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        datacite.cmd_export(self.project_config, self.env)
        for path in (datacite.CATALOGUE_CSV, datacite.CATALOGUE_JSON, datacite.SCHEMA_JSON,
                     datacite.MANUAL_REVIEW_CSV, datacite.MIGRATION_LOG_CSV):
            self.assertTrue(path.exists(), f"missing output: {path}")
        xml_files = list(datacite.CATALOGUE_XML_DIR.glob("*.xml"))
        self.assertEqual(len(xml_files), 2)
        xml_text = xml_files[0].read_text(encoding="utf-8")
        self.assertIn('xmlns="http://datacite.org/schema/kernel-4"', xml_text)
        self.assertIn("<publisher>Test University</publisher>", xml_text)

    def test_validate_flags_unresolved_publisher_when_no_institution_configured(self):
        datacite.cmd_scan({"project_id": "x"}, self.env, dry_run=False, apply=True)
        datacite.cmd_validate({"project_id": "x"}, self.env)
        review_rows = datacite.MANUAL_REVIEW_CSV.read_text(encoding="utf-8")
        self.assertIn("unresolved_publisher", review_rows)


if __name__ == "__main__":
    unittest.main()
