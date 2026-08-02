#!/usr/bin/env python3
"""Sentence/block-level diff-review tool for two text/Markdown files.

Unlike a unified +/- diff, this renders each change as its own reviewable
card (original vs suggested) and lets a human choose, per change: keep the
original, accept the suggestion, or type a manual replacement. Nothing is
applied until the human decides — an undecided change defaults to keeping
the original.

Usage:
    python3 review.py <original.md> <proposed.md> [--output OUT] [--port 8765]

`proposed.md` is treated as the file that will eventually be overwritten
with the reviewed result (default `--output`); a timestamped backup of
whatever is at that path is written before every save.
"""
from __future__ import annotations

import argparse
import base64
import difflib
import json
import mimetypes
import re
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

STATIC_DIR = Path(__file__).resolve().parent / "review_ui"
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Candidate PDF-rendering backends for `pandoc --pdf-engine=...`, checked in
# this order (first one found on PATH wins). Kept as a list rather than
# assuming one specific engine, since which of these is installed varies by
# machine and none is a hard dependency of this project.
PDF_ENGINE_CANDIDATES = ["wkhtmltopdf", "weasyprint", "pdflatex", "xelatex", "lualatex"]

# Markers this project's citation/evidence-tracking convention embeds inline
# (see AGENTS.md/CLAUDE.md's "standard uncertainty markers", and review.py's
# own CITATION_ENTRY_FIELDS above) — stripped for a "clean", submission-ready
# export. Exact-match, no wildcard, so unrelated bracketed text is untouched.
STANDARD_UNCERTAINTY_MARKERS = [
    "UNVERIFIED_SOURCE", "NEEDS_SOURCE", "CLAIM_NEEDS_REVISION",
    "PAGE_NEEDED", "QUOTE_NEEDED", "INTERNAL_EVIDENCE_NEEDED",
]
_CE_MARKER_RE = re.compile(r"\[CE-\d{3,6}(?:\s*[—-][^\]]*)?\]")
_INTERNAL_EVIDENCE_MARKER_RE = re.compile(r"\[INTERNAL[ _]EVIDENCE\s*[—-][^\]]*\]", re.IGNORECASE)
_NEW_REFERENCE_MARKER_RE = re.compile(r"\[NEW\s*[—-]\s*added[^\]]*\]", re.IGNORECASE)
_STANDARD_MARKER_RE = re.compile(
    r"\[(?:" + "|".join(re.escape(m) for m in STANDARD_UNCERTAINTY_MARKERS) + r")\]"
)


