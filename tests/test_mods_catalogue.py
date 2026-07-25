#!/usr/bin/env python3
"""Tests for mods_catalogue.py (the --mods mode).

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

import mods_catalogue as mods  # noqa: E402


class ClassifyTypeOfResourceTests(unittest.TestCase):
    def test_docx_is_text(self):
        self.assertEqual(mods.classify_type_of_resource(Path("report.docx")), "text")

    def test_png_is_still_image(self):
        self.assertEqual(mods.classify_type_of_resource(Path("figure.png")), "still image")

    def test_py_is_software_multimedia(self):
        self.assertEqual(mods.classify_type_of_resource(Path("script.py")), "software, multimedia")

    def test_unmapped_extension_is_mixed_material(self):
        self.assertEqual(mods.classify_type_of_resource(Path("weird.xyz123")), "mixed material")


class BuildRecordTests(unittest.TestCase):
    def test_digital_origin_omitted_by_default_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            record = mods.build_record(f, root, 1, {})
            self.assertIsNone(record["digital_origin"])

    def test_creator_name_from_configured_researcher_guards_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            record = mods.build_record(f, root, 1, {"researcher": "REPLACE_ME"})
            self.assertEqual(record["creator_name"], mods.UNKNOWN)
            record2 = mods.build_record(f, root, 1, {"researcher": "Pedro Veloso"})
            self.assertEqual(record2["creator_name"], "Pedro Veloso")

    def test_identifier_is_content_addressed_local_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            record = mods.build_record(f, root, 1, {})
            expected_hash = mods.common.sha256_file(f)
            self.assertEqual(record["identifier_type"], "local")
            self.assertEqual(record["identifier_value"], f"urn:mods:sha256:{expected_hash}")

    def test_unresolved_fields_default_to_unknown_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            record = mods.build_record(f, root, 1, {})
            self.assertEqual(record["genre"], mods.UNKNOWN)
            self.assertEqual(record["abstract"], mods.UNKNOWN)
            self.assertEqual(record["subjects"], [])


class ScanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source_root = self.tmp / "source"
        self.source_root.mkdir(parents=True)
        (self.source_root / "report.docx").write_text("body", encoding="utf-8")
        (self.source_root / "figure.png").write_text("fake image bytes", encoding="utf-8")
        (self.source_root / "__pycache__").mkdir()
        (self.source_root / "__pycache__" / "x.pyc").write_text("skip", encoding="utf-8")

        self._orig_db_path = mods.DB_PATH
        self._orig_output_dir = mods.OUTPUT_DIR
        self._orig_paths = {
            name: getattr(mods, name)
            for name in ("CATALOGUE_CSV", "CATALOGUE_JSON", "CATALOGUE_XML_DIR", "SCHEMA_JSON",
                         "MANUAL_REVIEW_CSV", "MIGRATION_LOG_CSV")
        }
        mods.DB_PATH = self.tmp / "catalogue_mods.db"
        mods.OUTPUT_DIR = self.tmp / "mods_out"
        mods.CATALOGUE_CSV = mods.OUTPUT_DIR / "mods_catalogue.csv"
        mods.CATALOGUE_JSON = mods.OUTPUT_DIR / "mods_catalogue.json"
        mods.CATALOGUE_XML_DIR = mods.OUTPUT_DIR / "mods_xml"
        mods.SCHEMA_JSON = mods.OUTPUT_DIR / "catalogue_schema.json"
        mods.MANUAL_REVIEW_CSV = mods.OUTPUT_DIR / "catalogue_manual_review.csv"
        mods.MIGRATION_LOG_CSV = mods.OUTPUT_DIR / "catalogue_migration_log.csv"

        self.env = {"SOURCE_DATA_ROOTS": str(self.source_root)}
        self.project_config = {"project_id": "test_project", "researcher": "Pedro Veloso", "institution": "QUT"}

    def tearDown(self):
        mods.DB_PATH = self._orig_db_path
        mods.OUTPUT_DIR = self._orig_output_dir
        for name, value in self._orig_paths.items():
            setattr(mods, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_does_not_create_real_db(self):
        mods.cmd_scan(self.project_config, self.env, dry_run=True, apply=False)
        self.assertFalse(mods.DB_PATH.exists())

    def test_apply_scan_catalogues_two_files(self):
        mods.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = mods.get_db()
        rows = conn.execute("SELECT * FROM mods_catalogue").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        by_name = {r["file_name"]: r for r in rows}
        self.assertEqual(by_name["report.docx"]["type_of_resource"], "text")
        self.assertEqual(by_name["figure.png"]["type_of_resource"], "still image")

    def test_scan_is_idempotent(self):
        mods.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = mods.get_db()
        first_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM mods_catalogue").fetchall()}
        conn.close()
        mods.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = mods.get_db()
        second_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM mods_catalogue").fetchall()}
        conn.close()
        self.assertEqual(first_ids, second_ids)

    def test_export_writes_valid_mods_xml_without_digital_origin(self):
        mods.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        mods.cmd_export(self.project_config, self.env)
        xml_files = list(mods.CATALOGUE_XML_DIR.glob("*.xml"))
        self.assertEqual(len(xml_files), 2)
        combined = "\n".join(f.read_text(encoding="utf-8") for f in xml_files)
        self.assertIn('xmlns="http://www.loc.gov/mods/v3"', combined)
        self.assertIn("<recordContentSource>", combined)
        self.assertNotIn("<digitalOrigin>", combined)
        self.assertIn("Pedro Veloso", combined)
        self.assertIn("QUT", combined)

    def test_validate_flags_unresolved_genre(self):
        mods.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        mods.cmd_validate(self.project_config, self.env)
        review_rows = mods.MANUAL_REVIEW_CSV.read_text(encoding="utf-8")
        self.assertIn("unresolved_genre", review_rows)


if __name__ == "__main__":
    unittest.main()
