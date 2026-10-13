"""The general page verifier of V0.6 section 11: `page_match`, `scoped_extract`, `revisit_match`.

A criterion names an object by a TextSlot (the anchor, e.g. "Заказ №125") and a field by its visible
name (e.g. "Notes"). The probe below looks for them in the live DOM of the main frame and binds them
only through structure: a container the anchor names (its first heading, or its `aria-label`), or one
table row. Two strings that merely sit near each other are not bound. Several anchors, several fields,
a field without a value, frames and shadow roots give `unknown`, never a guess.

The probe is the adapter's own read-only script (section 7: trusted observation code). It returns page
text to this process only; the journal records statuses and counts, never the values.
"""

from __future__ import annotations

import re

PROBE = r"""
(function (anchorText, fieldName) {
  const norm = s => String(s || "").replace(/\s+/g, " ").trim();
  const bare = s => norm(s).replace(/\s*:$/, "");
  const shown = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
  const HEADINGS = new Set(["H1", "H2", "H3", "H4", "H5", "H6"]);
  const level = el => HEADINGS.has(el.tagName) ? Number(el.tagName[1])
    : (el.getAttribute("role") === "heading" ? Number(el.getAttribute("aria-level") || 2) : 0);
  if (!document.body) return {state: "unknown", reason: "no_document"};
  const all = Array.from(document.body.querySelectorAll("*")).filter(shown);
  const frames = document.querySelectorAll("iframe,frame").length;
  const shadows = all.filter(el => el.shadowRoot).length;

  // The anchor: the innermost visible element whose whole text is the slot value, or whose aria-label is.
  const anchors = all.filter(el =>
    norm(el.getAttribute("aria-label")) === anchorText ||
    (norm(el.textContent) === anchorText && !Array.from(el.children).some(c => norm(c.textContent) === anchorText)));
  const base = {anchors: anchors.length, frames: frames, shadow_roots: shadows};
  if (anchors.length !== 1) return Object.assign(base, {state: "unknown", reason: anchors.length ? "several_anchors" : "no_anchor"});
  const anchor = anchors[0];

  // Field names: headings, labels, terms, header cells, legends, and named regions.
  const LABELS = new Set(["LABEL", "DT", "TH", "LEGEND", "CAPTION"]);
  const labels = all.filter(el => el !== anchor && !el.contains(anchor) &&
    (((HEADINGS.has(el.tagName) || LABELS.has(el.tagName) || el.getAttribute("role") === "heading") &&
      bare(el.textContent) === fieldName) || norm(el.getAttribute("aria-label")) === fieldName));

  const valuesOf = (label) => {
    const text = el => norm(el.innerText);   // rendered text: hidden descendants are not evidence
    const tag = label.tagName;
    if (tag === "LABEL") {
      const control = label.htmlFor ? document.getElementById(label.htmlFor) : label.querySelector("input,textarea,select");
      if (!control) return null;
      if (control.tagName === "SELECT") return Array.from(control.selectedOptions).map(text);
      return [String(control.value || "")];
    }
    if (tag === "DT") {
      const out = [];
      for (let n = label.nextElementSibling; n && n.tagName !== "DT"; n = n.nextElementSibling)
        if (n.tagName === "DD" && shown(n)) out.push(text(n));
      return out.length ? out : null;
    }
    if (level(label)) {
      let next = label.nextElementSibling;
      while (next && !shown(next)) next = next.nextElementSibling;
      if (!next || level(next)) return null;
      if (["UL", "OL"].includes(next.tagName))
        return Array.from(next.children).filter(c => c.tagName === "LI" && shown(c)).map(text);
      if (next.tagName === "DL") return Array.from(next.querySelectorAll("dd")).filter(shown).map(text);
      return [text(next)];
    }
    if (label.getAttribute("aria-label") !== null && norm(label.getAttribute("aria-label")) === fieldName)
      return [text(label)];
    return null;
  };

  // A table: the header cell's column in the anchor's own row.
  const row = anchor.closest("tr");
  if (row) {
    const table = row.closest("table");
    const heads = labels.filter(l => l.tagName === "TH" && l.closest("table") === table && !row.contains(l));
    if (heads.length > 1) return Object.assign(base, {state: "unknown", reason: "several_fields"});
    if (heads.length === 1) {
      if (table.querySelector("[colspan]:not([colspan='1']),[rowspan]:not([rowspan='1'])"))
        return Object.assign(base, {state: "unknown", reason: "merged_cells"});
      const cell = row.cells[heads[0].cellIndex];
      if (!cell || cell.contains(anchor)) return Object.assign(base, {state: "unknown", reason: "no_value"});
      return Object.assign(base, {state: "bound", binding: "table_row", values: [norm(cell.innerText)]});
    }
  }

  // Otherwise: the nearest container holding a field name must be one the anchor names.
  for (let box = anchor.parentElement; box; box = box.parentElement) {
    const inside = labels.filter(l => box.contains(l));
    if (!inside.length) continue;
    if (inside.length > 1) return Object.assign(base, {state: "unknown", reason: "several_fields"});
    const firstHeading = Array.from(box.querySelectorAll("*")).find(el => shown(el) && level(el));
    const named = norm(box.getAttribute("aria-label")) === anchorText ||
      (firstHeading && (firstHeading === anchor || firstHeading.contains(anchor)));
    if (!named) return Object.assign(base, {state: "unknown", reason: "no_structural_link"});
    const values = valuesOf(inside[0]);
    if (values === null) return Object.assign(base, {state: "unknown", reason: "no_value"});
    return Object.assign(base, {state: "bound", binding: "named_container", values: values});
  }
  return Object.assign(base, {state: "unknown", reason: labels.length ? "no_structural_link" : "no_field"});
})
"""

PREDICATES = ("page_match", "scoped_extract", "revisit_match")
_SPACE = re.compile(r"\s+")


def normalized(text: str) -> str:
    return _SPACE.sub(" ", str(text or "")).strip()


def judge(predicate: str, probe: dict | None, expected: str | None) -> dict:
    """pass/fail/unknown for one criterion from one probe result. Values stay out of the returned
    `facts` (they go to the journal); an extracted value is returned under `value` for `outputs`."""
    if type(probe) is not dict:
        return {"status": "unknown", "reason": "probe_failed", "facts": {}}
    facts = {k: probe[k] for k in ("anchors", "frames", "shadow_roots", "binding") if k in probe}
    if probe.get("state") != "bound":
        return {"status": "unknown", "reason": str(probe.get("reason") or "not_bound"), "facts": facts}
    values = [normalized(v) for v in probe.get("values") or []]
    facts["values"] = len(values)
    if predicate == "scoped_extract":
        present = [v for v in values if v]
        if len(present) > 1:
            return {"status": "unknown", "reason": "several_values", "facts": facts}
        if not present:
            return {"status": "fail", "reason": "empty_value", "facts": facts}
        return {"status": "pass", "reason": "value_extracted", "facts": facts, "value": present[0]}
    want = normalized(expected)
    matches = sum(1 for v in values if v == want)
    facts["matches"] = matches
    if matches:
        return {"status": "pass", "reason": "value_matches", "facts": facts}
    return {"status": "fail", "reason": "value_differs", "facts": facts}
