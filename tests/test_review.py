#!/usr/bin/env python3
"""Lightweight tests for review.py: block/sentence diff model and merge.

Stdlib unittest only. Run with:
    python3 -m unittest discover tests
or:
    python3 tests/test_review.py

Exercises the pure diff/reconstruct functions directly; no server/network
involved.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import review  # noqa: E402


class TestBlockSplitting(unittest.TestCase):
    def test_split_blocks_on_blank_lines(self):
        text = "Para one.\n\nPara two.\n\n\nPara three."
        self.assertEqual(review.split_blocks(text), ["Para one.", "Para two.", "Para three."])

    def test_classify_block_types(self):
        self.assertEqual(review.classify_block("## Heading"), "heading")
        self.assertEqual(review.classify_block("```\ncode\n```"), "code")
        self.assertEqual(review.classify_block("> quoted"), "blockquote")
        self.assertEqual(review.classify_block("- item one\n- item two"), "list")
        self.assertEqual(
            review.classify_block("| a | b |\n|---|---|\n| 1 | 2 |"), "table"
        )
        self.assertEqual(review.classify_block("Just a plain sentence."), "paragraph")


class TestDiffModelNoChange(unittest.TestCase):
    def test_identical_text_has_no_changes(self):
        text = "# Title\n\nSome unchanged paragraph here."
        blocks = review.build_diff_model(text, text)
        self.assertEqual(review.count_changes(blocks), 0)
        self.assertTrue(all(b.get("fixed") for b in blocks))
        self.assertEqual(review.reconstruct(blocks, {}), text + "\n")

    def test_every_block_has_a_stable_id_even_when_unchanged(self):
        text = "# Title\n\nSome unchanged paragraph here."
        blocks = review.build_diff_model(text, text)
        ids = [b["id"] for b in blocks]
        self.assertTrue(all(ids))
        self.assertEqual(len(ids), len(set(ids)), "block ids must be unique")


class TestBlockOverride(unittest.TestCase):
    def test_manual_override_on_an_unchanged_block_wins(self):
        text = "# Title\n\nOriginal untouched sentence."
        blocks = review.build_diff_model(text, text)
        target = next(b for b in blocks if b.get("fixed") and b["block_type"] == "paragraph")
        result = review.reconstruct(blocks, {}, {target["id"]: "Hand-rewritten sentence."})
        self.assertIn("Hand-rewritten sentence.", result)
        self.assertNotIn("Original untouched sentence.", result)

    def test_manual_override_takes_precedence_over_a_pending_change(self):
        old = "Intro.\n\nThe rule uses three fields."
        new = "Intro.\n\nThe rule uses five fields."
        blocks = review.build_diff_model(old, new)
        item_id = next(
            s["id"]
            for b in blocks
            if b.get("type") == "sentence_group"
            for s in b["subsegments"]
            if not s["fixed"]
        )
        group_id = next(b["id"] for b in blocks if b.get("type") == "sentence_group")
        result = review.reconstruct(
            blocks,
            {item_id: {"action": "accept"}},
            {group_id: "Completely custom paragraph text."},
        )
        self.assertIn("Completely custom paragraph text.", result)
        self.assertNotIn("five fields", result)
        self.assertNotIn("three fields", result)

    def test_empty_string_override_removes_the_block(self):
        text = "# Title\n\nParagraph to delete entirely."
        blocks = review.build_diff_model(text, text)
        target = next(b for b in blocks if b.get("fixed") and b["block_type"] == "paragraph")
        result = review.reconstruct(blocks, {}, {target["id"]: ""})
        self.assertNotIn("Paragraph to delete entirely.", result)


class TestInsertions(unittest.TestCase):
    def test_insert_after_a_block(self):
        text = "# Title\n\nFirst paragraph.\n\nSecond paragraph."
        blocks = review.build_diff_model(text, text)
        first = next(b for b in blocks if b.get("fixed") and "First paragraph" in b["text"])
        result = review.reconstruct(blocks, {}, {}, {first["id"]: ["A brand new inserted block."]})
        lines = result.strip().split("\n\n")
        self.assertEqual(
            lines,
            ["# Title", "First paragraph.", "A brand new inserted block.", "Second paragraph."],
        )

    def test_insert_at_start(self):
        text = "Only paragraph."
        blocks = review.build_diff_model(text, text)
        result = review.reconstruct(blocks, {}, {}, {"__start__": ["Inserted first."]})
        self.assertEqual(result, "Inserted first.\n\nOnly paragraph.\n")

    def test_multiple_insertions_after_same_block_preserve_order(self):
        text = "Only paragraph."
        blocks = review.build_diff_model(text, text)
        only = blocks[0]
        result = review.reconstruct(blocks, {}, {}, {only["id"]: ["Second.", "Third."]})
        self.assertEqual(result, "Only paragraph.\n\nSecond.\n\nThird.\n")


class TestSentenceLevelReplace(unittest.TestCase):
    def setUp(self):
        self.old = "Intro paragraph.\n\nThe rule uses three fields. It is fast."
        self.new = "Intro paragraph.\n\nThe rule uses five fields. It is fast."
        self.blocks = review.build_diff_model(self.old, self.new)

    def test_detects_single_sentence_change(self):
        self.assertEqual(review.count_changes(self.blocks), 1)
        group = next(b for b in self.blocks if b.get("type") == "sentence_group")
        changed = [s for s in group["subsegments"] if not s["fixed"]]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["kind"], "sentence_replace")
        self.assertEqual(changed[0]["original"], "The rule uses three fields.")
        self.assertEqual(changed[0]["suggested"], "The rule uses five fields.")

    def test_default_keeps_original(self):
        self.assertEqual(review.reconstruct(self.blocks, {}), self.old + "\n")

    def test_accept_applies_suggestion(self):
        item_id = next(
            s["id"]
            for b in self.blocks
            if b.get("type") == "sentence_group"
            for s in b["subsegments"]
            if not s["fixed"]
        )
        result = review.reconstruct(self.blocks, {item_id: {"action": "accept"}})
        self.assertEqual(result, self.new + "\n")

    def test_custom_text_overrides_both(self):
        item_id = next(
            s["id"]
            for b in self.blocks
            if b.get("type") == "sentence_group"
            for s in b["subsegments"]
            if not s["fixed"]
        )
        result = review.reconstruct(
            self.blocks, {item_id: {"action": "custom", "text": "The rule uses four fields."}}
        )
        self.assertIn("The rule uses four fields.", result)


class TestBlockInsertAndDelete(unittest.TestCase):
    def test_block_insert_defaults_to_omitted(self):
        old = "Para one."
        new = "Para one.\n\nBrand new paragraph."
        blocks = review.build_diff_model(old, new)
        insert_items = [b for b in blocks if not b.get("fixed") and b.get("kind") == "block_insert"]
        self.assertEqual(len(insert_items), 1)
        # default (no decision) keeps "original" for an insert, i.e. omits it
        self.assertEqual(review.reconstruct(blocks, {}), old + "\n")
        item_id = insert_items[0]["id"]
        accepted = review.reconstruct(blocks, {item_id: {"action": "accept"}})
        self.assertEqual(accepted, new + "\n")

    def test_block_delete_defaults_to_kept(self):
        old = "Para one.\n\nPara two to remove."
        new = "Para one."
        blocks = review.build_diff_model(old, new)
        delete_items = [b for b in blocks if not b.get("fixed") and b.get("kind") == "block_delete"]
        self.assertEqual(len(delete_items), 1)
        self.assertEqual(review.reconstruct(blocks, {}), old + "\n")
        item_id = delete_items[0]["id"]
        accepted = review.reconstruct(blocks, {item_id: {"action": "accept"}})
        self.assertEqual(accepted, new + "\n")


class TestTableTreatedAsAtomicBlock(unittest.TestCase):
    def test_table_change_is_one_block_level_item_not_sentence_split(self):
        old = "| a | b |\n|---|---|\n| 1 | 2 |"
        new = "| a | b |\n|---|---|\n| 1 | 3 |"
        blocks = review.build_diff_model(old, new)
        self.assertEqual(review.count_changes(blocks), 1)
        item = next(b for b in blocks if not b.get("fixed"))
        self.assertEqual(item["kind"], "block_replace")
        self.assertEqual(item["original"], old)
        self.assertEqual(item["suggested"], new)


class TestSectionTracking(unittest.TestCase):
    def test_change_item_carries_nearest_preceding_heading(self):
        old = "# Doc\n\n## Section A\n\nOld sentence here."
        new = "# Doc\n\n## Section A\n\nNew sentence here."
        blocks = review.build_diff_model(old, new)
        group = next(b for b in blocks if b.get("type") == "sentence_group")
        self.assertEqual(group["section"], "## Section A")
        # regression check: the frontend renders each subsegment's own
        # "section" field (not just the wrapping group's), so it must be
        # set on every changed subsegment too, not only on the group.
        changed = [s for s in group["subsegments"] if not s["fixed"]]
        self.assertTrue(changed)
        for sub in changed:
            self.assertEqual(sub["section"], "## Section A")


class TestStripCatalogueMarkers(unittest.TestCase):
    def test_ce_marker_with_annotation_removed(self):
        text = "as shown (Panayides & Song, 2009 [CE-0124 — see note, weak support])."
        self.assertEqual(
            review.strip_catalogue_markers(text),
            "as shown (Panayides & Song, 2009).",
        )

    def test_plain_ce_marker_removed(self):
        text = "supported (Mankins, 1995 [CE-0063]; Peltz, 2002 [CE-0052])."
        self.assertEqual(
            review.strip_catalogue_markers(text),
            "supported (Mankins, 1995; Peltz, 2002).",
        )

    def test_internal_evidence_marker_removed(self):
        text = "names this field directly [INTERNAL EVIDENCE — DSR-REF-GRY-0409], while continuing."
        self.assertEqual(
            review.strip_catalogue_markers(text),
            "names this field directly, while continuing.",
        )

    def test_new_reference_flag_removed(self):
        text = (
            "World Customs Organization. (2021). *SAFE Framework.* WCO. "
            "[NEW — added 2026-07-20, flagged for review. See CE-0070.]"
        )
        self.assertEqual(
            review.strip_catalogue_markers(text),
            "World Customs Organization. (2021). *SAFE Framework.* WCO.",
        )

    def test_standard_uncertainty_markers_removed(self):
        for marker in review.STANDARD_UNCERTAINTY_MARKERS:
            text = f"a claim needing evidence [{marker}] here."
            self.assertNotIn(marker, review.strip_catalogue_markers(text))

    def test_unrelated_bracket_text_untouched(self):
        text = "See the appendix [Appendix A] for details, or [1] in the numbered list."
        self.assertEqual(review.strip_catalogue_markers(text), text)


class TestFindPdfEngine(unittest.TestCase):
    def test_returns_none_or_a_known_candidate(self):
        engine = review.find_pdf_engine()
        self.assertTrue(engine is None or engine in review.PDF_ENGINE_CANDIDATES)


if __name__ == "__main__":
    unittest.main()
