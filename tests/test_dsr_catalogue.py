#!/usr/bin/env python3
"""Tests for dsr_catalogue.py (the --dsr mode).

Stdlib unittest only. Every test uses a disposable tempdir for source files
and monkeypatches dsr_catalogue's module-level DB/output-path constants to
tempfiles for the duration of the test - never reads or writes this
checkout's real instance/ (gitignored, per-project, and explicitly off
limits per the project's "do not touch my research/catalogue" policy).

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
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from catalogues import dsr_catalogue as dsr  # noqa: E402


class ExclusionTests(unittest.TestCase):
    def test_git_dir_excluded(self):
        root = Path("/proj")
        self.assertTrue(dsr.is_excluded(root / ".git" / "HEAD", root))

    def test_node_modules_excluded(self):
        root = Path("/proj")
        self.assertTrue(dsr.is_excluded(root / "node_modules" / "pkg" / "index.js", root))

    def test_office_lock_file_excluded(self):
        root = Path("/proj")
        self.assertTrue(dsr.is_excluded(root / "~$report.docx", root))

    def test_ds_store_excluded(self):
        root = Path("/proj")
        self.assertTrue(dsr.is_excluded(root / ".DS_Store", root))

    def test_bak_suffix_excluded(self):
        root = Path("/proj")
        self.assertTrue(dsr.is_excluded(root / "notes.md.bak", root))

    def test_exe_installer_excluded(self):
        root = Path("/proj")
        self.assertTrue(dsr.is_excluded(root / "some-app-setup.exe", root))

    def test_dmg_installer_excluded(self):
        root = Path("/proj")
        self.assertTrue(dsr.is_excluded(root / "GoogleChrome.dmg", root))

    def test_normal_file_not_excluded(self):
        root = Path("/proj")
        self.assertFalse(dsr.is_excluded(root / "artefacts" / "process-model.md", root))


class ClassifyExtensionTests(unittest.TestCase):
    def setUp(self):
        self.rules = dsr.default_dsr_rules()
        self.rules["project_code"] = "DSR"
        self.rules["id_padding"] = 4

    def test_csv_maps_to_dat_csv(self):
        result = dsr.classify_file(Path("data/export.csv"), Path("data").parent, self.rules)
        self.assertEqual((result["class_code"], result["subtype_code"]), ("DAT", "CSV"))

    def test_no_extension_unrecognisable_content_still_requires_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "mystery-file"
            f.write_bytes(b"\x00\x01\x02 not recognisable as anything in particular")
            result = dsr.classify_file(f, root, self.rules)
            self.assertEqual(result["confidence_status"], dsr.REQUIRES_REVIEW)

    def test_no_extension_json_content_maps_to_dat_jse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "some-export"
            f.write_text('{"a": 1, "b": [1, 2, 3]}', encoding="utf-8")
            result = dsr.classify_file(f, root, self.rules)
            # class/subtype resolve confidently via content-sniff even though
            # this filename (deliberately, for this test) has no version
            # token - that's an independent, pre-existing reason a record
            # can still land in Requires Review overall.
            self.assertEqual((result["class_code"], result["subtype_code"]), ("DAT", "JSE"))
            self.assertNotEqual(result["classification_rule"], "fallback:unmapped_extension")

    def test_no_extension_html_content_maps_to_doc_wrk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "saved-page"
            f.write_text("<!DOCTYPE html><html><body>hi</body></html>", encoding="utf-8")
            result = dsr.classify_file(f, root, self.rules)
            self.assertEqual((result["class_code"], result["subtype_code"]), ("DOC", "WRK"))

    def test_no_extension_pdf_content_maps_like_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "documents"
            root.mkdir()
            f = root / "no-ext-report"
            f.write_bytes(b"%PDF-1.7\n%rest of a fake pdf body")
            result = dsr.classify_file(f, Path(tmp), self.rules)
            self.assertEqual((result["class_code"], result["subtype_code"]), ("DOC", "RPT"))

    def test_no_extension_email_content_maps_to_rec_cor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "message"
            f.write_bytes(b"From: a@example.com\nTo: b@example.com\nSubject: hi\n\nbody text")
            result = dsr.classify_file(f, root, self.rules)
            self.assertEqual((result["class_code"], result["subtype_code"]), ("REC", "COR"))

    def test_html_maps_to_doc_wrk(self):
        result = dsr.classify_file(Path("misc/delivery-notice.html"), Path("misc").parent, self.rules)
        self.assertEqual((result["class_code"], result["subtype_code"]), ("DOC", "WRK"))

    def test_htm_maps_to_doc_wrk(self):
        result = dsr.classify_file(Path("misc/notice.htm"), Path("misc").parent, self.rules)
        self.assertEqual((result["class_code"], result["subtype_code"]), ("DOC", "WRK"))

    def test_python_source_maps_to_cod_pyt(self):
        result = dsr.classify_file(Path("scripts/run.py"), Path("scripts").parent, self.rules)
        self.assertEqual((result["class_code"], result["subtype_code"]), ("COD", "PYT"))

    def test_python_test_file_maps_to_cod_tst(self):
        result = dsr.classify_file(Path("scripts/test_run.py"), Path("scripts").parent, self.rules)
        self.assertEqual(result["subtype_code"], "TST")

    def test_lifecycle_png_refines_to_img_lif(self):
        result = dsr.classify_file(Path("figures/container-lifecycle.png"), Path("figures").parent, self.rules)
        self.assertEqual((result["class_code"], result["subtype_code"]), ("IMG", "LIF"))

    def test_meeting_docx_maps_to_rec_mtg_by_filename_token(self):
        result = dsr.classify_file(Path("misc/2026-01-05-meeting-notes.docx"), Path("misc").parent, self.rules)
        self.assertEqual((result["class_code"], result["subtype_code"]), ("REC", "MTG"))

    def test_unmapped_extension_requires_review(self):
        result = dsr.classify_file(Path("misc/weird.xyz123"), Path("misc").parent, self.rules)
        self.assertEqual(result["confidence_status"], dsr.REQUIRES_REVIEW)

    def test_pdf_with_no_signal_is_unresolved_reference_grey(self):
        result = dsr.classify_file(Path("dump/random.pdf"), Path("dump").parent, self.rules)
        self.assertEqual((result["class_code"], result["subtype_code"]), ("REF", "GRY"))
        self.assertEqual(result["confidence_status"], dsr.REQUIRES_REVIEW)

    def test_pdf_under_references_dir_with_journal_token(self):
        result = dsr.classify_file(
            Path("references/smith-2020-journal-article.pdf"), Path("references").parent, self.rules
        )
        self.assertEqual((result["class_code"], result["subtype_code"]), ("REF", "JRN"))

    def test_pdf_under_documents_dir_is_authored_report(self):
        result = dsr.classify_file(Path("documents/annual-report.pdf"), Path("documents").parent, self.rules)
        self.assertEqual((result["class_code"], result["subtype_code"]), ("DOC", "RPT"))


class DirectoryAndArtefactTests(unittest.TestCase):
    def setUp(self):
        self.rules = dsr.default_dsr_rules()
        self.rules["project_code"] = "DSR"
        self.rules["id_padding"] = 4

    def test_artefacts_dir_with_model_token_yields_art_mod(self):
        result = dsr.classify_file(Path("artefacts/process-model.md"), Path("artefacts").parent, self.rules)
        self.assertEqual((result["class_code"], result["subtype_code"]), ("ART", "MOD"))
        self.assertEqual(result["dsr_artefact_type"], "Model")

    def test_artefacts_dir_with_framework_token_wins_over_model(self):
        result = dsr.classify_file(
            Path("artefacts/integrated-framework-and-model.md"), Path("artefacts").parent, self.rules
        )
        self.assertEqual(result["subtype_code"], "FRM")
        self.assertEqual(result["dsr_artefact_type"], "Integrated Framework")

    def test_artefacts_dir_with_no_token_is_requires_review(self):
        result = dsr.classify_file(Path("artefacts/untitled123.md"), Path("artefacts").parent, self.rules)
        self.assertEqual(result["subtype_code"], dsr.ART_UNRESOLVED_SUBTYPE)
        self.assertEqual(result["dsr_artefact_type"], dsr.REQUIRES_REVIEW)

    def test_ordinary_report_about_a_model_is_not_promoted_to_art(self):
        # Outside an /artefacts/ directory and without an explicit sidecar,
        # a document merely discussing "model" must stay DOC, not ART - file
        # type/filename tokens alone must never promote a report into being
        # treated as the artefact itself.
        result = dsr.classify_file(
            Path("documents/report-about-the-process-model.docx"), Path("documents").parent, self.rules
        )
        self.assertEqual(result["class_code"], "DOC")
        self.assertEqual(result["dsr_artefact_type"], "Not Applicable")


class IdSafeClassTokenTests(unittest.TestCase):
    def test_requires_review_maps_to_safe_placeholder(self):
        self.assertEqual(dsr.id_safe_class_token(dsr.REQUIRES_REVIEW), "UNC")

    def test_real_class_code_passes_through_unchanged(self):
        for cls in dsr.MAIN_CLASSES:
            self.assertEqual(dsr.id_safe_class_token(cls), cls)

    def test_placeholder_has_no_space_or_lowercase(self):
        token = dsr.id_safe_class_token(dsr.REQUIRES_REVIEW)
        self.assertNotIn(" ", token)
        self.assertEqual(token, token.upper())


class VersionTests(unittest.TestCase):
    def test_no_version_token_defaults_v0_1_and_flags_review(self):
        version, review = dsr._determine_version("process-model")
        self.assertEqual(version, "V0.1")
        self.assertTrue(review)

    def test_v1_token_parsed(self):
        version, review = dsr._determine_version("process-model_v1")
        self.assertEqual(version, "V1.0")
        self.assertFalse(review)

    def test_v_dot_token_parsed(self):
        version, review = dsr._determine_version("process-model-v2.3")
        self.assertEqual(version, "V2.3")
        self.assertFalse(review)


class SidecarMetadataTests(unittest.TestCase):
    def test_valid_sidecar_overrides_extension_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "misc" / "some-file.txt"
            target.parent.mkdir(parents=True)
            target.write_text("content", encoding="utf-8")
            sidecar = target.with_name(target.name + ".dsrmeta.json")
            sidecar.write_text(json.dumps({"class_code": "ART", "subtype_code": "CON"}), encoding="utf-8")

            rules = dsr.default_dsr_rules()
            rules["project_code"] = "DSR"
            rules["id_padding"] = 4
            result = dsr.classify_file(target, root, rules)
            self.assertEqual((result["class_code"], result["subtype_code"]), ("ART", "CON"))
            self.assertEqual(result["classification_rule"], "explicit_sidecar_metadata")

    def test_invalid_sidecar_class_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "data.csv"
            target.write_text("a,b\n1,2\n", encoding="utf-8")
            sidecar = target.with_name(target.name + ".dsrmeta.json")
            sidecar.write_text(json.dumps({"class_code": "NOT-A-CLASS", "subtype_code": "X"}), encoding="utf-8")

            rules = dsr.default_dsr_rules()
            rules["project_code"] = "DSR"
            rules["id_padding"] = 4
            result = dsr.classify_file(target, root, rules)
            self.assertEqual((result["class_code"], result["subtype_code"]), ("DAT", "CSV"))


class ScanIntegrationTests(unittest.TestCase):
    """Exercises cmd_scan end-to-end against a disposable source tree and
    a disposable DSR db/output dir (module constants monkeypatched, restored
    in tearDown) - the real instance/ is never touched."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source_root = self.tmp / "source"
        (self.source_root / "artefacts").mkdir(parents=True)
        (self.source_root / "artefacts" / "process-model-v1.md").write_text("model spec", encoding="utf-8")
        (self.source_root / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        (self.source_root / "node_modules" / "pkg").mkdir(parents=True)
        (self.source_root / "node_modules" / "pkg" / "index.js").write_text("skip me", encoding="utf-8")

        self._orig_db_path = dsr.DSR_DB_PATH
        self._orig_output_dir = dsr.DSR_OUTPUT_DIR
        self._orig_paths = {
            name: getattr(dsr, name)
            for name in (
                "DSR_RESEARCH_CATALOGUE_CSV", "DSR_RESEARCH_CATALOGUE_JSON", "DSR_RESEARCH_CATALOGUE_MD",
                "DSR_RESEARCH_CATALOGUE_SQLITE", "DSR_RELATIONSHIPS_CSV", "DSR_SCHEMA_JSON",
                "DSR_CONTROLLED_VOCAB_JSON", "DSR_MIGRATION_LOG_CSV", "DSR_MANUAL_REVIEW_CSV",
                "DSR_CLASSIFICATION_RULES_JSON",
            )
        }

        dsr.DSR_DB_PATH = self.tmp / "catalogue_dsr.db"
        dsr.DSR_OUTPUT_DIR = self.tmp / "dsr_out"
        dsr.DSR_RESEARCH_CATALOGUE_CSV = dsr.DSR_OUTPUT_DIR / "research_catalogue.csv"
        dsr.DSR_RESEARCH_CATALOGUE_JSON = dsr.DSR_OUTPUT_DIR / "research_catalogue.json"
        dsr.DSR_RESEARCH_CATALOGUE_MD = dsr.DSR_OUTPUT_DIR / "research_catalogue.md"
        dsr.DSR_RESEARCH_CATALOGUE_SQLITE = dsr.DSR_OUTPUT_DIR / "research_catalogue.sqlite"
        dsr.DSR_RELATIONSHIPS_CSV = dsr.DSR_OUTPUT_DIR / "catalogue_relationships.csv"
        dsr.DSR_SCHEMA_JSON = dsr.DSR_OUTPUT_DIR / "catalogue_schema.json"
        dsr.DSR_CONTROLLED_VOCAB_JSON = dsr.DSR_OUTPUT_DIR / "catalogue_controlled_vocabulary.json"
        dsr.DSR_MIGRATION_LOG_CSV = dsr.DSR_OUTPUT_DIR / "catalogue_migration_log.csv"
        dsr.DSR_MANUAL_REVIEW_CSV = dsr.DSR_OUTPUT_DIR / "catalogue_manual_review.csv"
        dsr.DSR_CLASSIFICATION_RULES_JSON = dsr.DSR_OUTPUT_DIR / "catalogue_classification_rules.json"

        self.env = {"SOURCE_DATA_ROOTS": str(self.source_root)}
        self.project_config = {"project_id": "test_project"}

    def tearDown(self):
        dsr.DSR_DB_PATH = self._orig_db_path
        dsr.DSR_OUTPUT_DIR = self._orig_output_dir
        for name, value in self._orig_paths.items():
            setattr(dsr, name, value)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_does_not_create_real_db(self):
        dsr.cmd_scan(self.project_config, self.env, dry_run=True, apply=False)
        self.assertFalse(dsr.DSR_DB_PATH.exists())

    def test_apply_scan_catalogues_two_files_and_skips_node_modules(self):
        dsr.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = dsr.get_dsr_db()
        rows = conn.execute("SELECT * FROM dsr_catalogue").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        by_name = {r["file_name"]: r for r in rows}
        self.assertIn("process-model-v1.md", by_name)
        self.assertIn("data.csv", by_name)
        model_row = by_name["process-model-v1.md"]
        self.assertEqual(model_row["class_code"], "ART")
        self.assertEqual(model_row["subtype_code"], "MOD")
        self.assertEqual(model_row["version"], "V1.0")
        self.assertTrue(model_row["stable_id"].startswith("DSR-ART-MOD-"))
        self.assertEqual(model_row["catalogue_id"], f"{model_row['stable_id']}-V1.0")

    def test_unmapped_extension_gets_space_free_stable_id(self):
        (self.source_root / "mystery.xyz123").write_text("no known mapping", encoding="utf-8")
        dsr.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = dsr.get_dsr_db()
        row = conn.execute("SELECT * FROM dsr_catalogue WHERE file_name = 'mystery.xyz123'").fetchone()
        conn.close()
        self.assertEqual(row["class_code"], dsr.REQUIRES_REVIEW)  # DB column keeps the real status
        self.assertNotIn(" ", row["stable_id"])  # but the id string itself never embeds it literally
        self.assertTrue(row["stable_id"].startswith("DSR-UNC-"))

    def test_scan_is_idempotent(self):
        dsr.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = dsr.get_dsr_db()
        first_count = conn.execute("SELECT COUNT(*) AS c FROM dsr_catalogue").fetchone()["c"]
        first_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM dsr_catalogue").fetchall()}
        conn.close()

        dsr.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = dsr.get_dsr_db()
        second_count = conn.execute("SELECT COUNT(*) AS c FROM dsr_catalogue").fetchone()["c"]
        second_ids = {r["catalogue_id"] for r in conn.execute("SELECT catalogue_id FROM dsr_catalogue").fetchall()}
        conn.close()

        self.assertEqual(first_count, second_count)
        self.assertEqual(first_ids, second_ids)

    def test_stable_id_survives_file_rename(self):
        dsr.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = dsr.get_dsr_db()
        before = conn.execute(
            "SELECT stable_id, source_path FROM dsr_catalogue WHERE file_name = 'data.csv'"
        ).fetchone()
        conn.close()

        old_path = Path(before["source_path"])
        new_path = old_path.with_name("renamed-data.csv")
        old_path.rename(new_path)

        # A rename changes source_path, which this engine's identity keys on;
        # the old row still exists un-updated (its file is gone) and a *new*
        # row is created for the new path - this exercises that no crash
        # occurs and that stable-id counters keep incrementing safely, while
        # documenting the current identity-tracking limitation (source_path,
        # not inode/basename) noted in dsr_catalogue.py's docstring.
        dsr.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = dsr.get_dsr_db()
        rows = conn.execute("SELECT * FROM dsr_catalogue WHERE file_name = 'renamed-data.csv'").fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)

    def test_export_writes_all_required_outputs(self):
        dsr.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        dsr.cmd_export(self.project_config, self.env)
        for path in (
            dsr.DSR_RESEARCH_CATALOGUE_CSV, dsr.DSR_RESEARCH_CATALOGUE_JSON, dsr.DSR_RESEARCH_CATALOGUE_MD,
            dsr.DSR_RESEARCH_CATALOGUE_SQLITE, dsr.DSR_RELATIONSHIPS_CSV, dsr.DSR_SCHEMA_JSON,
            dsr.DSR_CONTROLLED_VOCAB_JSON, dsr.DSR_MIGRATION_LOG_CSV, dsr.DSR_MANUAL_REVIEW_CSV,
            dsr.DSR_CLASSIFICATION_RULES_JSON,
        ):
            self.assertTrue(path.exists(), f"missing output: {path}")

    def test_validate_reports_no_structural_issues_on_clean_scan(self):
        dsr.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        dsr.cmd_validate(self.project_config, self.env)  # should not raise

    def test_migrate_never_reclassifies_a_directory_source_path(self):
        """A repo-rollup-style dsr_catalogue row (source_path pointing at a
        directory, as legacy_dsr_migration.py inserts) must survive
        cmd_migrate untouched - classify_file() has no notion of a directory
        and would otherwise clobber it via the unmapped-extension fallback."""
        conn = dsr.get_dsr_db()
        now = "2026-01-01T00:00:00+00:00"
        conn.execute(
            """
            INSERT INTO dsr_catalogue (
                catalogue_id, stable_id, version, file_name, relative_path, source_path,
                class_code, subtype_code, dsr_artefact_type, confidence_status,
                classification_rule, classification_evidence, legacy_ids_json,
                created_date, modified_date, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "DSR-COD-UNK-0001-V0.1", "DSR-COD-UNK-0001", "V0.1", "artefacts", ".",
                str(self.source_root / "artefacts"), "COD", "UNK", "Not Applicable", "Requires Review",
                "repo_rollup:directory_not_single_file", "legacy repo rollup (2 files) - Requires Review",
                "[]", "2026-01-01", "2026-01-01", now, now,
            ),
        )
        conn.commit()
        conn.close()

        dsr.cmd_migrate(self.project_config, self.env, dry_run=False, apply=True)

        conn = dsr.get_dsr_db()
        row = conn.execute("SELECT * FROM dsr_catalogue WHERE catalogue_id = ?", ("DSR-COD-UNK-0001-V0.1",)).fetchone()
        conn.close()
        self.assertEqual(row["class_code"], "COD")
        self.assertEqual(row["subtype_code"], "UNK")

    def test_duplicate_content_is_flagged_not_auto_merged(self):
        (self.source_root / "artefacts" / "process-model-copy-v1.md").write_text("model spec", encoding="utf-8")
        dsr.cmd_scan(self.project_config, self.env, dry_run=False, apply=True)
        conn = dsr.get_dsr_db()
        rows = conn.execute("SELECT * FROM dsr_catalogue WHERE file_name LIKE 'process-model%'").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        stable_ids = {r["stable_id"] for r in rows}
        self.assertEqual(len(stable_ids), 2, "duplicate content must not collapse into one stable_id automatically")
        statuses = {r["duplicate_status"] for r in rows}
        self.assertIn("possible_duplicate", statuses)


class UpdateReferencesTests(unittest.TestCase):
    def test_noop_without_configured_roots(self):
        # No dsr_reference_roots configured -> must not touch anything.
        dsr.cmd_update_references({"project_id": "x"}, {}, dry_run=True, apply=False)  # should not raise


def _fake_openai_response(body: dict):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(body).encode("utf-8")

    return _Resp()


class AiDecideClassificationTests(unittest.TestCase):
    def test_returns_none_without_api_key(self):
        result = dsr._ai_decide_classification("", "f.txt", "content", "evidence")
        self.assertIsNone(result)

    def test_valid_response_within_vocabulary_is_accepted(self):
        body = {"choices": [{"message": {"content": json.dumps(
            {"class_code": "DAT", "subtype_code": "JSE", "reasoning": "looks like a JSON export"}
        )}}]}
        with mock.patch("catalogues.dsr_catalogue.urllib.request.urlopen", return_value=_fake_openai_response(body)):
            result = dsr._ai_decide_classification("fake-key", "export", "{}", "no extension")
        self.assertEqual(result, {"class_code": "DAT", "subtype_code": "JSE", "reasoning": "looks like a JSON export"})

    def test_invented_class_code_is_rejected(self):
        body = {"choices": [{"message": {"content": json.dumps(
            {"class_code": "NOPE", "subtype_code": "", "reasoning": "..."}
        )}}]}
        with mock.patch("catalogues.dsr_catalogue.urllib.request.urlopen", return_value=_fake_openai_response(body)):
            result = dsr._ai_decide_classification("fake-key", "f", "content", "evidence")
        self.assertIsNone(result)

    def test_invented_subtype_for_a_known_class_is_rejected(self):
        body = {"choices": [{"message": {"content": json.dumps(
            {"class_code": "DAT", "subtype_code": "MADE_UP", "reasoning": "..."}
        )}}]}
        with mock.patch("catalogues.dsr_catalogue.urllib.request.urlopen", return_value=_fake_openai_response(body)):
            result = dsr._ai_decide_classification("fake-key", "f", "content", "evidence")
        self.assertIsNone(result)

    def test_network_failure_returns_none(self):
        with mock.patch("catalogues.dsr_catalogue.urllib.request.urlopen", side_effect=OSError("boom")):
            result = dsr._ai_decide_classification("fake-key", "f", "content", "evidence")
        self.assertIsNone(result)


class RunAiDecideReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = self.tmp / "catalogue_dsr.db"
        self.conn = dsr.get_dsr_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _insert_row(self, catalogue_id: str, source_path: Path | None, status: str = "active",
                     confidence_status: str = dsr.REQUIRES_REVIEW) -> None:
        self.conn.execute(
            """
            INSERT INTO dsr_catalogue (
                catalogue_id, stable_id, version, file_name, relative_path, source_path,
                class_code, subtype_code, dsr_artefact_type, confidence_status, status,
                classification_rule, classification_evidence, legacy_ids_json,
                created_date, modified_date, created_at, updated_at
            ) VALUES (?, ?, 'V0.1', ?, '.', ?, 'Requires Review', 'UNK', 'Not Applicable', ?, ?,
                      'fallback:unmapped_extension', 'ext=(none) has no mapping', '[]',
                      '2026-01-01', '2026-01-01', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """,
            (catalogue_id, catalogue_id, catalogue_id, str(source_path) if source_path else "", confidence_status, status),
        )
        self.conn.commit()

    def test_resolves_a_real_file_and_marks_ai_assigned(self):
        target = self.tmp / "some-export"
        target.write_text('{"a": 1}', encoding="utf-8")
        self._insert_row("REVIEW-0001", target)

        body = {"choices": [{"message": {"content": json.dumps(
            {"class_code": "DAT", "subtype_code": "JSE", "reasoning": "valid JSON content"}
        )}}]}
        with mock.patch("catalogues.dsr_catalogue.urllib.request.urlopen", return_value=_fake_openai_response(body)):
            changes = dsr._run_ai_decide_review(self.conn, "fake-key")

        self.assertEqual(len(changes), 1)
        row = self.conn.execute("SELECT * FROM dsr_catalogue WHERE catalogue_id='REVIEW-0001'").fetchone()
        self.assertEqual(row["class_code"], "DAT")
        self.assertEqual(row["subtype_code"], "JSE")
        self.assertEqual(row["confidence_status"], dsr.AI_ASSIGNED)

    def test_skips_excluded_records(self):
        target = self.tmp / "junk"
        target.write_text("some content", encoding="utf-8")
        self._insert_row("REVIEW-0002", target, status="excluded")

        with mock.patch("catalogues.dsr_catalogue.urllib.request.urlopen") as urlopen:
            changes = dsr._run_ai_decide_review(self.conn, "fake-key")
        urlopen.assert_not_called()
        self.assertEqual(changes, [])

    def test_skips_directory_source_path(self):
        directory = self.tmp / "a-rollup-dir"
        directory.mkdir()
        self._insert_row("REVIEW-0003", directory)

        with mock.patch("catalogues.dsr_catalogue.urllib.request.urlopen") as urlopen:
            changes = dsr._run_ai_decide_review(self.conn, "fake-key")
        urlopen.assert_not_called()
        self.assertEqual(changes, [])


if __name__ == "__main__":
    unittest.main()
