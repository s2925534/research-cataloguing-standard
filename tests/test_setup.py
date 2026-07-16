#!/usr/bin/env python3
"""Lightweight tests for setup.py: config validation and schema merge.

Stdlib unittest only (no pytest dependency). Run with:
    python3 -m unittest discover tests
or:
    python3 tests/test_setup.py

Exercises the pure functions directly with synthetic fixtures - never reads
or writes this checkout's real instance/ (that's gitignored, per-project
config, not something a test suite should depend on or mutate).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import setup as setup_mod  # noqa: E402


MINIMAL_PROJECT_CONFIG = {
    "project_id": "test_project",
    "project_name": "Test Project",
    "repository_layout": "flat",
    "organisations": ["ACME"],
    "systems": ["ACMESYS"],
    "extended_artefact_types": ["CUSTOM-TYPE"],
    "domain_identifier_fields": ["widget_ids"],
    "research_taxonomies": {"lifecycle_stages": ["plan", "build"]},
    "preview_max_words": 300,
}

MINIMAL_VOCAB_CORE = {
    "unknown_value": "UNKNOWN",
    "core_artefact_types": ["REPORT", "OTHER"],
}

MINIMAL_SCHEMA_CORE = {
    "properties": {
        "source_organisation": {"type": ["string", "null"], "description": "org."},
        "source_system": {"type": ["string", "null"], "description": "sys."},
        "artefact_type": {"type": "string", "description": "artefact."},
        "primary_entity_type": {"type": "string", "enum": ["document", "none"]},
        "domain_identifiers": {"type": "object", "description": "Free-form map."},
        "research_taxonomy": {"type": "object", "description": "Free-form map."},
        "content_preview": {"type": ["string", "null"], "description": "Preview."},
    }
}


class ValidateProjectConfigTests(unittest.TestCase):
    def test_valid_config_has_no_errors(self):
        self.assertEqual(setup_mod.validate_project_config(MINIMAL_PROJECT_CONFIG), [])

    def test_missing_required_key_is_reported(self):
        config = dict(MINIMAL_PROJECT_CONFIG)
        del config["organisations"]
        errors = setup_mod.validate_project_config(config)
        self.assertTrue(any("organisations" in e for e in errors))

    def test_bad_repository_layout_is_reported(self):
        config = dict(MINIMAL_PROJECT_CONFIG, repository_layout="nested")
        errors = setup_mod.validate_project_config(config)
        self.assertTrue(any("repository_layout" in e for e in errors))

    def test_research_taxonomies_must_be_object(self):
        config = dict(MINIMAL_PROJECT_CONFIG, research_taxonomies=["not", "a", "dict"])
        errors = setup_mod.validate_project_config(config)
        self.assertTrue(any("research_taxonomies" in e for e in errors))


class ValidateEnvTests(unittest.TestCase):
    def test_valid_env_has_no_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"SOURCE_DATA_ROOTS": tmp, "OUTPUT_ROOT": tmp}
            self.assertEqual(setup_mod.validate_env(env), [])

    def test_missing_required_key_is_reported(self):
        errors = setup_mod.validate_env({"SOURCE_DATA_ROOTS": "/tmp"})
        self.assertTrue(any("OUTPUT_ROOT" in e for e in errors))

    def test_nonexistent_source_root_is_reported(self):
        env = {"SOURCE_DATA_ROOTS": "/definitely/does/not/exist/anywhere", "OUTPUT_ROOT": "/tmp"}
        errors = setup_mod.validate_env(env)
        self.assertTrue(any("does not exist on disk" in e for e in errors))

    def test_multiple_comma_separated_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"SOURCE_DATA_ROOTS": f"{tmp}, /definitely/does/not/exist", "OUTPUT_ROOT": tmp}
            errors = setup_mod.validate_env(env)
            self.assertEqual(len(errors), 1)


class ParseEnvFileTests(unittest.TestCase):
    def test_parses_key_value_pairs_skips_comments_and_blanks(self):
        content = "\n".join([
            "# a comment",
            "",
            "SOURCE_DATA_ROOTS=/a/b, /c/d",
            "OUTPUT_ROOT = /out ",
            "# OCR_ENABLED=true (disabled for now)",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(content, encoding="utf-8")
            parsed = setup_mod.parse_env_file(env_path)
        self.assertEqual(parsed["SOURCE_DATA_ROOTS"], "/a/b, /c/d")
        self.assertEqual(parsed["OUTPUT_ROOT"], "/out")
        self.assertNotIn("OCR_ENABLED", parsed)

    def test_missing_file_returns_empty_dict(self):
        self.assertEqual(setup_mod.parse_env_file(Path("/no/such/.env")), {})


class GenerateSchemaTests(unittest.TestCase):
    def test_merges_project_organisations_and_systems(self):
        schema = setup_mod.generate_schema(MINIMAL_SCHEMA_CORE, MINIMAL_VOCAB_CORE, MINIMAL_PROJECT_CONFIG)
        self.assertIn("ACME", schema["properties"]["source_organisation"]["enum"])
        self.assertIn("UNKNOWN", schema["properties"]["source_organisation"]["enum"])
        self.assertIn("ACMESYS", schema["properties"]["source_system"]["enum"])
        self.assertIn("NA", schema["properties"]["source_system"]["enum"])

    def test_merges_core_and_extended_artefact_types(self):
        schema = setup_mod.generate_schema(MINIMAL_SCHEMA_CORE, MINIMAL_VOCAB_CORE, MINIMAL_PROJECT_CONFIG)
        types = schema["properties"]["artefact_type"]["enum"]
        self.assertIn("REPORT", types)
        self.assertIn("CUSTOM-TYPE", types)

    def test_does_not_mutate_input_schema_core(self):
        # generate_schema deep-copies schema_core - a caller re-running setup
        # (or a test suite running twice) must not see prior runs' project
        # data leak into what should be the pristine core template.
        before = json.dumps(MINIMAL_SCHEMA_CORE, sort_keys=True)
        setup_mod.generate_schema(MINIMAL_SCHEMA_CORE, MINIMAL_VOCAB_CORE, MINIMAL_PROJECT_CONFIG)
        after = json.dumps(MINIMAL_SCHEMA_CORE, sort_keys=True)
        self.assertEqual(before, after)

    def test_research_taxonomy_keys_documented(self):
        schema = setup_mod.generate_schema(MINIMAL_SCHEMA_CORE, MINIMAL_VOCAB_CORE, MINIMAL_PROJECT_CONFIG)
        self.assertIn("lifecycle_stages", schema["properties"]["research_taxonomy"]["description"])

    def test_against_real_templates(self):
        # Integration-style check with this repo's actual committed
        # templates, so a change to schema_core.json/vocabularies_core.json
        # that breaks the merge is caught even if the unit fixtures above
        # still pass.
        templates_dir = Path(__file__).resolve().parent.parent / "templates"
        schema_core = json.loads((templates_dir / "schema_core.json").read_text(encoding="utf-8"))
        vocab_core = json.loads((templates_dir / "vocabularies_core.json").read_text(encoding="utf-8"))
        schema = setup_mod.generate_schema(schema_core, vocab_core, MINIMAL_PROJECT_CONFIG)
        self.assertIn("ACME", schema["properties"]["source_organisation"]["enum"])
        header = setup_mod.csv_header_from_schema(schema)
        self.assertIn("catalogue_id", header.split(","))


class CsvHeaderFromSchemaTests(unittest.TestCase):
    def test_returns_comma_joined_property_names(self):
        schema = {"properties": {"a": {}, "b": {}, "c": {}}}
        self.assertEqual(setup_mod.csv_header_from_schema(schema), "a,b,c")


if __name__ == "__main__":
    unittest.main()
