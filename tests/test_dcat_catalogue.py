#!/usr/bin/env python3
"""Tests for dcat_catalogue.py (the --dcat mode).

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

from catalogues import dcat_catalogue as dcat  # noqa: E402


class BuildRecordTests(unittest.TestCase):
    def test_dataset_and_distribution_uris_are_content_addressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "data.csv"
            f.write_text("a,b\n1,2\n", encoding="utf-8")
            record = dcat.build_record(f, root, 1, {})
            expected_hash = dcat.common.sha256_file(f)
            self.assertEqual(record["dataset_uri"], f"urn:dcat:dataset:sha256-{expected_hash}")
            self.assertEqual(record["distribution_uri"], f"urn:dcat:distribution:sha256-{expected_hash}")

    def test_access_url_is_a_file_uri(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "data.csv"
            f.write_text("a,b\n1,2\n", encoding="utf-8")
            record = dcat.build_record(f, root, 1, {})
            self.assertTrue(record["access_url"].startswith("file://"))

    def test_publisher_uses_project_config_institution_when_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "data.csv"
            f.write_text("a,b\n1,2\n", encoding="utf-8")
            record = dcat.build_record(f, root, 1, {"institution": "QUT"})
            self.assertEqual(record["publisher"], "QUT")

    def test_unresolved_fields_default_to_unknown_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "data.csv"
            f.write_text("a,b\n1,2\n", encoding="utf-8")
            record = dcat.build_record(f, root, 1, {})
            self.assertEqual(record["description"], dcat.UNKNOWN)
            self.assertEqual(record["license"], dcat.UNKNOWN)
            self.assertEqual(record["keywords"], [])


class TurtleBuildTests(unittest.TestCase):
    def test_empty_catalog_produces_valid_terminator(self):
        turtle = dcat._build_turtle("urn:dcat:catalog:test", "Test Catalog", [])
        self.assertIn('<urn:dcat:catalog:test> a dcat:Catalog ;', turtle)
        self.assertIn('dct:title """Test Catalog""" .', turtle)

    def test_non_empty_catalog_lists_datasets(self):
        records = [{
            "dataset_uri": "urn:dcat:dataset:sha256-abc", "distribution_uri": "urn:dcat:distribution:sha256-abc",
            "title": "A dataset", "description": dcat.UNKNOWN, "issued": "2026-01-01", "modified": "2026-01-02",
            "publisher": dcat.UNKNOWN, "license": dcat.UNKNOWN, "keywords": [],
            "access_url": "file:///tmp/data.csv", "media_type": "text/csv", "format": "CSV",
            "byte_size": 42, "sha256": "abc123",
        }]
        turtle = dcat._build_turtle("urn:dcat:catalog:test", "Test Catalog", records)
        self.assertIn("dcat:dataset <urn:dcat:dataset:sha256-abc> .", turtle)
        self.assertIn("spdx:checksumValue \"abc123\"^^xsd:hexBinary", turtle)


class ScanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source_root = self.tmp / "source"
        self.source_root.mkdir(parents=True)
        (self.source_root / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (self.source_root / "report.pdf").write_text("body", encoding="utf-8")
        (self.source_root / "node_modules").mkdir()
        (self.source_root / "node_modules" / "x.js").write_text("skip", encoding="utf-8")

        self._orig_db_path = dcat.DB_PATH
        self._orig_output_dir = dcat.OUTPUT_DIR
        self._orig_paths = {
            name: getattr(dcat, name)
            for name in ("CATALOGUE_CSV", "CATALOGUE_JSON", "TURTLE_DIR", "SCHEMA_JSON",
                         "MANUAL_REVIEW_CSV", "MIGRATION_LOG_CSV")
        }
        dcat.DB_PATH = self.tmp / "catalogue_dcat.db"
        dcat.OUTPUT_DIR = self.tmp / "dcat_out"
        dcat.CATALOGUE_CSV = dcat.OUTPUT_DIR / "dcat_catalogue.csv"
        dcat.CATALOGUE_JSON = dcat.OUTPUT_DIR / "dcat_catalogue.json"
        dcat.TURTLE_DIR = dcat.OUTPUT_DIR / "turtle"
        dcat.SCHEMA_JSON = dcat.OUTPUT_DIR / "catalogue_schema.json"
        dcat.MANUAL_REVIEW_CSV = dcat.OUTPUT_DIR / "catalogue_manual_review.csv"
        dcat.MIGRATION_LOG_CSV = dcat.OUTPUT_DIR / "catalogue_migration_log.csv"

        self.env = {"SOURCE_DATA_ROOTS": str(self.source_root)}
        self.project_config = {"project_id": "test_project", "institution": "Test University"}

    def tearDown(self):
        dcat.DB_PATH = self._orig_db_path
        dcat.OUTPUT_DIR = self._orig_output_dir
        for name, value in self._orig_paths.items():
            setattr(dcat, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_does_not_create_real_db(self):
        dcat.cmd_scan(self.project_config, self.env, dry_run=True, apply=False)
        self.assertFalse(dcat.DB_PATH.exists())

    def test_apply_scan_catalogues_two_files_and_skips_node_modules(self):
        dcat.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = dcat.get_db()
        rows = conn.execute("SELECT * FROM dcat_catalogue").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)

    def test_scan_is_idempotent(self):
        dcat.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = dcat.get_db()
        first_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM dcat_catalogue").fetchall()}
        conn.close()
        dcat.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = dcat.get_db()
        second_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM dcat_catalogue").fetchall()}
        conn.close()
        self.assertEqual(first_ids, second_ids)

    def test_export_writes_valid_turtle_file(self):
        dcat.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        dcat.cmd_export(self.project_config, self.env)
        ttl_files = list(dcat.TURTLE_DIR.glob("*.ttl"))
        self.assertEqual(len(ttl_files), 1)
        ttl_text = ttl_files[0].read_text(encoding="utf-8")
        self.assertIn("a dcat:Catalog", ttl_text)
        self.assertIn("a dcat:Dataset", ttl_text)
        self.assertIn("a dcat:Distribution", ttl_text)
        self.assertIn("spdx:Checksum", ttl_text)

    def test_validate_flags_unresolved_publisher_when_not_configured(self):
        dcat.cmd_scan({"project_id": "x"}, self.env, dry_run=False, apply=True)
        dcat.cmd_validate({"project_id": "x"}, self.env)
        review_rows = dcat.MANUAL_REVIEW_CSV.read_text(encoding="utf-8")
        self.assertIn("unresolved_publisher", review_rows)


if __name__ == "__main__":
    unittest.main()
