#!/usr/bin/env python3
"""Tests for premis_catalogue.py (the --premis mode).

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

import premis_catalogue as premis  # noqa: E402


class BuildObjectRecordTests(unittest.TestCase):
    def test_object_identifier_is_content_addressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            sha256 = premis.common.sha256_file(f)
            record = premis.build_object_record(f, root, 1, sha256)
            self.assertEqual(record["object_identifier_value"], f"urn:premis:sha256:{sha256}")
            self.assertEqual(record["message_digest_algorithm"], "SHA-256")

    def test_creating_application_and_preservation_level_not_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            sha256 = premis.common.sha256_file(f)
            record = premis.build_object_record(f, root, 1, sha256)
            self.assertEqual(record["creating_application"], premis.UNKNOWN)
            self.assertEqual(record["preservation_level"], premis.NOT_ASSIGNED)


class ScanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source_root = self.tmp / "source"
        self.source_root.mkdir(parents=True)
        (self.source_root / "note.txt").write_text("hello", encoding="utf-8")
        (self.source_root / ".git").mkdir()
        (self.source_root / ".git" / "HEAD").write_text("skip", encoding="utf-8")

        self._orig_db_path = premis.DB_PATH
        self._orig_output_dir = premis.OUTPUT_DIR
        self._orig_paths = {
            name: getattr(premis, name)
            for name in ("OBJECTS_CSV", "OBJECTS_JSON", "EVENTS_CSV", "EVENTS_JSON",
                         "PREMIS_XML_DIR", "SCHEMA_JSON", "MANUAL_REVIEW_CSV", "MIGRATION_LOG_CSV")
        }
        premis.DB_PATH = self.tmp / "catalogue_premis.db"
        premis.OUTPUT_DIR = self.tmp / "premis_out"
        premis.OBJECTS_CSV = premis.OUTPUT_DIR / "premis_objects.csv"
        premis.OBJECTS_JSON = premis.OUTPUT_DIR / "premis_objects.json"
        premis.EVENTS_CSV = premis.OUTPUT_DIR / "premis_events.csv"
        premis.EVENTS_JSON = premis.OUTPUT_DIR / "premis_events.json"
        premis.PREMIS_XML_DIR = premis.OUTPUT_DIR / "premis_xml"
        premis.SCHEMA_JSON = premis.OUTPUT_DIR / "catalogue_schema.json"
        premis.MANUAL_REVIEW_CSV = premis.OUTPUT_DIR / "catalogue_manual_review.csv"
        premis.MIGRATION_LOG_CSV = premis.OUTPUT_DIR / "catalogue_migration_log.csv"

        self.env = {"SOURCE_DATA_ROOTS": str(self.source_root)}
        self.project_config = {"project_id": "test_project"}

    def tearDown(self):
        premis.DB_PATH = self._orig_db_path
        premis.OUTPUT_DIR = self._orig_output_dir
        for name, value in self._orig_paths.items():
            setattr(premis, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_does_not_create_real_db(self):
        premis.cmd_scan(self.project_config, self.env, dry_run=True, apply=False)
        self.assertFalse(premis.DB_PATH.exists())

    def test_first_scan_ingests_and_logs_one_ingestion_event(self):
        premis.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = premis.get_db()
        objects = conn.execute("SELECT * FROM premis_objects").fetchall()
        events = conn.execute("SELECT * FROM premis_events").fetchall()
        conn.close()
        self.assertEqual(len(objects), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "ingestion")
        self.assertEqual(events[0]["event_outcome"], "success")

    def test_object_identity_stable_but_events_grow_on_rescan(self):
        # PREMIS is the one module where "idempotent" does NOT mean "nothing
        # changes on rescan" - objects must not duplicate, but a fresh fixity
        # check event is the whole point of running scan again.
        premis.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = premis.get_db()
        first_object_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM premis_objects").fetchall()}
        conn.close()

        premis.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = premis.get_db()
        second_object_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM premis_objects").fetchall()}
        events = conn.execute("SELECT * FROM premis_events ORDER BY event_id").fetchall()
        conn.close()

        self.assertEqual(first_object_ids, second_object_ids)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event_type"], "ingestion")
        self.assertEqual(events[1]["event_type"], "fixity check")
        self.assertEqual(events[1]["event_outcome"], "success")

    def test_changed_content_logs_fixity_warning_and_updates_digest(self):
        premis.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        (self.source_root / "note.txt").write_text("modified content", encoding="utf-8")
        premis.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)

        conn = premis.get_db()
        obj = conn.execute("SELECT * FROM premis_objects").fetchone()
        events = conn.execute("SELECT * FROM premis_events ORDER BY event_id").fetchall()
        conn.close()

        self.assertEqual(obj["message_digest"], premis.common.sha256_file(self.source_root / "note.txt"))
        fixity_events = [e for e in events if e["event_type"] == "fixity check"]
        self.assertEqual(len(fixity_events), 1)
        self.assertEqual(fixity_events[0]["event_outcome"], "warning")

    def test_export_writes_objects_events_and_premis_xml(self):
        premis.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        premis.cmd_export(self.project_config, self.env)
        for path in (premis.OBJECTS_CSV, premis.OBJECTS_JSON, premis.EVENTS_CSV, premis.EVENTS_JSON,
                     premis.SCHEMA_JSON, premis.MANUAL_REVIEW_CSV, premis.MIGRATION_LOG_CSV):
            self.assertTrue(path.exists(), f"missing output: {path}")
        xml_files = list(premis.PREMIS_XML_DIR.glob("*.xml"))
        self.assertEqual(len(xml_files), 1)
        xml_text = xml_files[0].read_text(encoding="utf-8")
        self.assertIn("premis:objectCharacteristics", xml_text)
        self.assertIn("premis:event", xml_text)
        self.assertIn("premis:agent", xml_text)
        self.assertIn(premis.AGENT_NAME, xml_text)

    def test_validate_passes_with_no_structural_issues(self):
        premis.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        premis.cmd_validate(self.project_config, self.env)  # should not raise


if __name__ == "__main__":
    unittest.main()
