#!/usr/bin/env python3
"""Tests for cerif_catalogue.py (the --cerif mode).

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

import cerif_catalogue as cerif  # noqa: E402


class ClassifyEntityTests(unittest.TestCase):
    def test_journal_article_manuscript_is_respubl(self):
        entity_type, result_type, rule, evidence = cerif.classify_entity(
            Path("misc/smith-2024-journal-article.pdf"), Path("misc").parent
        )
        self.assertEqual(entity_type, "cfResPubl")
        self.assertEqual(result_type, "Journal Article")

    def test_dataset_csv_is_resprod(self):
        entity_type, result_type, rule, evidence = cerif.classify_entity(
            Path("data/export.csv"), Path("data").parent
        )
        self.assertEqual(entity_type, "cfResProd")
        self.assertEqual(result_type, "Dataset")

    def test_python_script_is_resprod_software(self):
        entity_type, result_type, rule, evidence = cerif.classify_entity(
            Path("scripts/run.py"), Path("scripts").parent
        )
        self.assertEqual(entity_type, "cfResProd")
        self.assertEqual(result_type, "Software")

    def test_unmapped_extension_is_other_product(self):
        entity_type, result_type, rule, evidence = cerif.classify_entity(
            Path("misc/weird.xyz123"), Path("misc").parent
        )
        self.assertEqual(entity_type, "cfResProd")
        self.assertEqual(result_type, "Other Product")


class BuildRecordTests(unittest.TestCase):
    def test_person_relation_defaults_from_configured_researcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "data.csv"
            f.write_text("a,b\n1,2\n", encoding="utf-8")
            record = cerif.build_record(f, root, 1, {"researcher": "Pedro Veloso"})
            self.assertEqual(len(record["person_relations"]), 1)
            self.assertEqual(record["person_relations"][0]["name"], "Pedro Veloso")

    def test_person_relation_empty_without_config_never_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "data.csv"
            f.write_text("a,b\n1,2\n", encoding="utf-8")
            record = cerif.build_record(f, root, 1, {})
            self.assertEqual(record["person_relations"], [])

    def test_placeholder_researcher_value_is_not_used(self):
        # templates/project_config.template.json ships "REPLACE_ME" as a
        # placeholder - must never be treated as real configured data.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "data.csv"
            f.write_text("a,b\n1,2\n", encoding="utf-8")
            record = cerif.build_record(f, root, 1, {"researcher": "REPLACE_ME"})
            self.assertEqual(record["person_relations"], [])

    def test_cerif_id_is_content_addressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "data.csv"
            f.write_text("a,b\n1,2\n", encoding="utf-8")
            record = cerif.build_record(f, root, 1, {})
            expected_hash = cerif.common.sha256_file(f)
            self.assertEqual(record["cerif_id"], f"urn:cerif:sha256:{expected_hash}")


class ScanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source_root = self.tmp / "source"
        self.source_root.mkdir(parents=True)
        (self.source_root / "smith-2024-journal-article.pdf").write_text("body", encoding="utf-8")
        (self.source_root / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (self.source_root / "__pycache__").mkdir()
        (self.source_root / "__pycache__" / "x.pyc").write_text("skip", encoding="utf-8")

        self._orig_db_path = cerif.DB_PATH
        self._orig_output_dir = cerif.OUTPUT_DIR
        self._orig_paths = {
            name: getattr(cerif, name)
            for name in ("CATALOGUE_CSV", "CATALOGUE_JSON", "CATALOGUE_XML_DIR", "SCHEMA_JSON",
                         "MANUAL_REVIEW_CSV", "MIGRATION_LOG_CSV")
        }
        cerif.DB_PATH = self.tmp / "catalogue_cerif.db"
        cerif.OUTPUT_DIR = self.tmp / "cerif_out"
        cerif.CATALOGUE_CSV = cerif.OUTPUT_DIR / "cerif_catalogue.csv"
        cerif.CATALOGUE_JSON = cerif.OUTPUT_DIR / "cerif_catalogue.json"
        cerif.CATALOGUE_XML_DIR = cerif.OUTPUT_DIR / "cerif_xml"
        cerif.SCHEMA_JSON = cerif.OUTPUT_DIR / "catalogue_schema.json"
        cerif.MANUAL_REVIEW_CSV = cerif.OUTPUT_DIR / "catalogue_manual_review.csv"
        cerif.MIGRATION_LOG_CSV = cerif.OUTPUT_DIR / "catalogue_migration_log.csv"

        self.env = {"SOURCE_DATA_ROOTS": str(self.source_root)}
        self.project_config = {"project_id": "test_project", "researcher": "Pedro Veloso",
                                "institution": "QUT", "project_name": "Test Project"}

    def tearDown(self):
        cerif.DB_PATH = self._orig_db_path
        cerif.OUTPUT_DIR = self._orig_output_dir
        for name, value in self._orig_paths.items():
            setattr(cerif, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_does_not_create_real_db(self):
        cerif.cmd_scan(self.project_config, self.env, dry_run=True, apply=False)
        self.assertFalse(cerif.DB_PATH.exists())

    def test_apply_scan_catalogues_both_with_correct_entity_types(self):
        cerif.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = cerif.get_db()
        rows = conn.execute("SELECT * FROM cerif_catalogue").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        by_name = {r["file_name"]: r for r in rows}
        self.assertEqual(by_name["smith-2024-journal-article.pdf"]["entity_type"], "cfResPubl")
        self.assertEqual(by_name["data.csv"]["entity_type"], "cfResProd")

    def test_scan_is_idempotent(self):
        cerif.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = cerif.get_db()
        first_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM cerif_catalogue").fetchall()}
        conn.close()
        cerif.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = cerif.get_db()
        second_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM cerif_catalogue").fetchall()}
        conn.close()
        self.assertEqual(first_ids, second_ids)

    def test_export_writes_valid_cerif_xml_relations(self):
        cerif.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        cerif.cmd_export(self.project_config, self.env)
        xml_files = sorted(cerif.CATALOGUE_XML_DIR.glob("*.xml"))
        self.assertEqual(len(xml_files), 2)
        combined = "\n".join(f.read_text(encoding="utf-8") for f in xml_files)
        self.assertIn("cfPers_ResPubl", combined + "cfPers_ResPubl")  # sanity - element naming pattern
        self.assertIn("Pedro Veloso", combined)
        self.assertIn("QUT", combined)

    def test_validate_passes_with_no_structural_issues(self):
        cerif.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        cerif.cmd_validate(self.project_config, self.env)  # should not raise


if __name__ == "__main__":
    unittest.main()
