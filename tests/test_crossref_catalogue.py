#!/usr/bin/env python3
"""Tests for crossref_catalogue.py (the --crossref mode).

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

from catalogues import crossref_catalogue as crossref  # noqa: E402


class ApplicabilityTests(unittest.TestCase):
    def test_ordinary_dataset_is_not_applicable(self):
        applicable, work_type, evidence = crossref.determine_applicability(
            Path("data/export.csv"), Path("data").parent
        )
        self.assertFalse(applicable)

    def test_filename_with_journal_article_token_is_applicable(self):
        applicable, work_type, evidence = crossref.determine_applicability(
            Path("misc/smith-2024-journal-article-draft.pdf"), Path("misc").parent
        )
        self.assertTrue(applicable)
        self.assertEqual(work_type, "journal-article")

    def test_manuscript_dir_with_pdf_is_applicable_but_work_type_unresolved(self):
        applicable, work_type, evidence = crossref.determine_applicability(
            Path("publications/untitled123.pdf"), Path("publications").parent
        )
        self.assertTrue(applicable)
        self.assertIsNone(work_type)

    def test_manuscript_dir_with_csv_is_not_applicable(self):
        # A directory marker alone isn't enough without a manuscript-like
        # extension - a CSV sitting in /publications/ isn't the manuscript itself.
        applicable, work_type, evidence = crossref.determine_applicability(
            Path("publications/supplementary-data.csv"), Path("publications").parent
        )
        self.assertFalse(applicable)


class BuildRecordTests(unittest.TestCase):
    def test_non_applicable_file_gets_not_applicable_publication_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "data.csv"
            f.write_text("a,b\n1,2\n", encoding="utf-8")
            record = crossref.build_record(f, root, 1)
            self.assertEqual(record["crossref_applicable"], 0)
            self.assertEqual(record["publication_type"], crossref.NOT_APPLICABLE)
            self.assertEqual(record["confidence_status"], crossref.NOT_APPLICABLE)

    def test_never_fabricates_a_doi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "journal-article-draft.pdf"
            f.write_text("manuscript body", encoding="utf-8")
            record = crossref.build_record(f, root, 1)
            self.assertIsNone(record["doi"])
            self.assertEqual(record["doi_status"], "unregistered_local")

    def test_sidecar_can_supply_real_doi_and_contributors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "manuscript.pdf"
            f.write_text("body", encoding="utf-8")
            sidecar = f.with_name(f.name + ".crossref.json")
            sidecar.write_text(json.dumps({
                "doi": "10.1234/example.5678",
                "publication_type": "journal-article",
                "contributors": [{"given_name": "Jane", "surname": "Doe", "role": "author"}],
            }), encoding="utf-8")
            record = crossref.build_record(f, root, 1)
            self.assertEqual(record["doi"], "10.1234/example.5678")
            self.assertEqual(record["doi_status"], "registered")
            self.assertEqual(record["crossref_applicable"], 1)
            self.assertEqual(len(record["contributors"]), 1)

    def test_unresolved_fields_default_to_unknown_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "journal-article-draft.pdf"
            f.write_text("body", encoding="utf-8")
            record = crossref.build_record(f, root, 1)
            self.assertEqual(record["container_title"], crossref.UNKNOWN)
            self.assertEqual(record["publisher"], crossref.UNKNOWN)
            self.assertEqual(record["contributors"], [])


class ScanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source_root = self.tmp / "source"
        (self.source_root / "publications").mkdir(parents=True)
        (self.source_root / "publications" / "smith-2024-journal-article.pdf").write_text("body", encoding="utf-8")
        (self.source_root / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (self.source_root / ".git").mkdir()
        (self.source_root / ".git" / "HEAD").write_text("skip", encoding="utf-8")

        self._orig_db_path = crossref.DB_PATH
        self._orig_output_dir = crossref.OUTPUT_DIR
        self._orig_paths = {
            name: getattr(crossref, name)
            for name in ("CATALOGUE_CSV", "CATALOGUE_JSON", "CATALOGUE_XML_DIR", "SCHEMA_JSON",
                         "MANUAL_REVIEW_CSV", "MIGRATION_LOG_CSV")
        }
        crossref.DB_PATH = self.tmp / "catalogue_crossref.db"
        crossref.OUTPUT_DIR = self.tmp / "crossref_out"
        crossref.CATALOGUE_CSV = crossref.OUTPUT_DIR / "crossref_catalogue.csv"
        crossref.CATALOGUE_JSON = crossref.OUTPUT_DIR / "crossref_catalogue.json"
        crossref.CATALOGUE_XML_DIR = crossref.OUTPUT_DIR / "crossref_xml"
        crossref.SCHEMA_JSON = crossref.OUTPUT_DIR / "catalogue_schema.json"
        crossref.MANUAL_REVIEW_CSV = crossref.OUTPUT_DIR / "catalogue_manual_review.csv"
        crossref.MIGRATION_LOG_CSV = crossref.OUTPUT_DIR / "catalogue_migration_log.csv"

        self.env = {"SOURCE_DATA_ROOTS": str(self.source_root)}
        self.project_config = {"project_id": "test_project"}

    def tearDown(self):
        crossref.DB_PATH = self._orig_db_path
        crossref.OUTPUT_DIR = self._orig_output_dir
        for name, value in self._orig_paths.items():
            setattr(crossref, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_does_not_create_real_db(self):
        crossref.cmd_scan(self.project_config, self.env, dry_run=True, apply=False)
        self.assertFalse(crossref.DB_PATH.exists())

    def test_apply_scan_catalogues_both_but_flags_only_one_applicable(self):
        crossref.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = crossref.get_db()
        rows = conn.execute("SELECT * FROM crossref_catalogue").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        by_name = {r["file_name"]: r for r in rows}
        self.assertEqual(by_name["smith-2024-journal-article.pdf"]["crossref_applicable"], 1)
        self.assertEqual(by_name["smith-2024-journal-article.pdf"]["publication_type"], "journal-article")
        self.assertEqual(by_name["data.csv"]["crossref_applicable"], 0)
        self.assertEqual(by_name["data.csv"]["publication_type"], crossref.NOT_APPLICABLE)

    def test_scan_is_idempotent(self):
        crossref.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = crossref.get_db()
        first_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM crossref_catalogue").fetchall()}
        conn.close()
        crossref.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = crossref.get_db()
        second_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM crossref_catalogue").fetchall()}
        conn.close()
        self.assertEqual(first_ids, second_ids)

    def test_export_writes_xml_only_for_applicable_records(self):
        crossref.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        crossref.cmd_export(self.project_config, self.env)
        xml_files = list(crossref.CATALOGUE_XML_DIR.glob("*.xml"))
        self.assertEqual(len(xml_files), 1)
        xml_text = xml_files[0].read_text(encoding="utf-8")
        self.assertIn('work_type="journal-article"', xml_text)

    def test_validate_ignores_non_applicable_records(self):
        crossref.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        crossref.cmd_validate(self.project_config, self.env)
        review_rows = crossref.MANUAL_REVIEW_CSV.read_text(encoding="utf-8")
        # The applicable record has no contributors -> flagged; the
        # non-applicable data.csv must not appear at all.
        self.assertIn("unresolved_contributors", review_rows)
        self.assertNotIn("data.csv", review_rows)


if __name__ == "__main__":
    unittest.main()