def strip_catalogue_markers(text: str) -> str:
    """Remove this project's inline catalogue/citation-tracking markers —
    [CE-####] (with any trailing " — ..." annotation), [INTERNAL EVIDENCE -
    ...], [NEW - added ...] reference-list flags, and the standard
    uncertainty markers — for a "clean", submission-ready read of the
    document. Best-effort only: removing a bracketed marker can leave a
    stray space before the punctuation that followed it (e.g. "text [CE-1],
    more" -> "text , more"), so a light whitespace-before-punctuation
    cleanup runs afterward. Not a proofreading tool - review the result
    before submitting anywhere.
    """
    text = _CE_MARKER_RE.sub("", text)
    text = _INTERNAL_EVIDENCE_MARKER_RE.sub("", text)
    text = _NEW_REFERENCE_MARKER_RE.sub("", text)
    text = _STANDARD_MARKER_RE.sub("", text)
    text = re.sub(r"[ \t]+([,.;:!?)\]])", r"\1", text)
    text = re.sub(r"([ \t])[ \t]+", r"\1", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
    return text


_HTML_FIGURE_RE = re.compile(
    r'<figure>\s*<img\s+src="([^"]+)"[^>]*/>\s*<figcaption><p>(.*?)</p></figcaption>\s*</figure>',
    re.DOTALL,
)


def convert_html_figures_for_pandoc(text: str) -> str:
    """Rewrite this project's `<figure><img src="..." /><figcaption><p>...</p>
    </figcaption></figure>` blocks into native `![caption](src)` markdown image
    syntax, for the docx/pdf export path only.

    Why this exists: pandoc's markdown reader turns raw HTML blocks it can't
    represent in a non-HTML target (docx, pdf) into RawBlock "html" nodes,
    which its docx/pdf writers silently drop — the image never gets embedded,
    though surrounding plain text (including, confusingly, the figcaption's
    own text) survives. Verified directly: a minimal two-image test document,
    one native-markdown image and one HTML `<figure>`, round-tripped through
    `pandoc ... -o out.docx` with only the native-markdown image present in
    the output's `word/document.xml`. Native `![caption](src)` markdown does
    not have this problem — pandoc's `implicit_figures` extension (on by
    default) turns an image-only paragraph into a proper embedded figure with
    a caption, confirmed the same way.

    Markdown-format exports intentionally skip this rewrite — the original
    HTML is valid there and some consumers may prefer it as-is.
    """
    def _replace(match: re.Match[str]) -> str:
        src, caption = match.group(1), match.group(2)
        return f"![{caption}]({src})"

    return _HTML_FIGURE_RE.sub(_replace, text)


def find_pdf_engine() -> str | None:
    for candidate in PDF_ENGINE_CANDIDATES:
        if shutil.which(candidate):
            return candidate
    return None

SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")
TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
LIST_ITEM_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s+")

# Field labels used by thesis/evidence_maps/citation_evidence_register.md's
# "Entry format" template. Kept here (not auto-discovered) so a stray label
# added to the register doesn't silently vanish from parsed output — add it
# to this list too if the template grows a new field.
CITATION_ENTRY_FIELDS = [
    "Working document", "Current section", "Claim supported",
    "Inline citation used", "Source type", "Source file",
    "Full APA 7 reference", "Search method used", "Search terms used",
    "Verbatim quotation from source", "Page number or location",
    "How the quote supports the claim", "Confidence", "Review status", "Notes",
]
_CITATION_HEADER_RE = re.compile(r"^### (CE-\d{4})\s*$", re.MULTILINE)
_CITATION_FIELD_RE = re.compile(
    r"^(" + "|".join(re.escape(f) for f in CITATION_ENTRY_FIELDS) + r"):\s*$",
    re.MULTILINE,
)


def parse_citation_register(path: Path) -> dict[str, dict[str, str]]:
    """Parse a citation_evidence_register.md-style file into {CE-id: {field: value}}.

    Tolerant by design: an entry missing a field just omits that key rather
    than erroring, since this only drives review-UI tooltips, not the
    citation work itself — a parsing gap here should never block `--output`
    from being written.
    """
    text = path.read_text(encoding="utf-8")
    headers = list(_CITATION_HEADER_RE.finditer(text))
    entries: dict[str, dict[str, str]] = {}
    for idx, header in enumerate(headers):
        ce_id = header.group(1)
        body_start = header.end()
        body_end = headers[idx + 1].start() if idx + 1 < len(headers) else len(text)
        body = text[body_start:body_end]
        fields = list(_CITATION_FIELD_RE.finditer(body))
        parsed: dict[str, str] = {}
        for fidx, fmatch in enumerate(fields):
            label = fmatch.group(1)
            start = fmatch.end()
            end = fields[fidx + 1].start() if fidx + 1 < len(fields) else len(body)
            value = body[start:end].strip()
            # The register's own trailing "add new entries below" instruction
            # sits right after the last entry's last field with no field
            # label of its own, so it would otherwise get swept into that
            # field's value (typically "Notes") for whichever entry is last.
            value = re.split(r"\n_\(Add new `CE-####`", value)[0].strip()
            if value:
                parsed[label] = value
        if parsed:
            entries[ce_id] = parsed
    return entries


_CONTRADICTION_PHRASES = [
    "claim_needs_revision", "claim not supported", "cuts against",
    "contradicts", "cuts against its own claim", "false support",
]

_FLAG_PHRASES = [
    "weak support", "not yet inserted", "not yet applied",
    "pending physical library scan", "book review", "not the book",
    "needs_source", "unverified_source", "year discrepancy",
    "citation-identity fix", "orphan citation", "no matching source",
    "still open", "unresolved", "still unresolved", "not yet checked",
    "genuinely unresolved",
]

# Below this many entries sharing (near-)identical "Claim supported" text,
# redundancy isn't worth flagging — three separate sources backing one
# sentence is normal, not padding.
_REDUNDANCY_GROUP_MIN = 4


def _normalize_claim(claim: str) -> str:
    """Loose key for grouping CE entries that support the same sentence:
    lowercased, punctuation/whitespace collapsed. Entries citing the same
    sentence usually quote it near-verbatim in 'Claim supported', so this
    catches the common case without needing an explicit shared identifier."""
    return re.sub(r"[^a-z0-9]+", " ", claim.lower()).strip()


def derive_recommendation(entry: dict[str, str]) -> dict[str, str]:
    """Best-effort accept/flag/remove verdict for one CE-#### entry,
    synthesised from fields already written by whoever verified the citation
    (Confidence, Review status, Notes, How the quote supports the claim) —
    not a fresh judgement, just surfacing the judgement already recorded in
    those fields as a single actionable label, so a reviewer doesn't have to
    read all five fields to find out "is this one settled or does it need a
    second look."

    Four verdicts:
      accept  - clean fit, no action needed.
      flag    - incomplete or uncertain (pending source, no confidence
                recorded, low confidence) but not necessarily wrong -
                recommend a follow-up, not necessarily removing it.
      remove  - the source actively contradicts or fails to support the
                claim it's cited for (a CLAIM_NEEDS_REVISION-type finding) -
                recommend dropping the citation from the sentence, not just
                a follow-up, since keeping a source that argues the other
                way is worse than citing nothing.
      info    - corroboration only, doesn't back a specific claim in the
                document text.
    A fifth pseudo-verdict, 'redundant', is applied afterward by
    enrich_citations_with_recommendations() once every entry's claim text
    can be cross-checked against every other entry's — see there.

    Also returns 'status_label': a replacement for the register's own literal
    "Review status: Needs review" text, which every entry carries regardless
    of how solid it actually is (that field just tracks "has a human looked
    at this in the browser yet", not "is this any good") — left as-is it
    makes 150+ good citations look exactly as uncertain as the handful that
    actually need a second look. status_label instead says what the entry's
    own Confidence/Notes fields actually imply: a clean high/medium-confidence
    fit reads as settled, not alarming; only the genuinely flagged ones keep
    "Needs review" framing, now with the specific reason attached.
    """
    confidence = (entry.get("Confidence") or "").strip().lower()
    review_status = entry.get("Review status") or ""
    notes = entry.get("Notes") or ""
    how_supports = entry.get("How the quote supports the claim") or ""
    haystack = " ".join([review_status, notes, how_supports]).lower()
    review_status_lower = review_status.strip().lower()

    if "no action needed" in review_status_lower or "informational" in review_status_lower:
        return {
            "verdict": "info",
            "reason": "Informational corroboration only — no claim change depends on this.",
            "status_label": "Informational only — no action needed",
        }

    # A human (or an AI acting on the human's explicit instruction) has
    # already reviewed this entry and recorded a settled outcome — trust
    # that over the phrase-matching below, which otherwise keeps flagging
    # entries whose *historical* Notes text still describes a since-fixed
    # problem (e.g. a note explaining a citation was removed because it
    # once triggered a CLAIM_NEEDS_REVISION finding will itself contain
    # "claim_needs_revision" and get misread as a live contradiction).
    if review_status_lower.startswith(("resolved", "accepted", "verified")):
        return {
            "verdict": "accept",
            "reason": "Marked Resolved/Accepted/Verified in the register — treated as settled.",
            "status_label": review_status.strip(),
        }

    contradiction_hit = next((p for p in _CONTRADICTION_PHRASES if p in haystack), None)
    if contradiction_hit:
        reason = (
            f"The entry's own notes say the source cuts against or fails to support the claim "
            f"(matched: “{contradiction_hit}”) — recommend removing this citation rather than just flagging it, "
            "since a source arguing the other way is worse than no source at all."
        )
        return {"verdict": "remove", "reason": reason, "status_label": f"Recommend removal — {contradiction_hit}"}

    hit = next((p for p in _FLAG_PHRASES if p in haystack), None)
    if hit:
        reason = f"The entry's own notes flag a concern (matched: “{hit}”) — worth a second look before relying on this."
        return {"verdict": "flag", "reason": reason, "status_label": f"Needs review — {hit}"}

    # startswith, not ==: several entries record confidence as e.g. "High
    # (for the 269 figure, as directly counted); the ..." — a qualified
    # rating, not a bare word — and should still be read as their leading word.
    if confidence.startswith("low"):
        return {
            "verdict": "flag",
            "reason": "Recorded as Low confidence — a weak or generic fit even though a source exists.",
            "status_label": "Needs review — low confidence, weak/generic fit",
        }
    if confidence.startswith("high"):
        return {
            "verdict": "accept",
            "reason": "High confidence, clean fit — recommend accepting as-is.",
            "status_label": "High confidence — good fit, no action needed",
        }
    if confidence.startswith("medium"):
        return {
            "verdict": "accept",
            "reason": "Medium confidence, reasonable supplementary support — recommend accepting, though it's not as strong as a High-confidence source.",
            "status_label": "Medium confidence — solid supplementary support",
        }
    return {
        "verdict": "flag",
        "reason": "No confidence rating recorded for this entry — worth checking before relying on it.",
        "status_label": "Needs review — no confidence rating recorded",
    }


def enrich_citations_with_recommendations(citations: dict[str, dict[str, str]]) -> None:
    """Mutates each entry in place, adding a synthetic '_recommendation' key
    (verdict + reason) alongside the fields parsed from the register file.
    Prefixed with an underscore so it can never collide with a real field
    label from CITATION_ENTRY_FIELDS.

    Two passes: first each entry gets an independent accept/flag/remove/info
    verdict (derive_recommendation, entry in isolation). Second, a redundancy
    check across entries that cite the *same* claim — if a sentence already
    has several strong (High-confidence, non-flagged) citations, a weaker one
    piled onto the same claim is padding, not support, and gets relabelled
    'redundant' (its own verdict wins if it was already 'remove'; a merely
    weak/uncertain 'flag' or a plain 'accept' can both be redundant).
    """
    for entry in citations.values():
        entry["_recommendation"] = derive_recommendation(entry)

    groups: dict[str, list[dict]] = {}
    for entry in citations.values():
        claim = _normalize_claim(entry.get("Claim supported") or "")
        if claim:
            groups.setdefault(claim, []).append(entry)

    for group in groups.values():
        if len(group) < _REDUNDANCY_GROUP_MIN:
            continue
        strong = [e for e in group if e["_recommendation"]["verdict"] == "accept"
                  and (e.get("Confidence") or "").strip().lower().startswith("high")]
        for entry in group:
            rec = entry["_recommendation"]
            if rec["verdict"] in ("remove", "info"):
                continue  # a real contradiction is worse than redundant; informational entries aren't "support" to begin with
            if entry in strong:
                continue  # a High-confidence, clean-fit citation isn't "redundant" just because company exists
            other_strong = [e for e in strong if e is not entry]
            if len(other_strong) < 2:
                continue
            count = len(other_strong)
            # Advisory, not a directive: this is a much weaker signal than an
            # actual contradiction (verdict "remove"), so it's phrased as
            # something to weigh, not something to act on automatically —
            # a co-cited standard, for instance, may deliberately be there to
            # ground terminology rather than to "support the claim" the same
            # way a paper does, even if it looks numerically redundant here.
            reason = (
                f"This exact claim already has {count} other High-confidence citation(s) attached to it. "
                "Not necessarily wrong to keep — e.g. a standard cited alongside several papers is often "
                "doing a different job (grounding terminology) than piling on redundant support — but worth "
                "a quick judgement call on whether this one is pulling its weight here."
            )
            entry["_recommendation"] = {
                "verdict": "redundant",
                "reason": reason,
                "status_label": f"Worth a look — {count} other strong citation(s) already on this exact claim",
            }


def _write_followup_section(lines: list[str], heading: str, items: dict[str, dict], citations: dict) -> None:
    if not items:
        return
    lines.append(f"## {heading} ({len(items)})")
    lines.append("")
    for key, d in sorted(items.items()):
        entry = citations.get(key, {})
        lines.append(f"### {key}")
        lines.append("")
        if entry.get("Claim supported"):
            lines.append(f"**Claim:** {entry['Claim supported']}")
        if entry.get("Confidence"):
            lines.append(f"**Confidence on record:** {entry['Confidence']}")
        rec = entry.get("_recommendation") or {}
        if rec.get("reason"):
            lines.append(f"**Tool's recommendation was:** {rec['reason']}")
        note = (d or {}).get("note")
        if note:
            lines.append(f"**Your note:** {note}")
        decided_at = (d or {}).get("decided_at")
        if decided_at:
            lines.append(f"**Decided at:** {decided_at}")
        lines.append("")


def write_followup_report(
    report_path: Path,
    citation_decisions: dict[str, dict],
    citations: dict[str, dict[str, str]],
) -> None:
    """(Re)write a standalone markdown report of every citation marker
    explicitly flagged or recommended for removal during review, so "what
    still needs a new source, a second look, or should come out" is
    answerable without re-opening the review UI or re-reading the whole
    register. Fully regenerated each save (not appended) since decisions
    can change or be reversed later — this file always reflects current
    state, not history.
    """
    flagged = {k: d for k, d in citation_decisions.items() if (d or {}).get("decision") == "flag"}
    removals = {k: d for k, d in citation_decisions.items() if (d or {}).get("decision") == "remove"}
    lines = [
        "# Citation Review Follow-Ups",
        "",
        f"_Auto-generated by review.py on {time.strftime('%Y-%m-%d %H:%M:%S')} "
        "— regenerated on every save, not appended to. Reflects current "
        "review-tool decisions only; see citation_evidence_register.md for "
        "the full evidence record._",
        "",
    ]
    if not flagged and not removals:
        lines.append("Nothing currently flagged or recommended for removal.")
    else:
        _write_followup_section(lines, "Recommended for removal", removals, citations)
        _write_followup_section(lines, "Flagged for follow-up", flagged, citations)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def split_blocks(text: str) -> list[str]:
    """Split on blank lines into paragraph/heading/list/table/code blocks."""
    text = text.replace("\r\n", "\n").strip("\n")
    if not text:
        return []
    raw = re.split(r"\n\s*\n", text)
    return [b for b in raw if b.strip() != ""]


def classify_block(block: str) -> str:
    lines = block.strip("\n").split("\n")
    first = lines[0].strip()
    if first.startswith("#"):
        return "heading"
    if first.startswith("```"):
        return "code"
    if first.startswith(">"):
        return "blockquote"
    if len(lines) >= 2 and "|" in first and TABLE_SEPARATOR_RE.match(lines[1]):
        return "table"
    if LIST_ITEM_RE.match(first):
        return "list"
    return "paragraph"


def split_sentences(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    return [p.strip() for p in SENTENCE_SPLIT_RE.split(text) if p.strip()]


def _new_id_factory():
    counter = {"n": 0}

    def next_id() -> str:
        counter["n"] += 1
        return f"c{counter['n']}"

    return next_id


def _sentence_group(old_para: str, new_para: str, section: str | None, next_id) -> dict:
    old_sents = split_sentences(old_para)
    new_sents = split_sentences(new_para)
    sm = difflib.SequenceMatcher(None, old_sents, new_sents, autojunk=False)
    subsegments = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            subsegments.append({"fixed": True, "text": " ".join(old_sents[i1:i2])})
        elif tag == "replace":
            subsegments.append({
                "fixed": False, "id": next_id(), "kind": "sentence_replace", "section": section,
                "original": " ".join(old_sents[i1:i2]),
                "suggested": " ".join(new_sents[j1:j2]),
            })
        elif tag == "delete":
            subsegments.append({
                "fixed": False, "id": next_id(), "kind": "sentence_delete", "section": section,
                "original": " ".join(old_sents[i1:i2]), "suggested": None,
            })
        elif tag == "insert":
            subsegments.append({
                "fixed": False, "id": next_id(), "kind": "sentence_insert", "section": section,
                "original": None, "suggested": " ".join(new_sents[j1:j2]),
            })
    return {
        "id": next_id(), "fixed": False, "type": "sentence_group",
        "section": section, "subsegments": subsegments,
    }


def build_diff_model(old_text: str, new_text: str) -> list[dict]:
    """Return an ordered list of block dicts describing the reviewable diff.

    Every top-level element carries an "id" — even unchanged ("fixed")
    blocks — so the UI can offer a manual free-text override on *any* block,
    not just the ones the diff flagged as changed. An element is either
    `{"id", "fixed": True, "text", "section", "block_type"}` or a changed
    unit: `{"id", "fixed": False, "type": "change", "kind", "section",
    "original", "suggested"}` for a whole-block change, or `{"id", "fixed":
    False, "type": "sentence_group", "section", "subsegments": [...]}` for a
    changed paragraph broken down sentence-by-sentence (each subsegment
    following the same fixed/change shape, minus "type", plus its own "id").
    """
    old_blocks = split_blocks(old_text)
    new_blocks = split_blocks(new_text)
    sm = difflib.SequenceMatcher(None, old_blocks, new_blocks, autojunk=False)
    next_id = _new_id_factory()
    blocks: list[dict] = []
    current_section = None

    def track_section(block_text: str, block_type: str) -> str | None:
        nonlocal current_section
        if block_type == "heading":
            current_section = block_text.strip().split("\n", 1)[0].strip()
        return current_section

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                btext = old_blocks[i1 + k]
                btype = classify_block(btext)
                sec = track_section(btext, btype)
                blocks.append({
                    "id": next_id(), "fixed": True, "text": btext,
                    "section": sec, "block_type": btype,
                })
        elif tag == "replace":
            old_group = old_blocks[i1:i2]
            new_group = new_blocks[j1:j2]
            if (
                len(old_group) == 1
                and len(new_group) == 1
                and classify_block(old_group[0]) == "paragraph"
                and classify_block(new_group[0]) == "paragraph"
            ):
                sec = track_section(new_group[0], "paragraph")
                blocks.append(_sentence_group(old_group[0], new_group[0], sec, next_id))
            else:
                sec = track_section((new_group or old_group)[0], "block")
                blocks.append({
                    "fixed": False, "type": "change", "kind": "block_replace",
                    "id": next_id(), "section": sec,
                    "original": "\n\n".join(old_group) if old_group else None,
                    "suggested": "\n\n".join(new_group) if new_group else None,
                })
        elif tag == "delete":
            old_group = old_blocks[i1:i2]
            sec = track_section(old_group[0], "block")
            blocks.append({
                "fixed": False, "type": "change", "kind": "block_delete",
                "id": next_id(), "section": sec,
                "original": "\n\n".join(old_group), "suggested": None,
            })
        elif tag == "insert":
            new_group = new_blocks[j1:j2]
            sec = track_section(new_group[0], "block")
            blocks.append({
                "fixed": False, "type": "change", "kind": "block_insert",
                "id": next_id(), "section": sec,
                "original": None, "suggested": "\n\n".join(new_group),
            })
    return blocks


def count_changes(blocks: list[dict]) -> int:
    total = 0
    for block in blocks:
        if block.get("fixed"):
            continue
        if block.get("type") == "sentence_group":
            total += sum(1 for s in block["subsegments"] if not s.get("fixed"))
        else:
            total += 1
    return total


def _resolve(item: dict, decisions: dict, default_action: str = "original") -> str:
    decision = decisions.get(item["id"]) if item.get("id") else None
    action = (decision or {}).get("action") or ("accept" if default_action == "suggested" else "keep")
    if action == "accept":
        return item["suggested"] or ""
    if action == "custom":
        return decision.get("text", "") or ""
    return item["original"] or ""


def reconstruct(
    blocks: list[dict],
    decisions: dict,
    block_overrides: dict | None = None,
    insertions: dict | None = None,
    default_action: str = "original",
) -> str:
    """Rebuild the final document text given per-item decisions, an optional
    manual free-text override per whole top-level block (wins over anything
    else for that block), and optional brand-new blocks to splice in.

    `insertions` maps a block id (or the sentinel `"__start__"`) to a list of
    raw text blocks to insert immediately after it (or, for `"__start__"`,
    before the first block) — this is how the UI adds new images/tables/text
    that weren't part of either input file's diff.

    `default_action` controls what an item with no explicit decision resolves
    to: "original" (default) keeps the original/base-file text — the safe
    choice when "suggested" is a genuinely pending proposal that shouldn't be
    silently applied. "suggested" instead keeps the suggested/proposed-file
    text by default — for review sessions where "proposed" is already the
    live, correct document and "original" is only there for side-by-side
    comparison (e.g. reviewing already-applied edits against a pre-edit
    snapshot) — in that setup, defaulting to "original" would silently
    *revert* real, already-accepted work for every block nobody got around
    to clicking through, which is the wrong direction to fail in.
    """
    block_overrides = block_overrides or {}
    insertions = insertions or {}
    out_blocks = []

    def emit_insertions(key: str) -> None:
        for text in insertions.get(key, []):
            if text:
                out_blocks.append(text)

    emit_insertions("__start__")
    for block in blocks:
        override = block_overrides.get(block.get("id"))
        if override is not None:
            if override:
                out_blocks.append(override)
        elif block.get("fixed"):
            out_blocks.append(block["text"])
        elif block.get("type") == "sentence_group":
            parts = []
            for sub in block["subsegments"]:
                text = sub["text"] if sub.get("fixed") else _resolve(sub, decisions, default_action)
                if text:
                    parts.append(text)
            joined = " ".join(parts)
            if joined:
                out_blocks.append(joined)
        else:
            text = _resolve(block, decisions, default_action)
            if text:
                out_blocks.append(text)
        emit_insertions(block.get("id"))
    return "\n\n".join(out_blocks) + "\n"


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, addr, handler_cls, *,
        blocks: list[dict], output_path: Path, asset_root: Path, media_dir: Path,
        citations: dict[str, dict[str, str]] | None = None,
        followup_report_path: Path | None = None,
        default_action: str = "original",
    ):
        super().__init__(addr, handler_cls)
        self.blocks = blocks
        self.output_path = output_path
        self.asset_root = asset_root.resolve()
        self.media_dir = media_dir
        # CE-#### -> parsed citation_evidence_register.md fields, for the
        # review UI's marker tooltips ([CE-0001], [CLAIM_NEEDS_REVISION], ...).
        # Empty dict (not None) when no register was found/passed, so the
        # frontend can tell "no data available" from "still loading".
        self.citations = citations or {}
        # Where to (re)write the flagged-citations follow-up report on every
        # save. None when there's no citation register to derive it from.
        self.followup_report_path = followup_report_path
        # See reconstruct()'s docstring — "original" (default) or "suggested".
        self.default_action = default_action
        # Populated by the first successful /api/save this server process
        # handles; returned from /api/diff so a page reload can restore the
        # same decisions instead of showing every change as pending again.
        # In-memory only (this process is long-lived across reloads, but a
        # server restart forgets it — the saved *file* is never at risk
        # either way, only this convenience state).
        self.last_save: dict | None = None


class ReviewHandler(BaseHTTPRequestHandler):
    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status: int = 200) -> None:
        self._send_bytes(json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8", status)

    def _send_download(self, body: bytes, content_type: str, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, filename: str, content_type: str) -> None:
        path = STATIC_DIR / filename
        if not path.is_file():
            self.send_error(404)
            return
        self._send_bytes(path.read_bytes(), f"{content_type}; charset=utf-8")

    def _serve_asset(self, url_path: str) -> None:
        """Serve a file referenced by the document (e.g. an <img src=...>)
        relative to --asset-root, so images embedded via a path like
        "thesis/working/media/foo.png" actually resolve instead of 404ing."""
        rel = unquote(url_path).lstrip("/")
        candidate = (self.server.asset_root / rel).resolve()
        if self.server.asset_root not in candidate.parents and candidate != self.server.asset_root:
            self.send_error(403)
            return
        if not candidate.is_file():
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self._send_bytes(candidate.read_bytes(), content_type)

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_static("index.html", "text/html")
        elif path == "/app.js":
            self._serve_static("app.js", "application/javascript")
        elif path == "/style.css":
            self._serve_static("style.css", "text/css")
        elif path == "/api/diff":
            self._send_json({
                "blocks": self.server.blocks,
                "total_changes": count_changes(self.server.blocks),
                "output_path": str(self.server.output_path),
                "last_save": self.server.last_save,
                "default_action": self.server.default_action,
            })
        elif path == "/api/citations":
            self._send_json({"citations": self.server.citations})
        else:
            self._serve_asset(path)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json_body()
        except (ValueError, UnicodeDecodeError):
            self._send_json({"error": "invalid JSON body"}, 400)
            return

        if path == "/api/save":
            self._handle_save(payload)
        elif path == "/api/upload-asset":
            self._handle_upload(payload)
        elif path == "/api/export":
            self._handle_export(payload)
        else:
            self.send_error(404)

    def _handle_save(self, payload: dict) -> None:
        decisions = payload.get("decisions", {})
        block_overrides = payload.get("block_overrides", {})
        insertions = payload.get("insertions", {})
        citation_decisions = payload.get("citation_decisions", {})
        merged = reconstruct(
            self.server.blocks, decisions, block_overrides, insertions,
            default_action=self.server.default_action,
        )

        output_path = self.server.output_path
        backup_path = None
        if output_path.exists():
            ts = time.strftime("%Y-%m-%d_%H%M%S")
            backup_path = output_path.with_name(f"{output_path.stem}.review-backup-{ts}{output_path.suffix}")
            backup_path.write_text(output_path.read_text(encoding="utf-8"), encoding="utf-8")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(merged, encoding="utf-8")

        followup_report_path = None
        if self.server.followup_report_path is not None:
            write_followup_report(
                self.server.followup_report_path, citation_decisions, self.server.citations,
            )
            followup_report_path = str(self.server.followup_report_path)

        self.server.last_save = {
            "decisions": decisions,
            "block_overrides": block_overrides,
            "insertions": insertions,
            "citation_decisions": citation_decisions,
            "written_path": str(output_path),
            "backup_path": str(backup_path) if backup_path else None,
            "followup_report_path": followup_report_path,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._send_json({
            "status": "ok",
            "written_path": str(output_path),
            "backup_path": str(backup_path) if backup_path else None,
            "followup_report_path": followup_report_path,
        })

    def _handle_export(self, payload: dict) -> None:
        """Render the current review state (whatever's currently decided —
        same reconstruct() as /api/save, just never written to disk here) as
        a downloadable file. `clean=True` strips this project's inline
        catalogue/citation-tracking markers first (see
        strip_catalogue_markers()) for a submission-ready read; `clean=False`
        keeps them, e.g. for an internal reviewer who wants to see everything.
        `format` is one of "md" (no external tool needed), "docx", or "pdf"
        (both via pandoc — see find_pdf_engine() for the PDF backend)."""
        decisions = payload.get("decisions", {})
        block_overrides = payload.get("block_overrides", {})
        insertions = payload.get("insertions", {})
        fmt = payload.get("format", "md")
        clean = bool(payload.get("clean"))

        merged = reconstruct(
            self.server.blocks, decisions, block_overrides, insertions,
            default_action=self.server.default_action,
        )
        if clean:
            merged = strip_catalogue_markers(merged)

        stem = self.server.output_path.stem or "document"
        suffix = "clean" if clean else "as-shown"
        base_filename = f"{stem}_{suffix}"

        if fmt == "md":
            self._send_download(
                merged.encode("utf-8"), "text/markdown; charset=utf-8", f"{base_filename}.md",
            )
            return

        if shutil.which("pandoc") is None:
            self._send_json(
                {"error": "pandoc is not installed on this machine — install it "
                          "(e.g. `brew install pandoc`) to export PDF/Word, or "
                          "download Markdown instead."},
                501,
            )
            return

        pandoc_cmd = ["pandoc", "--resource-path", str(self.server.asset_root)]
        if fmt == "pdf":
            engine = find_pdf_engine()
            if engine is None:
                self._send_json(
                    {"error": "No PDF engine found on this machine (looked for: "
                              + ", ".join(PDF_ENGINE_CANDIDATES) + "). Install one "
                              "(e.g. `brew install wkhtmltopdf`), or download "
                              "Word/Markdown instead."},
                    501,
                )
                return
            pandoc_cmd += ["--pdf-engine", engine]
            out_suffix, content_type = ".pdf", "application/pdf"
        elif fmt == "docx":
            out_suffix = ".docx"
            content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        else:
            self._send_json({"error": f"unknown format: {fmt!r}"}, 400)
            return

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            in_path = tmp_dir / "input.md"
            out_path = tmp_dir / f"output{out_suffix}"
            in_path.write_text(convert_html_figures_for_pandoc(merged), encoding="utf-8")
            result = subprocess.run(
                pandoc_cmd + [str(in_path), "-o", str(out_path)],
                capture_output=True, text=True,
            )
            if result.returncode != 0 or not out_path.is_file():
                self._send_json(
                    {"error": f"pandoc failed: {result.stderr.strip() or 'unknown error'}"}, 500,
                )
                return
            self._send_download(out_path.read_bytes(), content_type, f"{base_filename}{out_suffix}")

    def _handle_upload(self, payload: dict) -> None:
        filename = payload.get("filename") or "upload"
        data_b64 = payload.get("data_base64", "")
        try:
            data = base64.b64decode(data_b64)
        except (ValueError, TypeError):
            self._send_json({"error": "invalid base64 payload"}, 400)
            return

        stem = Path(SAFE_FILENAME_RE.sub("_", filename)).stem or "image"
        suffix = Path(filename).suffix or ".png"
        media_dir = self.server.media_dir
        media_dir.mkdir(parents=True, exist_ok=True)

        candidate = media_dir / f"{stem}{suffix}"
        n = 1
        while candidate.exists():
            candidate = media_dir / f"{stem}_{n}{suffix}"
            n += 1
        candidate.write_bytes(data)

        try:
            rel_path = candidate.resolve().relative_to(self.server.asset_root)
        except ValueError:
            rel_path = candidate
        self._send_json({"path": rel_path.as_posix()})

    def log_message(self, fmt: str, *args) -> None:  # keep console quiet
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("original", help="Path to the original/base file")
    ap.add_argument("proposed", help="Path to the proposed/edited file")
    ap.add_argument("--output", help="Where to write the reviewed result (default: overwrite <proposed>)")
    ap.add_argument(
        "--asset-root",
        help="Base directory that relative asset paths (e.g. <img src=\"a/b.png\">) "
        "are resolved against for serving (default: current working directory)",
    )
    ap.add_argument(
        "--media-dir",
        help="Where images inserted via the UI are saved (default: a 'media' "
        "folder next to <proposed.md>)",
    )
    ap.add_argument(
        "--citation-register",
        help="Path to a citation_evidence_register.md-style file. Its CE-#### "
        "entries power hover/click tooltips in the review UI for [CE-####], "
        "[CLAIM_NEEDS_REVISION], and similar inline markers, showing the "
        "verbatim quote and why a source does/doesn't support a claim. "
        "Default: thesis/evidence_maps/citation_evidence_register.md next to "
        "the current working directory, if it exists; omitted (tooltips just "
        "show the marker text) if not found and not explicitly passed.",
    )
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="Don't auto-open a browser tab")
    ap.add_argument(
        "--default-keep",
        choices=["original", "suggested"],
        default="original",
        help="What an undecided change resolves to on save. 'original' (default) is "
        "safe when <proposed> is a genuinely pending edit that shouldn't be applied "
        "without review. Use 'suggested' when <proposed> is already the live, correct "
        "document and <original> is only there for side-by-side comparison (e.g. "
        "reviewing already-applied edits against a pre-edit snapshot) — otherwise "
        "saving before reviewing every change would silently revert real work.",
    )
    args = ap.parse_args(argv)

    original_path = Path(args.original)
    proposed_path = Path(args.proposed)
    old_text = original_path.read_text(encoding="utf-8")
    new_text = proposed_path.read_text(encoding="utf-8")

    blocks = build_diff_model(old_text, new_text)
    output_path = Path(args.output) if args.output else proposed_path
    asset_root = Path(args.asset_root) if args.asset_root else Path.cwd()
    media_dir = Path(args.media_dir) if args.media_dir else proposed_path.resolve().parent / "media"

    if args.citation_register:
        citation_register_path = Path(args.citation_register)
    else:
        default_register = Path.cwd() / "thesis/evidence_maps/citation_evidence_register.md"
        citation_register_path = default_register if default_register.is_file() else None

    citations: dict[str, dict[str, str]] = {}
    followup_report_path: Path | None = None
    if citation_register_path:
        if citation_register_path.is_file():
            citations = parse_citation_register(citation_register_path)
            enrich_citations_with_recommendations(citations)
            followup_report_path = citation_register_path.with_name("citation_review_followups.md")
        else:
            print(f"Warning: --citation-register path not found, tooltips will be text-only: {citation_register_path}")

    server = ReviewServer(
        ("127.0.0.1", args.port), ReviewHandler,
        blocks=blocks, output_path=output_path, asset_root=asset_root, media_dir=media_dir,
        citations=citations, followup_report_path=followup_report_path,
        default_action=args.default_keep,
    )
    url = f"http://127.0.0.1:{args.port}/"
    total = count_changes(blocks)
    print(f"{total} change(s) to review.")
    print(f"Serving review UI at {url}")
    print(f"Assets resolved against: {asset_root}")
    print(f"Uploaded images saved to: {media_dir}")
    if citation_register_path:
        print(f"Citation tooltips loaded from: {citation_register_path} ({len(citations)} entries)")
        print(f"Follow-up report will be (re)written to: {followup_report_path} on every save")
    else:
        print("Citation tooltips: none found (pass --citation-register to enable)")
    print(f"Save will write to: {output_path} (a timestamped backup is made first)")
    print(
        f"Undecided changes default to keeping: {args.default_keep}"
        + (" (safe: matches <proposed>, nothing reverts)" if args.default_keep == "suggested" else "")
    )
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
