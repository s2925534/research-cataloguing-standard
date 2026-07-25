#!/usr/bin/env python3
"""Tests for marc21_catalogue.py (the --marc21 mode).

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

import marc21_catalogue as marc21  # noqa: E402


class LeaderAnd008Tests(unittest.TestCase):
    def test_leader_is_exactly_24_chars(self):
        for record_type in marc21.RECORD_TYPES:
            leader = marc21.build_leader(record_type)
            self.assertEqual(len(leader), 24, msg=f"record_type={record_type}")

    def test_leader_encodes_type_of_record_at_position_6(self):
        leader = marc21.build_leader("k")
        self.assertEqual(leader[6], "k")

    def test_leader_ends_with_fixed_entry_map_4500(self):
        leader = marc21.build_leader("a")
        self.assertTrue(leader.endswith("4500"))

    def test_field_008_is_exactly_40_chars(self):
        field = marc21.build_008("260101", "2025")
        self.assertEqual(len(field), 40)

    def test_field_008_encodes_date_entered_and_year(self):
        field = marc21.build_008("260101", "2025")
        self.assertTrue(field.startswith("260101s2025"))

    def test_field_008_language_is_und(self):
        field = marc21.build_008("260101", "2025")
        self.assertEqual(field[35:38], "und")

    def test_field_008_uses_fill_char_for_unevaluated_positions(self):
        field = marc21.build_008("260101", "2025")
        self.assertEqual(field[11:15], marc21.FILL * 4)
        self.assertEqual(field[15:18], marc21.FILL * 3)


class ClassifyRecordTypeTests(unittest.TestCase):
    def test_docx_is_language_material(self):
        self.assertEqual(marc21.classify_record_type(Path("report.docx")), "a")

    def test_png_is_two_dimensional_graphic(self):
        self.assertEqual(marc21.classify_record_type(Path("figure.png")), "k")

    def test_unmapped_extension_defaults_to_computer_file(self):
        self.assertEqual(marc21.classify_record_type(Path("weird.xyz123")), "m")


class NonfilingCountTests(unittest.TestCase):
    def test_the_prefix_counts_four(self):
        self.assertEqual(marc21._nonfiling_count("The Great Report"), 4)

    def test_no_article_counts_zero(self):
        self.assertEqual(marc21._nonfiling_count("Great Report"), 0)


class BuildRecordTests(unittest.TestCase):
    def test_creator_name_guards_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            record = marc21.build_record(f, root, 1, {"researcher": "REPLACE_ME"})
            self.assertIsNone(record["creator_name"])
            record2 = marc21.build_record(f, root, 1, {"researcher": "Pedro Veloso"})
            self.assertEqual(record2["creator_name"], "Pedro Veloso")

    def test_never_fabricates_isbn_issn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello", encoding="utf-8")
            record = marc21.build_record(f, root, 1, {})
            self.assertIsNone(record["isbn"])
            self.assertIsNone(record["issn"])

    def test_extent_uses_online_resource_phrasing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "note.txt"
            f.write_text("hello world", encoding="utf-8")
            record = marc21.build_record(f, root, 1, {})
            self.assertEqual(record["extent"], f"1 online resource ({len('hello world')} bytes)")


class ScanIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source_root = self.tmp / "source"
        self.source_root.mkdir(parents=True)
        (self.source_root / "the-great-report.docx").write_text("body", encoding="utf-8")
        (self.source_root / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (self.source_root / ".git").mkdir()
        (self.source_root / ".git" / "HEAD").write_text("skip", encoding="utf-8")

        self._orig_db_path = marc21.DB_PATH
        self._orig_output_dir = marc21.OUTPUT_DIR
        self._orig_paths = {
            name: getattr(marc21, name)
            for name in ("CATALOGUE_CSV", "CATALOGUE_JSON", "MARC_MRK_PATH", "SCHEMA_JSON",
                         "MANUAL_REVIEW_CSV", "MIGRATION_LOG_CSV")
        }
        marc21.DB_PATH = self.tmp / "catalogue_marc21.db"
        marc21.OUTPUT_DIR = self.tmp / "marc21_out"
        marc21.CATALOGUE_CSV = marc21.OUTPUT_DIR / "marc21_catalogue.csv"
        marc21.CATALOGUE_JSON = marc21.OUTPUT_DIR / "marc21_catalogue.json"
        marc21.MARC_MRK_PATH = marc21.OUTPUT_DIR / "marc21_catalogue.mrk"
        marc21.SCHEMA_JSON = marc21.OUTPUT_DIR / "catalogue_schema.json"
        marc21.MANUAL_REVIEW_CSV = marc21.OUTPUT_DIR / "catalogue_manual_review.csv"
        marc21.MIGRATION_LOG_CSV = marc21.OUTPUT_DIR / "catalogue_migration_log.csv"

        self.env = {"SOURCE_DATA_ROOTS": str(self.source_root)}
        self.project_config = {"project_id": "test_project", "researcher": "Pedro Veloso"}

    def tearDown(self):
        marc21.DB_PATH = self._orig_db_path
        marc21.OUTPUT_DIR = self._orig_output_dir
        for name, value in self._orig_paths.items():
            setattr(marc21, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_does_not_create_real_db(self):
        marc21.cmd_scan(self.project_config, self.env, dry_run=True, apply=False)
        self.assertFalse(marc21.DB_PATH.exists())

    def test_apply_scan_catalogues_two_files_and_skips_git(self):
        marc21.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = marc21.get_db()
        rows = conn.execute("SELECT * FROM marc21_catalogue").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(len(row["leader"]), 24)
            self.assertEqual(len(row["field_008"]), 40)

    def test_scan_is_idempotent(self):
        marc21.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = marc21.get_db()
        first_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM marc21_catalogue").fetchall()}
        conn.close()
        marc21.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = marc21.get_db()
        second_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM marc21_catalogue").fetchall()}
        conn.close()
        self.assertEqual(first_ids, second_ids)

    def test_export_writes_valid_mrk_with_creator_and_electronic_location(self):
        marc21.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        marc21.cmd_export(self.project_config, self.env)
        self.assertTrue(marc21.MARC_MRK_PATH.exists())
        mrk_text = marc21.MARC_MRK_PATH.read_text(encoding="utf-8")
        self.assertIn("=LDR  ", mrk_text)
        self.assertIn("=245  10$a", mrk_text)  # ind1=1 (has 100 field), nonfiling=0 (no article prefix)
        self.assertIn("=100  0\\$aPedro Veloso", mrk_text)
        self.assertIn("=856  0\\$u", mrk_text)

    def test_nonfiling_count_reflected_in_mrk_when_title_has_article_prefix(self):
        sidecar = (self.source_root / "data.csv").with_name("data.csv.marc21.json")
        sidecar.write_text('{"title": "The Great Report"}', encoding="utf-8")
        marc21.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        marc21.cmd_export(self.project_config, self.env)
        mrk_text = marc21.MARC_MRK_PATH.read_text(encoding="utf-8")
        self.assertIn("=245  14$aThe Great Report", mrk_text)

    def test_validate_passes_with_no_structural_issues(self):
        marc21.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        marc21.cmd_validate(self.project_config, self.env)  # should not raise


if __name__ == "__main__":
    unittest.main()
