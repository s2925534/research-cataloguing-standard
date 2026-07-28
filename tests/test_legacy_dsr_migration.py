#!/usr/bin/env python3
"""Tests for catalogues/legacy_dsr_migration.py.

Stdlib unittest only. Every test builds its own disposable legacy db (a
minimal subset of catalogue.py's real schema) and DSR db in a tempdir - the
real instance/catalogue.db is opened read-only if at all, and only ever via
an explicit path pointing at a test fixture, never the real one.

Run with:
    python3 -m unittest discover tests
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from catalogues import dsr_catalogue as dsr  # noqa: E402
from catalogues import legacy_dsr_migration as migration  # noqa: E402

LEGACY_SCHEMA_SQL = """
CREATE TABLE catalogue (
    catalogue_id TEXT PRIMARY KEY,
    source_path TEXT,
    proposed_filename TEXT,
    file_class TEXT,
    is_repo_rollup INTEGER DEFAULT 0,
    repo_file_count INTEGER,
    repo_total_size_bytes INTEGER
);
"""


def make_legacy_db(path: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA_SQL)
    for row in rows:
        conn.execute(
            "INSERT INTO catalogue (catalogue_id, source_path, proposed_filename, file_class, "
            "is_repo_rollup, repo_file_count, repo_total_size_bytes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                row["catalogue_id"], row.get("source_path"), row.get("proposed_filename"),
                row.get("file_class"), row.get("is_repo_rollup", 0),
                row.get("repo_file_count"), row.get("repo_total_size_bytes"),
            ),
        )
    conn.commit()
    conn.close()


class CrosswalkBuildTests(unittest.TestCase):
    """Migration source is exclusively the catalogued_files/documents/ copy -
    source_path is deliberately absent/irrelevant/stale-looking in these
    fixtures to prove it's never consulted."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source_dir = self.tmp / "source"
        self.source_dir.mkdir()

        self.legacy_db_path = self.tmp / "catalogue.db"
        make_legacy_db(self.legacy_db_path, [
            {"catalogue_id": "STD-00001", "source_path": str(self.source_dir / "moved-away-report.pdf"),
             "proposed_filename": "report_renamed.pdf", "file_class": "STD"},
            {"catalogue_id": "OPS-00001", "source_path": str(self.source_dir / "missing.pdf"),
             "proposed_filename": "also_missing.pdf", "file_class": "OPS"},
        ])
        self.dsr_db_path = self.tmp / "catalogue_dsr.db"
        self._orig_documents_dir = migration.CATALOGUE_DOCUMENTS_DIR
        migration.CATALOGUE_DOCUMENTS_DIR = self.tmp / "documents"
        migration.CATALOGUE_DOCUMENTS_DIR.mkdir(parents=True)
        # STD-00001's destination copy exists even though its source_path
        # points at a location that no longer has the file - proves the
        # resolver uses the destination, not source_path, as ground truth.
        (migration.CATALOGUE_DOCUMENTS_DIR / "report_renamed.pdf").write_text("pdf content", encoding="utf-8")

    def tearDown(self):
        migration.CATALOGUE_DOCUMENTS_DIR = self._orig_documents_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_migrates_from_destination_copy_ignoring_stale_source_path(self):
        legacy_conn = migration.open_legacy_db_readonly(self.legacy_db_path)
        dsr_conn = dsr.get_dsr_db(self.dsr_db_path)
        rows, stats = migration.build_crosswalk(legacy_conn, dsr_conn, {"project_id": "test"})
        dsr_conn.commit()
        legacy_conn.close()
        dsr_conn.close()

        self.assertEqual(stats["legacy_total"], 2)
        self.assertEqual(stats["migrated"], 1)
        self.assertEqual(stats["no_file"], 1)

        by_id = {r["legacy_catalogue_id"]: r for r in rows}
        self.assertEqual(by_id["STD-00001"]["status"], "migrated")
        self.assertEqual(by_id["STD-00001"]["source_kind"], "renamed_copy")
        self.assertTrue(by_id["STD-00001"]["dsr_stable_id"])
        # OPS-00001 has neither a destination copy nor is one findable -
        # its (also nonexistent) source_path must not rescue it.
        self.assertEqual(by_id["OPS-00001"]["status"], "skipped_no_file")
        self.assertEqual(by_id["OPS-00001"]["dsr_stable_id"], "")

    def test_valid_source_path_alone_is_not_enough_without_a_destination_copy(self):
        legacy_db = self.tmp / "catalogue_source_only.db"
        real_source = self.source_dir / "has-a-real-source-but-not-renamed-yet.pdf"
        real_source.write_text("still sitting at its original location", encoding="utf-8")
        make_legacy_db(legacy_db, [
            {"catalogue_id": "STD-00005", "source_path": str(real_source),
             "proposed_filename": None, "file_class": "STD"},
        ])
        legacy_conn = migration.open_legacy_db_readonly(legacy_db)
        dsr_conn = dsr.get_dsr_db(self.tmp / "catalogue_dsr_source_only.db")
        rows, stats = migration.build_crosswalk(legacy_conn, dsr_conn, {"project_id": "test"})
        dsr_conn.commit()
        legacy_conn.close()
        dsr_conn.close()

        self.assertEqual(rows[0]["status"], "skipped_no_file")
        self.assertEqual(stats["migrated"], 0)

    def test_repo_rollup_gets_cod_requires_review_entry(self):
        rollup_dir = self.tmp / "some_repo"
        rollup_dir.mkdir()
        (rollup_dir / "file.txt").write_text("x", encoding="utf-8")
        legacy_db = self.tmp / "catalogue_rollup.db"
        make_legacy_db(legacy_db, [
            {"catalogue_id": "STD-00099", "source_path": str(rollup_dir), "file_class": "STD",
             "is_repo_rollup": 1, "repo_file_count": 1, "repo_total_size_bytes": 1},
        ])
        legacy_conn = migration.open_legacy_db_readonly(legacy_db)
        dsr_conn = dsr.get_dsr_db(self.tmp / "catalogue_dsr_rollup.db")
        rows, stats = migration.build_crosswalk(legacy_conn, dsr_conn, {"project_id": "test"})
        dsr_conn.commit()
        legacy_conn.close()
        dsr_conn.close()

        self.assertEqual(stats["rollups"], 1)
        self.assertEqual(rows[0]["status"], "migrated")
        self.assertEqual(rows[0]["dsr_class_code"], "COD")
        self.assertEqual(rows[0]["dsr_confidence_status"], "Requires Review")

    def test_two_legacy_ids_pointing_at_same_file_share_one_dsr_entry(self):
        legacy_db = self.tmp / "catalogue_dup.db"
        make_legacy_db(legacy_db, [
            {"catalogue_id": "STD-00003", "proposed_filename": "report_renamed.pdf", "file_class": "STD"},
            {"catalogue_id": "STD-00004", "proposed_filename": "report_renamed.pdf", "file_class": "STD"},
        ])
        legacy_conn = migration.open_legacy_db_readonly(legacy_db)
        dsr_conn = dsr.get_dsr_db(self.tmp / "catalogue_dsr_dup.db")
        rows, stats = migration.build_crosswalk(legacy_conn, dsr_conn, {"project_id": "test"})
        dsr_conn.commit()
        legacy_conn.close()
        dsr_conn.close()

        self.assertEqual(rows[0]["dsr_stable_id"], rows[1]["dsr_stable_id"])
        dsr_conn2 = sqlite3.connect(self.tmp / "catalogue_dsr_dup.db")
        n = dsr_conn2.execute("SELECT COUNT(*) FROM dsr_catalogue").fetchone()[0]
        dsr_conn2.close()
        self.assertEqual(n, 1)


class CsvRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_then_load_crosswalk_map_only_keeps_migrated(self):
        rows = [
            {"legacy_catalogue_id": "STD-00001", "legacy_file_class": "STD", "status": "migrated",
             "source_kind": "source_path", "resolved_path": "x", "sha256": "abc",
             "dsr_stable_id": "DSR-STD-JRN-0001", "dsr_catalogue_id": "DSR-STD-JRN-0001-V0.1",
             "dsr_class_code": "REF", "dsr_subtype_code": "JRN", "dsr_confidence_status": "Confident",
             "notes": ""},
            {"legacy_catalogue_id": "OPS-00002", "legacy_file_class": "OPS", "status": "skipped_no_file",
             "source_kind": "unavailable", "resolved_path": "", "sha256": "", "dsr_stable_id": "",
             "dsr_catalogue_id": "", "dsr_class_code": "", "dsr_subtype_code": "",
             "dsr_confidence_status": "", "notes": "no file"},
        ]
        csv_path = self.tmp / "crosswalk.csv"
        migration.write_crosswalk_csv(rows, csv_path)
        mapping = migration.load_crosswalk_map(csv_path)
        self.assertEqual(mapping, {"STD-00001": "DSR-STD-JRN-0001"})


class ReferenceRewriteTests(unittest.TestCase):
    def test_single_id_marker_rewritten(self):
        text = "Some claim [INTERNAL EVIDENCE — STD-01533, flagged for review]."
        new_text, report = migration.rewrite_internal_evidence_markers(
            text, {"STD-01533": "DSR-STD-JRN-0042"}
        )
        self.assertEqual(new_text, "Some claim [INTERNAL EVIDENCE — DSR-STD-JRN-0042, flagged for review].")
        self.assertEqual(report["replaced"], {"STD-01533": 1})
        self.assertEqual(report["unmapped"], {})

    def test_multi_id_marker_both_rewritten(self):
        text = "[INTERNAL EVIDENCE — IMG-00037, IMG-00036, flagged for review]"
        new_text, report = migration.rewrite_internal_evidence_markers(
            text, {"IMG-00037": "DSR-IMG-SCR-0001", "IMG-00036": "DSR-IMG-SCR-0002"}
        )
        self.assertEqual(new_text, "[INTERNAL EVIDENCE — DSR-IMG-SCR-0001, DSR-IMG-SCR-0002, flagged for review]")

    def test_unmapped_id_left_untouched_and_reported(self):
        text = "[INTERNAL EVIDENCE — STD-99999, flagged for review]"
        new_text, report = migration.rewrite_internal_evidence_markers(text, {})
        self.assertEqual(new_text, text)
        self.assertEqual(report["unmapped"], {"STD-99999": 1})

    def test_qualifier_token_like_NEW_is_not_touched(self):
        text = "[INTERNAL EVIDENCE — STD-00986, NEW, flagged for review]"
        new_text, report = migration.rewrite_internal_evidence_markers(
            text, {"STD-00986": "DSR-STD-DOC-0007"}
        )
        self.assertEqual(new_text, "[INTERNAL EVIDENCE — DSR-STD-DOC-0007, NEW, flagged for review]")

    def test_ce_academic_citation_marker_never_touched(self):
        text = "As shown (Peltz et al., 2002 [CE-0057]) and [INTERNAL EVIDENCE — DAT-00107]."
        new_text, _ = migration.rewrite_internal_evidence_markers(text, {"DAT-00107": "DSR-DAT-SUR-0001"})
        self.assertIn("[CE-0057]", new_text)
        self.assertIn("DSR-DAT-SUR-0001", new_text)

    def test_register_source_line_trailing_id_rewritten_filename_untouched(self):
        text = (
            "Source file:\n"
            "instance/catalogued_files/documents/literature/"
            "LIT_OTHER_LIT-00535_19_07_2026_V01_RAW_PUB_TITLE_ROOT.pdf (LIT-00535)\n"
        )
        new_text, report = migration.rewrite_register_source_lines(text, {"LIT-00535": "DSR-REF-JRN-0001"})
        self.assertIn("LIT_OTHER_LIT-00535_19_07_2026_V01_RAW_PUB_TITLE_ROOT.pdf (DSR-REF-JRN-0001)", new_text)
        self.assertEqual(report["replaced"], {"LIT-00535": 1})


class ApplyToFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_apply_references_writes_copy_leaves_target_untouched(self):
        target = self.tmp / "working.md"
        target.write_text("A claim [INTERNAL EVIDENCE — STD-01533, flagged for review].", encoding="utf-8")
        out = self.tmp / "working.copy.md"
        migration.apply_references(target, out, {"STD-01533": "DSR-STD-JRN-0001"})
        self.assertIn("STD-01533", target.read_text(encoding="utf-8"))
        self.assertIn("DSR-STD-JRN-0001", out.read_text(encoding="utf-8"))


class ValidateContentPreservedTests(unittest.TestCase):
    def test_only_reference_change_passes(self):
        original = "Claim text [INTERNAL EVIDENCE — STD-01533, flagged for review] more text."
        updated = "Claim text [INTERNAL EVIDENCE — DSR-STD-JRN-0001, flagged for review] more text."
        tmp = Path(tempfile.mkdtemp())
        try:
            o, u = tmp / "o.md", tmp / "u.md"
            o.write_text(original, encoding="utf-8")
            u.write_text(updated, encoding="utf-8")
            ok, detail = migration.validate_content_preserved(o, u)
            self.assertTrue(ok, detail)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_prose_change_fails(self):
        original = "Claim text [INTERNAL EVIDENCE — STD-01533, flagged for review]."
        updated = "Different claim text entirely [INTERNAL EVIDENCE — DSR-STD-JRN-0001, flagged for review]."
        tmp = Path(tempfile.mkdtemp())
        try:
            o, u = tmp / "o.md", tmp / "u.md"
            o.write_text(original, encoding="utf-8")
            u.write_text(updated, encoding="utf-8")
            ok, detail = migration.validate_content_preserved(o, u)
            self.assertFalse(ok)
            self.assertIn("Different claim", detail)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PromoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_promote_backs_up_and_swaps(self):
        live = self.tmp / "live.md"
        live.write_text("old content", encoding="utf-8")
        new = self.tmp / "new.md"
        new.write_text("new content", encoding="utf-8")
        archive_dir = self.tmp / "archive"

        backup_path = migration.promote(live, new, archive_dir)

        self.assertEqual(live.read_text(encoding="utf-8"), "new content")
        self.assertEqual(backup_path.read_text(encoding="utf-8"), "old content")
        self.assertTrue(backup_path.parent == archive_dir)

    def test_promote_refuses_to_clobber_existing_backup(self):
        live = self.tmp / "live.md"
        live.write_text("old content", encoding="utf-8")
        new = self.tmp / "new.md"
        new.write_text("new content", encoding="utf-8")
        archive_dir = self.tmp / "archive"
        archive_dir.mkdir()

        # Pre-create the exact backup path promote() would compute right now,
        # so the collision is deterministic instead of relying on two real
        # calls landing in the same wall-clock second.
        from datetime import datetime as real_datetime, timezone as real_timezone
        timestamp = real_datetime.now(real_timezone.utc).strftime("%Y-%m-%d_%H%M%SZ")
        clobber_path = archive_dir / f"{live.stem}_pre-dsr-migration_{timestamp}{live.suffix}"
        clobber_path.write_text("pre-existing backup", encoding="utf-8")

        with self.assertRaises(SystemExit):
            migration.promote(live, new, archive_dir)


if __name__ == "__main__":
    unittest.main()
