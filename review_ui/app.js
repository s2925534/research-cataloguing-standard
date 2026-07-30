(function () {
  "use strict";

  // --- Theme toggle: cycling system -> light -> dark -> system --------
  // Same mechanism as the zqx project's theme-toggle.tsx: localStorage-
  // backed preference, a synchronous init script in index.html applies it
  // before first paint, and CSS custom properties under :root[data-theme]
  // override the prefers-color-scheme media query in both directions.

  const THEME_STORAGE_KEY = "review-theme";
  const THEME_ORDER = ["system", "light", "dark"];
  const THEME_LABEL = { system: "System", light: "Light", dark: "Dark" };
  const THEME_ICON = {
    system:
      '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
      '<rect x="3" y="4" width="18" height="13" rx="1.5" stroke="currentColor" stroke-width="1.75"/>' +
      '<path d="M8 20h8M12 17v3" stroke="currentColor" stroke-width="1.75" stroke-linecap="round"/></svg>',
    light:
      '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
      '<circle cx="12" cy="12" r="4.5" stroke="currentColor" stroke-width="1.75"/>' +
      '<path d="M12 2v2.5M12 19.5V22M22 12h-2.5M4.5 12H2M19.07 4.93l-1.77 1.77M6.7 17.3l-1.77 1.77M19.07 19.07l-1.77-1.77M6.7 6.7 4.93 4.93" ' +
      'stroke="currentColor" stroke-width="1.75" stroke-linecap="round"/></svg>',
    dark:
      '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
      '<path d="M20 13.5A8.5 8.5 0 1 1 10.5 4a6.5 6.5 0 0 0 9.5 9.5Z" stroke="currentColor" stroke-width="1.75" stroke-linejoin="round"/></svg>',
  };

  function readStoredThemePreference() {
    try {
      const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
      return stored === "light" || stored === "dark" ? stored : "system";
    } catch (e) {
      return "system";
    }
  }

  function applyThemePreference(preference) {
    if (preference === "system") {
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.setAttribute("data-theme", preference);
    }
  }

  function initThemeToggle() {
    const btn = document.getElementById("theme-toggle");
    if (!btn) return;
    let preference = readStoredThemePreference();

    function render() {
      const next = THEME_ORDER[(THEME_ORDER.indexOf(preference) + 1) % THEME_ORDER.length];
      btn.innerHTML = THEME_ICON[preference];
      btn.title = `Theme: ${THEME_LABEL[preference]} (click for ${THEME_LABEL[next]})`;
      btn.setAttribute("aria-label", btn.title);
    }
    render();

    btn.addEventListener("click", () => {
      preference = THEME_ORDER[(THEME_ORDER.indexOf(preference) + 1) % THEME_ORDER.length];
      applyThemePreference(preference);
      try {
        if (preference === "system") {
          window.localStorage.removeItem(THEME_STORAGE_KEY);
        } else {
          window.localStorage.setItem(THEME_STORAGE_KEY, preference);
        }
      } catch (e) {}
      render();
    });
  }

  initThemeToggle();

  const KIND_LABELS = {
    block_replace: "Changed",
    block_insert: "Added",
    block_delete: "Removed",
    sentence_replace: "Changed",
    sentence_insert: "Added",
    sentence_delete: "Removed",
  };

  const KIND_CSS_CLASS = {
    block_replace: "kind-changed",
    block_insert: "kind-added",
    block_delete: "kind-removed",
    sentence_replace: "kind-changed",
    sentence_insert: "kind-added",
    sentence_delete: "kind-removed",
  };

  // --- Citation marker tooltips -------------------------------------------
  // Thesis-specific but harmless for any other document: inline markers like
  // [CE-0100], [CLAIM_NEEDS_REVISION — ...], [UNVERIFIED_SOURCE] are wrapped
  // in a clickable span. Clicking shows a popover with the full explanation
  // and, when a CE-#### id is present and /api/citations has data for it,
  // the verbatim word-for-word quotation from the source and why it does or
  // doesn't support the claim. The raw document text is never changed by
  // this — it's a display-only decoration, same principle as the rest of
  // this file's rendering (see the note above inlineMarkdown).

  let citationRegister = {}; // CE-#### -> {field label: value}, from /api/citations
  const tooltipContents = []; // index -> pre-built tooltip inner HTML (info only, no decision controls)
  const tooltipMarkerKeys = []; // index -> stable key for that marker instance (see wrapCitationMarkers)

  // Per-marker accept/flag decisions, independent of the block-level
  // keep/accept/edit decisions above. A single sentence can carry several
  // citations where only one needs a second look — this lets that one be
  // flagged without having to accept or reject the whole sentence/paragraph
  // as a text change. Keyed by markerKey (see wrapCitationMarkers), value is
  // {decision: "accept"|"flag", note?: string, decided_at: ISO string}.
  const citationDecisions = {};

  const MARKER_KEYWORDS = [
    "CE-\\d{4}", "CLAIM_NEEDS_REVISION", "UNVERIFIED_SOURCE", "NEEDS_SOURCE",
    "PAGE_NEEDED", "QUOTE_NEEDED", "INTERNAL_EVIDENCE_NEEDED", "NEW",
    "pending physical library scan",
  ];
  const MARKER_RE = new RegExp("\\[((?:" + MARKER_KEYWORDS.join("|") + ")[^\\]]*)\\]", "g");

  const GENERIC_MARKER_INFO = {
    unverified: "Citation exists in the working document but hasn't been checked against its source yet.",
    needs_source: "This claim needs a source; none is currently attached.",
    page_needed: "A source was found and verified, but the page/location hasn't been recorded yet.",
    quote_needed: "A source was found, but a supporting quotation hasn't been captured yet.",
    internal_evidence: "This claim needs project artefacts/datasets/validation records rather than academic literature.",
    pending_scan: "Source exists but needs a physical library scan (not yet digitised/accessible) before it can be verified.",
    new: "A newly proposed addition — not yet accepted. Review before treating as final.",
    revision: "The cited source doesn't support this claim as worded (or actively cuts against it) — flagged for review.",
    other: "",
  };

  const MARKER_KIND_LABEL = {
    ce: "Verified citation",
    revision: "Needs revision",
    unverified: "Not yet checked",
    needs_source: "Needs a source",
    page_needed: "Page needed",
    quote_needed: "Quote needed",
    internal_evidence: "Needs internal evidence",
    pending_scan: "Pending library scan",
    new: "New — pending review",
    other: "Marker",
  };

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function splitMarker(full) {
    const idx = full.search(/[—–]/);
    if (idx === -1) return { head: full.trim(), explanation: "" };
    return { head: full.slice(0, idx).trim(), explanation: full.slice(idx + 1).trim() };
  }

  function markerKind(head) {
    if (/^CE-\d{4}$/.test(head)) return "ce";
    if (head.startsWith("CLAIM_NEEDS_REVISION")) return "revision";
    if (head.startsWith("UNVERIFIED_SOURCE")) return "unverified";
    if (head.startsWith("NEEDS_SOURCE")) return "needs_source";
    if (head.startsWith("PAGE_NEEDED")) return "page_needed";
    if (head.startsWith("QUOTE_NEEDED")) return "quote_needed";
    if (head.startsWith("INTERNAL_EVIDENCE_NEEDED")) return "internal_evidence";
    if (head.startsWith("pending physical library scan")) return "pending_scan";
    if (head.startsWith("NEW")) return "new";
    return "other";
  }

  function tipRow(label, value, cls) {
    if (!value) return "";
    return (
      `<div class="cite-tip-label">${escapeHtml(label)}</div>` +
      `<div class="cite-tip-body${cls ? " " + cls : ""}">${escapeHtml(value)}</div>`
    );
  }

  const RECOMMENDATION_LABEL = {
    accept: "Recommendation: accept",
    flag: "Recommendation: flag for follow-up",
    redundant: "Recommendation: worth a look (possibly redundant)",
    remove: "Recommendation: remove — cuts against the claim",
    info: "Informational — no action needed",
  };

  // Which of the three action buttons a given recommendation verdict points
  // at. "redundant" is an advisory signal (this claim already has other
  // strong support), not proof the source is wrong, so it steers toward
  // "Flag for follow-up" — a human judgement call — rather than the stronger
  // "Recommend removal", which is reserved for genuine contradictions.
  const VERDICT_TO_BUTTON = { accept: "accept", flag: "flag", redundant: "flag", remove: "remove" };

  function buildRecommendationHtml(entry) {
    const rec = entry && entry._recommendation;
    if (!rec || !rec.verdict) return "";
    const label = RECOMMENDATION_LABEL[rec.verdict] || "";
    return (
      '<div class="cite-tip-label">Tool recommendation</div>' +
      `<div class="cite-tip-rec cite-tip-rec--${escapeHtml(rec.verdict)}">${escapeHtml(label)}</div>` +
      (rec.reason ? `<div class="cite-tip-body">${escapeHtml(rec.reason)}</div>` : "")
    );
  }

  function buildTooltipHtml(kind, head, explanation, ceId) {
    const entry = ceId ? citationRegister[ceId] : null;
    const parts = [];
    const kindLabel = MARKER_KIND_LABEL[kind] || MARKER_KIND_LABEL.other;

    parts.push('<div class="cite-tip-title">' +
      escapeHtml(ceId || head) +
      ` <span class="cite-tip-kind cite-tip-kind--${kind}">${escapeHtml(kindLabel)}</span>` +
      (entry && entry["Confidence"]
        ? ` <span class="cite-tip-conf cite-tip-conf--${entry["Confidence"].toLowerCase()}">${escapeHtml(entry["Confidence"])} confidence</span>`
        : "") +
      "</div>");

    if (entry) {
      parts.push(buildRecommendationHtml(entry));
      parts.push(tipRow("Full APA 7 reference", entry["Full APA 7 reference"]));
      parts.push(tipRow("Claim supported", entry["Claim supported"]));
      if (kind === "revision" || explanation) {
        parts.push(tipRow("Why flagged for review", explanation || GENERIC_MARKER_INFO.revision, "cite-tip-warn"));
      }
      if (entry["Verbatim quotation from source"]) {
        parts.push(
          '<div class="cite-tip-label">Verbatim quotation from source</div>' +
          '<blockquote class="cite-tip-quote">' + escapeHtml(entry["Verbatim quotation from source"]) + "</blockquote>"
        );
      }
      parts.push(tipRow("Page / location", entry["Page number or location"]));
      parts.push(tipRow("How it supports the claim", entry["How the quote supports the claim"]));
      parts.push(tipRow("Notes", entry["Notes"]));
      // Every entry's raw "Review status" field literally says "Needs
      // review" regardless of how solid the citation actually is — that
      // field just tracks "has someone looked at this in the browser yet,"
      // not "is this any good." Show the synthesised status instead: a
      // clean high/medium-confidence fit reads as settled, only genuinely
      // flagged entries keep "Needs review" language (with the reason why).
      const rec = entry._recommendation;
      if (rec && rec.status_label) {
        parts.push(
          '<div class="cite-tip-label">Review status</div>' +
          `<div class="cite-tip-body cite-tip-status cite-tip-status--${escapeHtml(rec.verdict)}">${escapeHtml(rec.status_label)}</div>`
        );
      } else {
        parts.push(tipRow("Review status", entry["Review status"]));
      }
    } else {
      const generic = GENERIC_MARKER_INFO[kind] || "";
      if (generic) parts.push('<div class="cite-tip-body">' + escapeHtml(generic) + "</div>");
      if (explanation) parts.push(tipRow("Details", explanation, kind === "revision" ? "cite-tip-warn" : ""));
      if (!entry && ceId) {
        parts.push(
          '<div class="cite-tip-body cite-tip-muted">No entry for ' + escapeHtml(ceId) +
          " found in the citation register (either it hasn't been added yet, or the review tool wasn't started with --citation-register).</div>"
        );
      }
    }
    return parts.join("");
  }

  function wrapCitationMarkers(text) {
    return text.replace(MARKER_RE, (_match, full) => {
      const { head, explanation } = splitMarker(full);
      const kind = markerKind(head);
      const ceMatch = head.match(/CE-\d{4}/);
      const ceId = ceMatch ? ceMatch[0] : null;
      // CE-#### ids are stable and unique per source, so use that directly;
      // markers without one (bare NEEDS_SOURCE, UNVERIFIED_SOURCE, ...) fall
      // back to their full bracket text, which is unique enough in practice
      // since each was hand-written for one specific spot in the document.
      const markerKey = ceId || (head + "::" + explanation);
      const html = buildTooltipHtml(kind, head, explanation, ceId);
      const idx = tooltipContents.length;
      tooltipContents.push(html);
      tooltipMarkerKeys.push(markerKey);
      const label = ceId || head;
      const decision = citationDecisions[markerKey] ? citationDecisions[markerKey].decision : null;
      const decidedCls = decision ? ` cite-marker--decided-${decision}` : "";
      return (
        `<span class="cite-marker cite-marker--${kind}${decidedCls}" data-tip-idx="${idx}" ` +
        `data-marker-key="${escapeHtml(markerKey)}" ` +
        `tabindex="0" role="button" aria-haspopup="true">[${escapeHtml(label)}]</span>`
      );
    });
  }

  // --- Per-marker accept/flag decisions ------------------------------------
  // Separate from the block-level keep/accept/edit decisions: a single
  // sentence can carry several citations where only one needs a second
  // look, so this tracks each marker's own accept/flag state independently
  // of whatever happens to the surrounding text.

  const DECISION_LABEL = {
    accept: "You marked this: looks good",
    flag: "You marked this: flagged for follow-up",
    remove: "You marked this: recommended for removal",
  };

  function buildDecisionControlsHtml(markerKey) {
    if (!markerKey) return "";
    const d = citationDecisions[markerKey];
    const decision = d ? d.decision : null;
    const note = d && d.note ? d.note : "";
    const keyAttr = escapeHtml(markerKey);
    const entry = citationRegister[markerKey];
    const recVerdict = entry && entry._recommendation ? entry._recommendation.verdict : null;
    const recButton = recVerdict ? VERDICT_TO_BUTTON[recVerdict] : null;
    const acceptLabel = "&#10003; Looks good" + (recButton === "accept" ? " (Recommended)" : "");
    const flagLabel = "&#9873; Flag for follow-up" + (recButton === "flag" ? " (Recommended)" : "");
    const removeLabel = "&#128465; Recommend removal" + (recButton === "remove" ? " (Recommended)" : "");
    const showNote = decision === "flag" || decision === "remove";
    return (
      '<div class="cite-tip-decision" data-marker-key="' + keyAttr + '">' +
        '<div class="cite-tip-label">Your call</div>' +
        '<div class="cite-decision-row">' +
          '<button type="button" class="cite-action-btn cite-action-btn--accept' + (decision === "accept" ? " active" : "") + (recButton === "accept" ? " cite-action-btn--recommended" : "") + '" ' +
            'data-action="accept" data-marker-key="' + keyAttr + '">' + acceptLabel + '</button>' +
          '<button type="button" class="cite-action-btn cite-action-btn--flag' + (decision === "flag" ? " active" : "") + (recButton === "flag" ? " cite-action-btn--recommended" : "") + '" ' +
            'data-action="flag" data-marker-key="' + keyAttr + '">' + flagLabel + '</button>' +
          '<button type="button" class="cite-action-btn cite-action-btn--remove' + (decision === "remove" ? " active" : "") + (recButton === "remove" ? " cite-action-btn--recommended" : "") + '" ' +
            'data-action="remove" data-marker-key="' + keyAttr + '">' + removeLabel + '</button>' +
        '</div>' +
        (decision ? '<div class="cite-tip-body cite-tip-decision-status cite-tip-decision-status--' + decision + '">' + escapeHtml(DECISION_LABEL[decision] || "") + '</div>' : '') +
        '<textarea class="cite-note-input" data-marker-key="' + keyAttr + '" placeholder="Optional note — e.g. ‘need a newer source’, ‘ask supervisor’" ' +
          'style="' + (showNote ? "" : "display:none;") + '">' + escapeHtml(note) + '</textarea>' +
      '</div>'
    );
  }

  function updateMarkerDecisionIndicators(markerKey) {
    const decision = citationDecisions[markerKey] ? citationDecisions[markerKey].decision : null;
    document.querySelectorAll('.cite-marker[data-marker-key="' + CSS.escape(markerKey) + '"]').forEach((el) => {
      el.classList.remove("cite-marker--decided-accept", "cite-marker--decided-flag", "cite-marker--decided-remove");
      if (decision) el.classList.add("cite-marker--decided-" + decision);
    });
  }

  function citationDecisionCounts() {
    let accept = 0, flag = 0, remove = 0;
    Object.values(citationDecisions).forEach((d) => {
      if (d.decision === "accept") accept++;
      else if (d.decision === "flag") flag++;
      else if (d.decision === "remove") remove++;
    });
    return { accept, flag, remove, total: tooltipMarkerKeys.length ? new Set(tooltipMarkerKeys).size : 0 };
  }

  function updateCitationCounts() {
    const el = document.getElementById("citation-counts");
    if (!el) return;
    const { accept, flag, remove, total } = citationDecisionCounts();
    if (!total) { el.textContent = ""; return; }
    const decided = accept + flag + remove;
    let text = `${decided} / ${total} citation(s) reviewed`;
    const parts = [];
    if (accept) parts.push(`${accept} ok`);
    if (flag) parts.push(`${flag} flagged for follow-up`);
    if (remove) parts.push(`${remove} recommended for removal`);
    if (parts.length) text += ` — ${parts.join(", ")}`;
    el.textContent = text;
    el.className = (flag || remove) ? "has-flags" : "";
  }

  function setCitationDecision(markerKey, action) {
    if (!markerKey) return;
    const existing = citationDecisions[markerKey] || {};
    if (existing.decision === action) {
      // clicking the same decision again clears it back to undecided
      delete citationDecisions[markerKey];
    } else {
      citationDecisions[markerKey] = {
        decision: action,
        note: existing.note || "",
        decided_at: new Date().toISOString(),
      };
    }
    updateMarkerDecisionIndicators(markerKey);
    updateCitationCounts();
  }

  // Single shared popover, positioned next to whichever marker was clicked.
  let activeTipTrigger = null;
  const citeTip = document.createElement("div");
  citeTip.className = "cite-tip";
  citeTip.style.display = "none";
  citeTip.setAttribute("role", "tooltip");
  document.body.appendChild(citeTip);

  function hideCiteTip() {
    citeTip.style.display = "none";
    if (activeTipTrigger) activeTipTrigger.classList.remove("cite-marker--open");
    activeTipTrigger = null;
  }

  function positionCiteTip(trigger) {
    const rect = trigger.getBoundingClientRect();
    const tipRect = citeTip.getBoundingClientRect();
    let left = rect.left + window.scrollX;
    let top = rect.bottom + window.scrollY + 6;
    const maxLeft = window.scrollX + document.documentElement.clientWidth - tipRect.width - 12;
    if (left > maxLeft) left = Math.max(window.scrollX + 12, maxLeft);
    if (top + tipRect.height > window.scrollY + window.innerHeight) {
      top = rect.top + window.scrollY - tipRect.height - 6;
    }
    citeTip.style.left = left + "px";
    citeTip.style.top = top + "px";
  }

  function showCiteTip(trigger) {
    const idx = Number(trigger.getAttribute("data-tip-idx"));
    const html = tooltipContents[idx];
    if (html === undefined) return;
    if (activeTipTrigger === trigger) {
      hideCiteTip();
      return;
    }
    if (activeTipTrigger) activeTipTrigger.classList.remove("cite-marker--open");
    const markerKey = tooltipMarkerKeys[idx] || trigger.getAttribute("data-marker-key");
    citeTip.innerHTML = html + buildDecisionControlsHtml(markerKey);
    citeTip.style.display = "block";
    trigger.classList.add("cite-marker--open");
    activeTipTrigger = trigger;
    positionCiteTip(trigger);
  }

  citeTip.addEventListener("click", (e) => {
    const btn = e.target.closest(".cite-action-btn");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const markerKey = btn.getAttribute("data-marker-key");
    setCitationDecision(markerKey, btn.getAttribute("data-action"));
    // Re-render just the decision-controls portion so the note field's
    // visibility and the active-button state reflect the new decision,
    // without closing the tooltip or losing the info above it.
    const decisionWrap = citeTip.querySelector(".cite-tip-decision");
    if (decisionWrap) decisionWrap.outerHTML = buildDecisionControlsHtml(markerKey);
    if (activeTipTrigger) positionCiteTip(activeTipTrigger);
  });
  citeTip.addEventListener("input", (e) => {
    const ta = e.target.closest(".cite-note-input");
    if (!ta) return;
    const markerKey = ta.getAttribute("data-marker-key");
    const existing = citationDecisions[markerKey] || { decision: "flag" };
    existing.note = ta.value;
    existing.decided_at = existing.decided_at || new Date().toISOString();
    citationDecisions[markerKey] = existing;
  });

  document.addEventListener("click", (e) => {
    const trigger = e.target.closest(".cite-marker");
    if (trigger) {
      e.preventDefault();
      showCiteTip(trigger);
      return;
    }
    if (!e.target.closest(".cite-tip")) hideCiteTip();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideCiteTip();
    if ((e.key === "Enter" || e.key === " ") && document.activeElement && document.activeElement.classList.contains("cite-marker")) {
      e.preventDefault();
      showCiteTip(document.activeElement);
    }
  });
  window.addEventListener("scroll", () => {
    if (activeTipTrigger) positionCiteTip(activeTipTrigger);
  }, { passive: true });

  // --- Lightweight read-only Markdown/HTML rendering for context blocks ---
  // The source document already mixes Markdown with raw HTML (pandoc-style
  // <figure>/<figcaption>, <span class="mark">). This renderer trusts that
  // and only additionally interprets Markdown-specific syntax (headings,
  // tables, lists, code fences, blockquotes, **bold**/*italic*/`code`), so
  // plain text output no longer looks like a text dump of the source file.
  // Editing always operates on the raw text (see currentBlockText/textarea
  // prefill below), never on this rendered HTML — rendering is display-only.

  function inlineMarkdown(text) {
    return wrapCitationMarkers(text)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_]+)__/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
  }

  function renderHeading(text) {
    const line = text.trim().split("\n")[0];
    const match = line.match(/^(#{1,6})\s*(.*)$/);
    const level = match ? Math.min(match[1].length, 6) : 3;
    const el = document.createElement("h" + level);
    el.innerHTML = inlineMarkdown(match ? match[2] : line);
    return el;
  }

  function parseTableRow(line) {
    let l = line.trim();
    if (l.startsWith("|")) l = l.slice(1);
    if (l.endsWith("|")) l = l.slice(0, -1);
    return l.split("|").map((c) => c.trim());
  }

  function renderTable(text) {
    const lines = text.trim().split("\n").filter((l) => l.trim() !== "");
    const scroller = document.createElement("div");
    scroller.className = "table-scroll";
    const table = document.createElement("table");
    if (!lines.length) return scroller;

    const header = parseTableRow(lines[0]);
    const thead = document.createElement("thead");
    const headTr = document.createElement("tr");
    header.forEach((cell) => {
      const th = document.createElement("th");
      th.innerHTML = inlineMarkdown(cell);
      headTr.appendChild(th);
    });
    thead.appendChild(headTr);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    lines.slice(2).forEach((line) => {
      const tr = document.createElement("tr");
      parseTableRow(line).forEach((cell) => {
        const td = document.createElement("td");
        td.innerHTML = inlineMarkdown(cell);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    scroller.appendChild(table);
    return scroller;
  }

  function renderList(text) {
    const lines = text.trim().split("\n");
    const ordered = /^\s*\d+[.)]\s+/.test(lines[0]);
    const list = document.createElement(ordered ? "ol" : "ul");
    let currentLi = null;
    lines.forEach((line) => {
      const m = line.match(/^\s*(?:[-*+]|\d+[.)])\s+(.*)$/);
      if (m) {
        currentLi = document.createElement("li");
        currentLi.innerHTML = inlineMarkdown(m[1]);
        list.appendChild(currentLi);
      } else if (currentLi && line.trim()) {
        currentLi.innerHTML += " " + inlineMarkdown(line.trim());
      }
    });
    return list;
  }

  function renderCode(text) {
    const lines = text.split("\n");
    if (lines[0] && lines[0].trim().startsWith("```")) lines.shift();
    if (lines.length && lines[lines.length - 1].trim().startsWith("```")) lines.pop();
    const pre = document.createElement("pre");
    const code = document.createElement("code");
    code.textContent = lines.join("\n");
    pre.appendChild(code);
    return pre;
  }

  function renderBlockquote(text) {
    const bq = document.createElement("blockquote");
    const p = document.createElement("p");
    p.innerHTML = inlineMarkdown(
      text.split("\n").map((l) => l.replace(/^\s*>\s?/, "")).join(" ")
    );
    bq.appendChild(p);
    return bq;
  }

  function renderParagraph(text) {
    const div = document.createElement("div");
    div.className = "rendered-paragraph";
    div.innerHTML = inlineMarkdown(text);
    div.querySelectorAll("img").forEach((img, idx) => {
      img.dataset.imgIdx = String(idx);
      img.addEventListener("error", () => {
        const placeholder = document.createElement("div");
        placeholder.className = "image-missing";
        placeholder.textContent = "Image not found: " + img.getAttribute("src");
        img.replaceWith(placeholder);
      }, { once: true });
      attachImageResizeHandle(img);
    });
    return div;
  }

  // --- Image resize handle ----------------------------------------------
  // A single drag handle on the image's bottom edge. Dragging changes
  // height; width follows proportionally so the figure never distorts.
  // The live drag only touches on-screen px sizing — the actual source
  // text (and its inches-based style attribute, matching the rest of the
  // document's figures) is only rewritten once, on release, via the
  // "image-resized" event handled in wrapBlock().
  const CSS_PX_PER_IN = 96;

  function attachImageResizeHandle(img) {
    if (img.closest(".img-resize-wrap")) return; // already wrapped (re-render safety)
    const wrap = document.createElement("span");
    wrap.className = "img-resize-wrap";
    img.parentNode.insertBefore(wrap, img);
    wrap.appendChild(img);

    const handle = document.createElement("span");
    handle.className = "img-resize-handle";
    handle.title = "Drag to resize";
    handle.setAttribute("aria-hidden", "true");
    wrap.appendChild(handle);

    let startY = 0, startW = 0, startH = 0, ratio = 1;

    function onMove(e) {
      const clientY = e.touches ? e.touches[0].clientY : e.clientY;
      const newH = Math.max(30, startH + (clientY - startY));
      img.style.width = newH * ratio + "px";
      img.style.height = newH + "px";
    }

    function onUp() {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
      wrap.classList.remove("resizing");
      const rect = img.getBoundingClientRect();
      img.dispatchEvent(new CustomEvent("image-resized", {
        bubbles: true,
        detail: {
          idx: Number(img.dataset.imgIdx || 0),
          widthIn: rect.width / CSS_PX_PER_IN,
          heightIn: rect.height / CSS_PX_PER_IN,
        },
      }));
    }

    handle.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const rect = img.getBoundingClientRect();
      startY = e.clientY;
      startW = rect.width;
      startH = rect.height;
      ratio = startW / startH;
      wrap.classList.add("resizing");
      document.addEventListener("pointermove", onMove);
      document.addEventListener("pointerup", onUp);
    });
  }

  // Rewrites the width/height in the Nth <img> tag's style attribute
  // (0-indexed, matching data-imgIdx) within a raw HTML/Markdown block of
  // text. Preserves any other style properties and attributes untouched.
  function replaceImgSizeAtIndex(text, idx, widthIn, heightIn) {
    let counter = -1;
    return text.replace(/<img\b[^>]*>/gi, (tag) => {
      counter += 1;
      if (counter !== idx) return tag;
      const w = widthIn.toFixed(5) + "in";
      const h = heightIn.toFixed(5) + "in";
      if (/style\s*=\s*"[^"]*"/i.test(tag)) {
        return tag.replace(/style\s*=\s*"([^"]*)"/i, (_m, styleContent) => {
          const rest = styleContent
            .replace(/width\s*:\s*[^;]+;?/i, "")
            .replace(/height\s*:\s*[^;]+;?/i, "")
            .trim();
          return `style="width:${w};height:${h};${rest ? " " + rest : ""}"`;
        });
      }
      return tag.replace(/<img/i, `<img style="width:${w};height:${h};"`);
    });
  }

  // Mirrors review.py's split_blocks(): blank-line-separated chunks. Used
  // below to recover when a single diff unit actually bundles more than one
  // markdown element (e.g. a paragraph immediately followed by a table,
  // which happens whenever they change together in the same replace/insert
  // — the backend's diff grouping doesn't guarantee one block_type per
  // change, only per originally-split block).
  function splitIntoSubBlocks(text) {
    return text.replace(/\r\n/g, "\n").trim().split(/\n\s*\n/).filter((b) => b.trim() !== "");
  }

  function renderMarkdownish(text, blockType) {
    // classifyBlockJs (like its Python counterpart) only looks at the first
    // line to decide a block's type, which is right for a *single* markdown
    // element but wrong for a blob that bundles several — e.g. text starting
    // with a paragraph and ending with a table gets classified "paragraph"
    // for its entire length, and renderParagraph has no idea the tail is a
    // table: it just dumps the raw text (pipes, dashes and all) into HTML,
    // where consecutive newlines collapse to whitespace and the table reads
    // as one run-on line. Only reachable via blockType "paragraph" (the
    // default/fallback classification) — table/code/list/heading/blockquote
    // are trusted as single units so a code block's own blank lines are
    // never mistaken for a block boundary.
    if (blockType === "paragraph") {
      const subBlocks = splitIntoSubBlocks(text);
      if (subBlocks.length > 1) {
        const wrap = document.createElement("div");
        subBlocks.forEach((sub) => {
          wrap.appendChild(renderMarkdownish(sub, classifyBlockJs(sub)));
        });
        return wrap;
      }
    }
    switch (blockType) {
      case "heading": return renderHeading(text);
      case "table": return renderTable(text);
      case "list": return renderList(text);
      case "code": return renderCode(text);
      case "blockquote": return renderBlockquote(text);
      default: return renderParagraph(text);
    }
  }

  function classifyBlockJs(text) {
    const lines = text.trim().split("\n");
    const first = (lines[0] || "").trim();
    if (first.startsWith("#")) return "heading";
    if (first.startsWith("```")) return "code";
    if (first.startsWith(">")) return "blockquote";
    if (lines.length >= 2 && first.includes("|") && /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(lines[1])) {
      return "table";
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(first)) return "list";
    return "paragraph";
  }

  function buildFixedContent(text, blockType) {
    const holder = document.createElement("div");
    holder.className = "fixed-block " + (blockType || "");
    holder.appendChild(renderMarkdownish(text, blockType));
    return holder;
  }

  // --- Image upload plumbing -------------------------------------------

  function fileToBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    });
  }

  function uploadImageFile(file) {
    return fileToBase64(file)
      .then((data_base64) =>
        fetch("/api/upload-asset", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filename: file.name, data_base64 }),
        })
      )
      .then((r) => r.json())
      .then((data) => {
        if (data.error) throw new Error(data.error);
        return data.path;
      });
  }

  function pickImageFile() {
    return new Promise((resolve) => {
      const input = document.createElement("input");
      input.type = "file";
      input.accept = "image/*";
      input.style.display = "none";
      document.body.appendChild(input);
      input.addEventListener("change", () => {
        resolve(input.files[0] || null);
        input.remove();
      });
      input.click();
    });
  }

  // --- Review state -------------------------------------------------------

  const decisions = {}; // item_id -> {action: 'keep'|'accept'|'custom', text?}
  const blockOverrides = {}; // block_id -> free-text override, wins over everything else for that block
  const insertions = {}; // block_id (or "__start__") -> [raw text, ...] new blocks inserted after it
  const insertAnchors = {}; // block_id (or "__start__") -> DOM element the next insertion should follow
  let totalChanges = 0;
  let outputPath = "";
  // "original" (default) or "suggested" — see review.py's reconstruct()
  // docstring. Mirrors the server's own fallback so the live preview here
  // matches what a save would actually produce for an undecided change.
  let defaultAction = "original";
  let currentBlocksData = []; // last /api/diff response's `blocks`, kept around so bulk actions can re-render

  const docEl = document.getElementById("doc");
  const countsEl = document.getElementById("counts");
  const saveBtn = document.getElementById("save-btn");
  const printBtn = document.getElementById("print-btn");
  const saveStatusEl = document.getElementById("save-toast");
  const downloadMenu = document.getElementById("download-menu");

  // Floating, self-dismissing toast (replaces the old always-visible toolbar
  // status text). `autoHide` is false for the transient "Saving…" state
  // (superseded almost immediately by success/error) and true for anything
  // meant to be read once and then get out of the way.
  function showSaveToast(text, cls, autoHide) {
    clearTimeout(showSaveToast._hideTimer);
    saveStatusEl.textContent = text;
    saveStatusEl.className = "save-status " + cls + " visible";
    if (autoHide) {
      showSaveToast._hideTimer = setTimeout(() => {
        saveStatusEl.classList.remove("visible");
      }, 4000);
    }
  }

  function resolveText(item) {
    const d = decisions[item.id];
    const action = d ? d.action : (defaultAction === "suggested" ? "accept" : "keep");
    if (action === "accept") return item.suggested || "";
    if (action === "custom") return d.text || "";
    return item.original || "";
  }

  function currentBlockText(block) {
    if (block.id !== undefined && Object.prototype.hasOwnProperty.call(blockOverrides, block.id)) {
      return blockOverrides[block.id];
    }
    if (block.fixed) return block.text;
    if (block.type === "sentence_group") {
      return block.subsegments
        .map((sub) => (sub.fixed ? sub.text : resolveText(sub)))
        .filter(Boolean)
        .join(" ");
    }
    return resolveText(block);
  }

  function decidedCount() {
    return Object.keys(decisions).length;
  }

  function overriddenCount() {
    return Object.keys(blockOverrides).length;
  }

  function insertedCount() {
    return Object.values(insertions).reduce((n, arr) => n + arr.length, 0);
  }

  function decisionBreakdown() {
    let keep = 0, accept = 0, custom = 0;
    Object.values(decisions).forEach((d) => {
      if (d.action === "accept") accept++;
      else if (d.action === "custom") custom++;
      else if (d.action === "keep") keep++;
    });
    return { keep, accept, custom };
  }

  function updateCounts() {
    const { keep, accept, custom } = decisionBreakdown();
    const pending = totalChanges - decidedCount();
    let text = `${decidedCount()} / ${totalChanges} change(s) reviewed`;
    const parts = [];
    if (accept) parts.push(`${accept} accepted`);
    if (keep) parts.push(`${keep} kept original`);
    if (custom) parts.push(`${custom} custom`);
    if (pending) {
      const fallback = defaultAction === "suggested" ? "Keep Suggested" : "Keep Original";
      parts.push(`${pending} pending (defaults to ${fallback})`);
    }
    if (parts.length) text += ` — ${parts.join(", ")}`;
    if (overriddenCount()) text += ` · ${overriddenCount()} block(s) manually edited`;
    if (insertedCount()) text += ` · ${insertedCount()} new block(s) inserted`;
    countsEl.textContent = text;
  }

  function setDecision(id, action, text) {
    decisions[id] = text === undefined ? { action } : { action, text };
    updateCounts();
  }

  function buildPane(label, text, cls) {
    const pane = document.createElement("div");
    pane.className = "pane " + cls;
    if (text === null || text === undefined || text === "") {
      pane.classList.add("placeholder");
      pane.textContent = label === "original" ? "(none — this is new content)" : "(none — proposed removal)";
    } else {
      pane.appendChild(renderMarkdownish(text, classifyBlockJs(text)));
    }
    return pane;
  }

  function buildCard(item, onChangeCallback) {
    const card = document.createElement("div");
    card.className = "card";

    const sectionLabel = document.createElement("div");
    sectionLabel.className = "section-label";
    sectionLabel.textContent = item.section || "(before first heading)";
    card.appendChild(sectionLabel);

    const badge = document.createElement("span");
    badge.className = "kind-badge " + (KIND_CSS_CLASS[item.kind] || "");
    badge.textContent = KIND_LABELS[item.kind] || item.kind;
    card.appendChild(badge);

    const statusPill = document.createElement("span");
    statusPill.className = "status-pill";
    card.appendChild(statusPill);

    const originalPane = buildPane("original", item.original, "original");
    const suggestedPane = buildPane("suggested", item.suggested, "suggested");
    card.appendChild(originalPane);
    card.appendChild(suggestedPane);

    const actions = document.createElement("div");
    actions.className = "actions";

    const keepBtn = document.createElement("button");
    keepBtn.textContent = "Keep original";
    keepBtn.disabled = item.original === null || item.original === undefined;

    const acceptBtn = document.createElement("button");
    acceptBtn.textContent = "Accept suggested";
    acceptBtn.disabled = item.suggested === null || item.suggested === undefined;

    const editBtn = document.createElement("button");
    editBtn.textContent = "Edit manually";

    actions.appendChild(keepBtn);
    actions.appendChild(acceptBtn);
    actions.appendChild(editBtn);
    card.appendChild(actions);

    const editorWrap = document.createElement("div");
    editorWrap.className = "custom-editor";
    editorWrap.style.display = "none";
    const textarea = document.createElement("textarea");
    const editorActions = document.createElement("div");
    editorActions.className = "editor-actions";
    const useBtn = document.createElement("button");
    useBtn.textContent = "Use this text";
    const cancelBtn = document.createElement("button");
    cancelBtn.textContent = "Cancel";
    editorActions.appendChild(useBtn);
    editorActions.appendChild(cancelBtn);
    editorWrap.appendChild(textarea);
    editorWrap.appendChild(editorActions);
    card.appendChild(editorWrap);

    const STATUS_LABELS = {
      pending: "Pending",
      keep: "✓ Kept original",
      accept: "✓ Accepted",
      custom: "✓ Custom edit",
    };

    function refreshActiveStates() {
      const d = decisions[item.id];
      const action = d ? d.action : null; // null = still pending, no explicit decision yet
      keepBtn.classList.toggle("active", action === "keep");
      acceptBtn.classList.toggle("active", action === "accept");
      editBtn.classList.toggle("active", action === "custom");

      const statusKey = action || "pending";
      statusPill.className = "status-pill status-" + statusKey;
      statusPill.textContent = STATUS_LABELS[statusKey];

      originalPane.classList.toggle("dimmed", action === "accept" || action === "custom");
      suggestedPane.classList.toggle("dimmed", action === "keep" || action === "custom");
    }
    refreshActiveStates();

    keepBtn.addEventListener("click", () => {
      setDecision(item.id, "keep");
      editorWrap.style.display = "none";
      refreshActiveStates();
      if (onChangeCallback) onChangeCallback(resolveText(item), "keep");
    });

    acceptBtn.addEventListener("click", () => {
      setDecision(item.id, "accept");
      editorWrap.style.display = "none";
      refreshActiveStates();
      if (onChangeCallback) onChangeCallback(resolveText(item), "accept");
    });

    editBtn.addEventListener("click", () => {
      textarea.value = resolveText(item);
      editorWrap.style.display = editorWrap.style.display === "none" ? "block" : "none";
      textarea.focus();
    });

    useBtn.addEventListener("click", () => {
      setDecision(item.id, "custom", textarea.value);
      editorWrap.style.display = "none";
      refreshActiveStates();
      if (onChangeCallback) onChangeCallback(resolveText(item), "custom");
    });

    cancelBtn.addEventListener("click", () => {
      editorWrap.style.display = "none";
    });

    return card;
  }

  function buildSentenceGroup(block) {
    const wrap = document.createElement("div");

    const flow = document.createElement("div");
    flow.className = "paragraph-flow";

    const spansById = {};
    block.subsegments.forEach((sub) => {
      if (sub.fixed) {
        const span = document.createElement("span");
        span.innerHTML = inlineMarkdown(sub.text);
        flow.appendChild(span);
        flow.appendChild(document.createTextNode(" "));
      } else {
        const span = document.createElement("span");
        span.className = "inline-change status-pending";
        span.textContent = resolveText(sub);
        spansById[sub.id] = span;
        flow.appendChild(span);
        flow.appendChild(document.createTextNode(" "));
      }
    });
    wrap.appendChild(flow);

    const changed = block.subsegments.filter((s) => !s.fixed);
    if (changed.length) {
      const list = document.createElement("div");
      list.className = "sentence-list";
      changed.forEach((sub) => {
        const card = buildCard(sub, (newText, action) => {
          const span = spansById[sub.id];
          span.textContent = newText;
          span.className = "inline-change status-" + action;
        });
        list.appendChild(card);
      });
      wrap.appendChild(list);
    }
    return wrap;
  }

  // --- Insert-new-block UI (image / table / text), attachable to any point

  function attachInsertControl(toolbar, key, anchorEl) {
    insertAnchors[key] = anchorEl;

    const insertToggle = document.createElement("button");
    insertToggle.className = "block-edit-toggle";
    insertToggle.type = "button";
    insertToggle.textContent = "+ Insert";
    toolbar.appendChild(insertToggle);

    const panel = document.createElement("div");
    panel.className = "insert-panel";
    panel.style.display = "none";

    const chooser = document.createElement("div");
    chooser.className = "insert-chooser";
    const imgBtn = document.createElement("button");
    imgBtn.textContent = "Image";
    const tableBtn = document.createElement("button");
    tableBtn.textContent = "Table";
    const textBtn = document.createElement("button");
    textBtn.textContent = "Text";
    chooser.append(imgBtn, tableBtn, textBtn);
    panel.appendChild(chooser);

    const editorArea = document.createElement("div");
    editorArea.className = "custom-editor";
    editorArea.style.display = "none";
    const ta = document.createElement("textarea");
    const confirmRow = document.createElement("div");
    confirmRow.className = "editor-actions";
    const confirmBtn = document.createElement("button");
    confirmBtn.textContent = "Insert here";
    const cancelBtn = document.createElement("button");
    cancelBtn.textContent = "Cancel";
    confirmRow.append(confirmBtn, cancelBtn);
    editorArea.append(ta, confirmRow);
    panel.appendChild(editorArea);

    function closePanel() {
      editorArea.style.display = "none";
      chooser.style.display = "flex";
      panel.style.display = "none";
    }

    function openEditor(prefill) {
      chooser.style.display = "none";
      editorArea.style.display = "block";
      ta.value = prefill;
      ta.focus();
    }

    insertToggle.addEventListener("click", () => {
      panel.style.display = panel.style.display === "none" ? "block" : "none";
    });

    tableBtn.addEventListener("click", () => {
      openEditor("| Column 1 | Column 2 |\n|---|---|\n| Value | Value |");
    });

    textBtn.addEventListener("click", () => {
      openEditor("New paragraph text.");
    });

    imgBtn.addEventListener("click", () => {
      pickImageFile().then((file) => {
        if (!file) return;
        uploadImageFile(file)
          .then((relPath) => {
            const caption = window.prompt("Caption for this figure (optional):", "") || "";
            const figcaption = caption ? `\n<figcaption><p>${caption}</p></figcaption>` : "";
            openEditor(`<figure>\n<img src="${relPath}" alt="${caption || "Inserted image"}" />${figcaption}\n</figure>`);
          })
          .catch((err) => window.alert("Upload failed: " + err));
      });
    });

    cancelBtn.addEventListener("click", closePanel);

    confirmBtn.addEventListener("click", () => {
      const text = ta.value.trim();
      if (!text) return;
      if (!insertions[key]) insertions[key] = [];
      insertions[key].push(text);

      const previewWrap = document.createElement("div");
      previewWrap.className = "block-wrapper inserted-preview";
      const label = document.createElement("div");
      label.className = "section-label";
      label.textContent = "Newly inserted (not yet saved)";
      const contentHolder = buildFixedContent(text, classifyBlockJs(text));
      const removeBtn = document.createElement("button");
      removeBtn.className = "block-edit-toggle";
      removeBtn.textContent = "Remove this insertion";
      previewWrap.append(label, contentHolder, removeBtn);

      removeBtn.addEventListener("click", () => {
        const idx = insertions[key].indexOf(text);
        if (idx !== -1) insertions[key].splice(idx, 1);
        if (insertAnchors[key] === previewWrap) insertAnchors[key] = previewWrap.previousElementSibling;
        previewWrap.remove();
        updateCounts();
      });

      insertAnchors[key].insertAdjacentElement("afterend", previewWrap);
      insertAnchors[key] = previewWrap;
      closePanel();
      updateCounts();
    });

    return panel;
  }

  // Wraps any top-level block's normal content with hover-revealed controls:
  // "Edit block" (free-text override for the whole block), "+ Insert" (add a
  // brand-new image/table/text block right after this one), and, for blocks
  // that contain a figure, "Replace image" / "Edit caption". A manual
  // override always wins over whatever the diff/decision logic would
  // otherwise produce for that block.
  function wrapBlock(block, contentEl) {
    const wrapper = document.createElement("div");
    wrapper.className = "block-wrapper";

    const toolbar = document.createElement("div");
    toolbar.className = "block-toolbar";
    wrapper.appendChild(toolbar);

    const editToggle = document.createElement("button");
    editToggle.className = "block-edit-toggle";
    editToggle.type = "button";
    editToggle.textContent = "Edit block";
    toolbar.appendChild(editToggle);

    const contentHolder = document.createElement("div");
    contentHolder.className = "block-content";
    contentHolder.appendChild(contentEl);
    wrapper.appendChild(contentHolder);

    function replaceContent(newText) {
      contentHolder.innerHTML = "";
      contentHolder.appendChild(buildFixedContent(newText, block.block_type));
    }

    if (block.fixed && /<img\b/i.test(block.text)) {
      const replaceImgBtn = document.createElement("button");
      replaceImgBtn.className = "block-edit-toggle";
      replaceImgBtn.type = "button";
      replaceImgBtn.textContent = "Replace image";
      toolbar.appendChild(replaceImgBtn);
      replaceImgBtn.addEventListener("click", () => {
        pickImageFile().then((file) => {
          if (!file) return;
          uploadImageFile(file)
            .then((relPath) => {
              const newText = currentBlockText(block).replace(/src="[^"]*"/, `src="${relPath}"`);
              blockOverrides[block.id] = newText;
              replaceContent(newText);
              wrapper.classList.add("has-override");
              updateCounts();
            })
            .catch((err) => window.alert("Upload failed: " + err));
        });
      });

      // Committed once per drag (see attachImageResizeHandle's "pointerup"),
      // not on every mousemove — cheap enough to just rebuild the block.
      contentHolder.addEventListener("image-resized", (e) => {
        const { idx, widthIn, heightIn } = e.detail;
        const newText = replaceImgSizeAtIndex(currentBlockText(block), idx, widthIn, heightIn);
        blockOverrides[block.id] = newText;
        replaceContent(newText);
        wrapper.classList.add("has-override");
        updateCounts();
      });
    }

    if (block.fixed && /<figcaption\b/i.test(block.text)) {
      const editCaptionBtn = document.createElement("button");
      editCaptionBtn.className = "block-edit-toggle";
      editCaptionBtn.type = "button";
      editCaptionBtn.textContent = "Edit caption";
      toolbar.appendChild(editCaptionBtn);
      editCaptionBtn.addEventListener("click", () => {
        const current = currentBlockText(block);
        const match = current.match(/<figcaption>\s*<p>([\s\S]*?)<\/p>\s*<\/figcaption>/i);
        const existing = match ? match[1] : "";
        const next = window.prompt("Edit caption text:", existing);
        if (next === null) return;
        const newText = match
          ? current.replace(match[0], `<figcaption><p>${next}</p></figcaption>`)
          : current;
        blockOverrides[block.id] = newText;
        replaceContent(newText);
        wrapper.classList.add("has-override");
        updateCounts();
      });
    }

    attachInsertControl(toolbar, block.id, wrapper);

    const editorWrap = document.createElement("div");
    editorWrap.className = "custom-editor block-override-editor";
    editorWrap.style.display = "none";
    const textarea = document.createElement("textarea");
    const editorActions = document.createElement("div");
    editorActions.className = "editor-actions";
    const useBtn = document.createElement("button");
    useBtn.textContent = "Use this text";
    const cancelBtn = document.createElement("button");
    cancelBtn.textContent = "Cancel";
    editorActions.appendChild(useBtn);
    editorActions.appendChild(cancelBtn);
    editorWrap.appendChild(textarea);
    editorWrap.appendChild(editorActions);
    wrapper.appendChild(editorWrap);

    const banner = document.createElement("div");
    banner.className = "override-banner";
    banner.style.display = "none";
    const bannerPane = document.createElement("div");
    bannerPane.className = "pane suggested";
    const revertBtn = document.createElement("button");
    revertBtn.textContent = "Revert to reviewed content";
    banner.appendChild(bannerPane);
    banner.appendChild(revertBtn);
    wrapper.appendChild(banner);

    editToggle.addEventListener("click", () => {
      textarea.value = currentBlockText(block);
      editorWrap.style.display = editorWrap.style.display === "none" ? "block" : "none";
      textarea.focus();
    });

    cancelBtn.addEventListener("click", () => {
      editorWrap.style.display = "none";
    });

    useBtn.addEventListener("click", () => {
      blockOverrides[block.id] = textarea.value;
      editorWrap.style.display = "none";
      contentHolder.style.display = "none";
      bannerPane.textContent = textarea.value || "(block will be removed entirely)";
      banner.style.display = "block";
      wrapper.classList.add("has-override");
      updateCounts();
    });

    revertBtn.addEventListener("click", () => {
      delete blockOverrides[block.id];
      banner.style.display = "none";
      contentHolder.style.display = "";
      wrapper.classList.remove("has-override");
      updateCounts();
    });

    // A restored last-save may already have an override for this block
    // (see the /api/diff handler above) — reflect that immediately instead
    // of requiring the user to re-click "Use this text".
    if (block.id !== undefined && Object.prototype.hasOwnProperty.call(blockOverrides, block.id)) {
      const restored = blockOverrides[block.id];
      contentHolder.style.display = "none";
      bannerPane.textContent = restored || "(block will be removed entirely)";
      banner.style.display = "block";
      wrapper.classList.add("has-override");
    }

    return wrapper;
  }

  function render(blocks) {
    docEl.innerHTML = "";
    // render() can now run more than once per page load (bulk-accept actions
    // re-render to refresh every card at once) — reset the tooltip-index
    // arrays each time so they don't grow unbounded across re-renders.
    tooltipContents.length = 0;
    tooltipMarkerKeys.length = 0;

    const topBar = document.createElement("div");
    topBar.className = "block-toolbar top-insert-toolbar";
    docEl.appendChild(topBar);
    attachInsertControl(topBar, "__start__", topBar);

    blocks.forEach((block) => {
      let contentEl;
      if (block.fixed) {
        contentEl = buildFixedContent(block.text, block.block_type);
      } else if (block.type === "sentence_group") {
        contentEl = buildSentenceGroup(block);
      } else {
        contentEl = buildCard(block);
      }
      docEl.appendChild(wrapBlock(block, contentEl));
    });
    updateCounts();
  }

  function save() {
    const total = totalChanges;
    const decided = decidedCount();
    // Only warn when the fallback is "original" — that's the direction that
    // can silently discard work. When it's "suggested", an undecided change
    // just keeps the already-correct text, which is always safe to save.
    if (total > 0 && decided < total && defaultAction !== "suggested") {
      const proceed = confirm(
        `You've reviewed ${decided} of ${total} changes; the rest will default ` +
          `to Keep Original. Save anyway?`
      );
      if (!proceed) return;
    }
    const originalBtnLabel = saveBtn.textContent;
    saveBtn.disabled = true;
    saveBtn.classList.remove("btn-success");
    showSaveToast("Saving…", "state-saving", false);
    fetch("/api/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decisions, block_overrides: blockOverrides, insertions, citation_decisions: citationDecisions }),
    })
      .then((r) => r.json())
      .then((data) => {
        saveBtn.disabled = false;
        if (data.status === "ok") {
          const { flag, remove } = citationDecisionCounts();
          const followupCount = flag + remove;
          showSaveToast(
            `✓ Saved to ${data.written_path}` +
              (data.backup_path ? ` (backup: ${data.backup_path})` : "") +
              (data.followup_report_path
                ? ` · ${followupCount} citation(s) need follow-up — see ${data.followup_report_path}`
                : ""),
            "state-success",
            true
          );
          saveBtn.textContent = "✓ Saved";
          saveBtn.classList.add("btn-success");
          save._resetTimer = setTimeout(() => {
            saveBtn.textContent = originalBtnLabel;
            saveBtn.classList.remove("btn-success");
          }, 2000);
        } else {
          showSaveToast("✗ Error: " + (data.error || "unknown"), "state-error", true);
        }
      })
      .catch((err) => {
        saveBtn.disabled = false;
        showSaveToast("✗ Error: " + err, "state-error", true);
      });
  }

  saveBtn.addEventListener("click", save);

  // --- Download (PDF/Word/Markdown, as-shown or clean) ---------------------
  // Reuses whatever's currently decided in the review UI (same decisions/
  // block_overrides/insertions shape as save()), but never writes to disk —
  // /api/export just renders it and streams the file back.
  function extractFilename(contentDisposition, fallback) {
    const match = /filename="([^"]+)"/.exec(contentDisposition || "");
    return match ? match[1] : fallback;
  }

  function triggerBlobDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function downloadDocument(format, clean, btn) {
    const originalLabel = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Preparing…";
    showSaveToast(`Preparing ${format.toUpperCase()} (${clean ? "clean" : "as shown"})…`, "state-saving", false);
    fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decisions, block_overrides: blockOverrides, insertions, format, clean }),
    })
      .then(async (r) => {
        if (!r.ok) {
          const data = await r.json().catch(() => ({}));
          throw new Error(data.error || `export failed (HTTP ${r.status})`);
        }
        const blob = await r.blob();
        const filename = extractFilename(r.headers.get("Content-Disposition"), `document.${format}`);
        triggerBlobDownload(blob, filename);
        showSaveToast(`✓ Downloaded ${filename}`, "state-success", true);
      })
      .catch((err) => showSaveToast("✗ " + err.message, "state-error", true))
      .finally(() => {
        btn.disabled = false;
        btn.textContent = originalLabel;
        downloadMenu.removeAttribute("open");
      });
  }

  if (downloadMenu) {
    downloadMenu.querySelectorAll(".dl-menu-list button").forEach((btn) => {
      btn.addEventListener("click", () => {
        downloadDocument(btn.dataset.format, btn.dataset.clean === "1", btn);
      });
    });
    document.addEventListener("click", (e) => {
      if (downloadMenu.hasAttribute("open") && !downloadMenu.contains(e.target)) {
        downloadMenu.removeAttribute("open");
      }
    });
  }
  if (printBtn) printBtn.addEventListener("click", () => window.print());

  // --- Bulk actions --------------------------------------------------------
  // Both bulk-accept buttons follow the same shape: find what qualifies,
  // confirm the count with the user (so a stray click can't silently accept
  // everything), apply, then offer to save immediately afterward.

  function offerSaveAfterBulkAction() {
    if (window.confirm("Save the document now?")) save();
  }

  function acceptAllRecommendedCitations() {
    const seen = new Set();
    const keys = [];
    tooltipMarkerKeys.forEach((key) => {
      if (seen.has(key)) return;
      seen.add(key);
      const entry = citationRegister[key];
      const verdict = entry && entry._recommendation ? entry._recommendation.verdict : null;
      const recButton = verdict ? VERDICT_TO_BUTTON[verdict] : null;
      const already = citationDecisions[key] && citationDecisions[key].decision === "accept";
      if (recButton === "accept" && !already) keys.push(key);
    });
    if (!keys.length) {
      window.alert('No citations are currently recommended "Looks good" (or they\'re already accepted).');
      return;
    }
    if (!window.confirm(`Accept all ${keys.length} citation(s) marked "Looks good (Recommended)"?`)) return;
    keys.forEach((key) => {
      citationDecisions[key] = {
        decision: "accept",
        note: (citationDecisions[key] && citationDecisions[key].note) || "",
        decided_at: new Date().toISOString(),
      };
      updateMarkerDecisionIndicators(key);
    });
    updateCitationCounts();
    offerSaveAfterBulkAction();
  }

  // Flattens the current document into the same "change item" shape
  // buildCard() consumes, whether it arrived as a standalone block or
  // bundled inside a sentence_group's subsegments.
  function allChangeItems() {
    const items = [];
    (currentBlocksData || []).forEach((block) => {
      if (block.fixed) return;
      if (block.type === "sentence_group") {
        (block.subsegments || []).forEach((sub) => { if (!sub.fixed) items.push(sub); });
      } else {
        items.push(block);
      }
    });
    return items;
  }

  function acceptAllSuggested() {
    const items = allChangeItems().filter((item) => {
      if (item.suggested === null || item.suggested === undefined) return false;
      const d = decisions[item.id];
      return !d || d.action !== "accept";
    });
    if (!items.length) {
      window.alert("No pending changes have suggested text to accept (or they're already accepted).");
      return;
    }
    if (!window.confirm(`Accept the suggested text for all ${items.length} pending change(s)?`)) return;
    items.forEach((item) => setDecision(item.id, "accept"));
    render(currentBlocksData);
    offerSaveAfterBulkAction();
  }

  const acceptAllCitationsBtn = document.getElementById("accept-all-citations-btn");
  const acceptAllSuggestedBtn = document.getElementById("accept-all-suggested-btn");
  if (acceptAllCitationsBtn) acceptAllCitationsBtn.addEventListener("click", acceptAllRecommendedCitations);
  if (acceptAllSuggestedBtn) acceptAllSuggestedBtn.addEventListener("click", acceptAllSuggested);

  Promise.all([
    fetch("/api/diff").then((r) => r.json()),
    fetch("/api/citations").then((r) => r.json()).catch(() => ({ citations: {} })),
  ])
    .then(([data, citationData]) => {
      citationRegister = citationData.citations || {};
      totalChanges = data.total_changes;
      outputPath = data.output_path;
      defaultAction = data.default_action || "original";
      // Restore whatever the last successful save on this server process
      // decided, so reloading the page shows the same reviewed state
      // instead of every change looking pending again. (Note: previously
      // inserted new blocks aren't replayed into the DOM here, only
      // decisions/manual overrides — insertions are still safely on disk,
      // just not re-shown as "newly inserted" preview cards on reload.)
      if (data.last_save) {
        Object.assign(decisions, data.last_save.decisions || {});
        Object.assign(blockOverrides, data.last_save.block_overrides || {});
        Object.assign(citationDecisions, data.last_save.citation_decisions || {});
      }
      saveBtn.disabled = false;
      currentBlocksData = data.blocks;
      render(currentBlocksData);
      // Marker spans now exist in the DOM (render() ran wrapCitationMarkers
      // for every block) — reflect any restored citation decisions on them.
      Object.keys(citationDecisions).forEach(updateMarkerDecisionIndicators);
      updateCitationCounts();
      if (data.last_save) {
        showSaveToast(
          `✓ Restored last save (${data.last_save.saved_at || "unknown time"} ` +
            `→ ${data.last_save.written_path})`,
          "state-success",
          true
        );
      }
    })
    .catch((err) => {
      countsEl.textContent = "Failed to load diff: " + err;
    });
})();
