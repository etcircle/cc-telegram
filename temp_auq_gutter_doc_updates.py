#!/usr/bin/env python3
"""Stage the v0.4.8 AUQ-gutter doc updates into the GITIGNORED `.claude/` tree.

`.claude/` is gitignored, so these edits cannot ride the commit. This script
applies them idempotently and is asserted at every step: it either makes the
exact documented change or raises with the reason, never a partial write.

Covers:

  1. `.claude/reference/auq-dispatch.md` — a new subsection under the card
     liveness / source-resolution heading documenting (a) the gutter
     canonicalization in the `_record_consistent_with_pane` consistency check
     and `_infer_current_tab_idx`, and (b) the `title_mismatch`-only
     identity-proof override in the ctx gate.
  2. `.claude/reference/auq-dispatch.md` — a CC-VERSION DRIFT note pinning the
     2.1.237 multi-question gutter layout to its captured fixture.

Run from the repo root (or anywhere — the path is resolved from this file):

    python3 temp_auq_gutter_doc_updates.py          # apply
    python3 temp_auq_gutter_doc_updates.py --check  # report only, no writes

Delete this script once the docs are staged.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The repo root is this file's directory, EXCEPT when the file was staged into a
# worktree — then `.claude/` lives in the main checkout. Resolve by probing.
_CANDIDATE_ROOTS = [
    Path(__file__).resolve().parent,
    Path("/Users/felixcardix/dev-workspaces/cc-telegram"),
]

# ── Section 1: the consistency-check + identity-proof contract ───────────────

_ANCHOR_LIVENESS = (
    "## AUQ card liveness, token refresh, source drift, render rescue, "
    "restart recovery, pick dispatch\n"
)

_MARKER_GUTTER = "**Gutter-canonical title comparison (CC 2.1.237)**"

_BLOCK_GUTTER = """
**Gutter-canonical title comparison (CC 2.1.237)** — Claude Code 2.1.237 draws a
MULTI-question AUQ's question text inside a left `│` gutter box, wrapped over
several physical lines:

```
←  ☐ Dynamics  ☐ Todo scope  ☐ #387 ops fix  ✔ Submit  →

│ Dynamics CRM integration: which direction? (Full memo in temp/… Credits on
│ the client tenant unless users hold D365 … regardless.)

❯ 1. Option A spike (Recommended)
```

The multi-tab in-region title scan takes the FIRST non-blank/non-option/non-rule
line, `.strip()`s whitespace ONLY, and breaks — so `current_question_title`
became `'│ Dynamics CRM integration: … Credits on'`: gutter kept, question
clipped mid-sentence. `_record_consistent_with_pane` step 5.b requires a prefix
relation between the record's question and that pane title IN EITHER DIRECTION,
and the leading `│ ` breaks BOTH — every multi-question AUQ on 2.1.237 rejected
with `title_mismatch`, so `resolve_auq_source_for_render` returned a
complete-picker `bail` with `dispatch_trusted=True`, which lands on the ctx
gate's `bail_no_ctx` arm (the `bail` RESCUE arm is gated on
`not dispatch_trusted` and is unreachable there). Result: **the 📋 details card
was NEVER posted**, and the picker preamble fell back to the same clipped,
gutter-prefixed line. Single-question AUQs draw no tab header ⇒
`tab_header_governs` False ⇒ `current_question_title` None ⇒ step 5.b skipped ⇒
IMMUNE, which is why this bit multi-Q only.

`terminal_parser.strip_leading_gutter` is the ONE shared canonicalizer: it drops
a LEADING run of `│ ┃ |` plus following spaces, repeated, ANCHORED AT LINE START
— an interior `│` (a table row, a shell pipeline inside an option label) is
never touched. It is applied **SYMMETRICALLY to BOTH sides** (the repo's
symmetric-normalization-across-sources rule — a one-sided strip only moves the
mismatch) at three comparison sites: `_record_consistent_with_pane` steps 5.a
(candidate selection) and 5.b (the title check), and `_infer_current_tab_idx`'s
primary exact-title leg (which previously degraded silently to the weaker
option-label-overlap leg on every gutter pane).

**COMPARISON-TIME ONLY.** `current_question_title` is NEVER mutated: it feeds
`AskUserQuestionForm._canonical_repr()` → `fingerprint()` and
`decision_prompt_fingerprint`, where the "NO glyph stripping, EVER" rule is
load-bearing — a canonicalized title would rotate every live pick token and pop
still-live cards. Fixture-pinned: the real 2.1.237 capture's form fingerprint is
`c5d50e5fb1c168a3` both before and after the fix. A title that is nothing BUT
gutter glyphs canonicalizes to `""` and is treated as "no title" (it carries no
question text to compare) — a deliberate, documented widening of the skip.

**`pane_question_display_text` (DISPLAY-ONLY, the clip's other face)** — a new
`compare=False` field mirroring the `pane_walkback_title` precedent. The parser
JOINS the consecutive GUTTER-PREFIXED lines (each gutter-stripped, single-space
joined, stopping at the first blank / option / rule / non-gutter line, capped at
`_QUESTION_DISPLAY_MAX_LINES = 8`) into the whole question. Set by
`parse_ask_user_question` ONLY, and only when the first title line ACTUALLY
carries a gutter — every pre-2.1.237 layout leaves it `None` and renders
byte-identically to before. `resolve_ask_form` does NOT propagate it through its
merged-form constructors (same reasoning as `pane_walkback_title`: every
JSONL/side-file overlay path already holds the authoritative, un-clipped
question in `current_question_title`). The two picker-preamble fallback sites
(`interactive_ui._format_auq_context_message`'s single-tab branch and
`_render_ask_user_question`) PREFER it, then `current_question_title`, then
`pane_walkback_title`. **NO fingerprint, render signature or dedup key may
consume it** — pinned by a source-level grep test.

Note the preamble is still capped at `_SELCARD_TITLE_MAX_CHARS` (200): that is a
DELIBERATE pre-existing UX decision (a long question must not push the option
rows off the card). What changed is WHAT gets clipped — a clip of the full
question rather than a clip of an already-clipped physical line. The complete
question lives in the 📋 details card.

**Identity-proof override on a `title_mismatch`-ONLY bail (hardening)** — a
title comparison is a TEXT heuristic, so any future layout change that perturbs
the pane's rendering of the question re-opens this exact silent failure. An
OCCURRENCE IDENTITY is stronger evidence: `auq_source.ctx_source_via_identity_proof`
returns the side-file ctx payload when, and only when, ALL of

  * the rejection reason is EXACTLY `title_mismatch` — every other reason
    (`label_mismatch`, `count_sanity`, `no_candidate`, `no_pane_form`) is
    untouched, because those indicate genuinely different CONTENT, which an id
    match does not excuse; and
  * the side file's own `tool_use_id` is non-empty AND equals an identity the
    bot holds from an **INDEPENDENT** source.

Independence is the load-bearing part. `_current_auq_tool_use_id` and
`_live_auq_tool_use_id` are both side-file-FIRST, so comparing either against
the side file's own id would be TRUE BY CONSTRUCTION — a vacuously-true match
predicate, a bug class this repo has already been bitten by.
`interactive_ui._independent_auq_identity` therefore reads (1) the identity
STAMPED on the published picker card (`_InteractiveMsgMeta.tool_use_id`,
persisted in `interactive_state.json` — the witness that matched the side file
in the 2.1.237 incident), then (2) `_last_auq_tool_use_id` (JSONL-flushed,
usually unset while a picker is live). `None` ⇒ unknown ⇒ NO override, never a
match.

CTX-ONLY: the override does not change `dispatch_trusted`, mint any token, or
mutate `_pretool_ask_records` — a pane whose title could not be reconciled stays
UNTRUSTED for dispatch. **Ordering property:** the independent identity only
exists once a card has been recorded for the route, so the override can never
fire on a FIRST publish — the v0.4.5 details-before-picker defer gate is
untouched and a first tick still defers exactly as before. Disclosed residual:
on a genuine future drift the recovery therefore lands on a LATER tick, after
the defer cap has published the bare picker, so the details card can arrive
AFTER it — strictly better than today's "no details card at all", but not
details-before-picker. Part A is what keeps the 2.1.237 case on the first tick.

"""

# ── Section 2: the CC-version drift note ────────────────────────────────────

_MARKER_DRIFT = "**CC-VERSION DRIFT — 2.1.237 multi-question gutter layout**"

_BLOCK_DRIFT = """
**CC-VERSION DRIFT — 2.1.237 multi-question gutter layout** — 2.1.237 moved a
MULTI-question AUQ's question text into a left `│` gutter box (single-question
pickers are unchanged: no tab header, no gutter, question text absent from
`current_question_title` entirely). FIXTURE-PINNED at
`tests/cctelegram/fixtures/auq_multiq_gutter_pane_v2.1.237.txt` — a real 160x50
capture — driven by `tests/cctelegram/test_auq_gutter_layout_v2_1_237.py` (unit
floor: consistency, the display join, fingerprint non-mutation, tab inference)
and `tests/scenarios/test_auq_gutter_details_card.py` (the Telegram seam:
details card posted, details-BEFORE-picker, un-clipped preamble, and the four
identity-proof cases). The fixture is also baked into the two corpus-completeness
baselines — `test_input_box_predicate._BASELINE_CLASSIFICATIONS` (`no_input_box`:
a live picker replaces the input box, so the delivery gate keeps refusing) and
`fixtures/decision_footerless_corpus_baseline.json` (`AskUserQuestion`).

Re-capture this pane on every CC minor: the gutter glyph, the wrap column and
the tab-header shape are all TUI-drift surfaces. If a future release nests the
question deeper, `strip_leading_gutter`'s alphabet (`│ ┃ |`) is the single place
to widen — and it must stay LINE-START-ANCHORED and applied to BOTH sides.

"""


def _find_root() -> Path:
    for root in _CANDIDATE_ROOTS:
        if (root / ".claude" / "reference" / "auq-dispatch.md").is_file():
            return root
    raise SystemExit(
        "FATAL: could not locate `.claude/reference/auq-dispatch.md` in any of: "
        + ", ".join(str(r) for r in _CANDIDATE_ROOTS)
    )


def _apply(path: Path, marker: str, anchor: str, block: str, *, check: bool) -> str:
    text = path.read_text()

    if marker in text:
        return f"SKIP (already staged): {marker}"

    assert anchor in text, (
        f"FATAL: anchor not found in {path.name}; the doc has drifted and this "
        f"script must be re-derived before use.\n  anchor: {anchor!r}"
    )
    assert text.count(anchor) == 1, (
        f"FATAL: anchor is AMBIGUOUS in {path.name} "
        f"({text.count(anchor)} occurrences) — refusing to guess.\n"
        f"  anchor: {anchor!r}"
    )

    if check:
        return f"WOULD APPLY: {marker}"

    updated = text.replace(anchor, anchor + block, 1)
    assert marker in updated, "FATAL: post-write verification failed (marker absent)"
    assert len(updated) > len(text), "FATAL: post-write verification failed (no growth)"
    path.write_text(updated)

    # Re-read from disk — never trust the in-memory string.
    assert marker in path.read_text(), "FATAL: re-read verification failed"
    return f"APPLIED: {marker}"


def main() -> int:
    check = "--check" in sys.argv[1:]
    root = _find_root()
    doc = root / ".claude" / "reference" / "auq-dispatch.md"

    print(f"root: {root}")
    print(f"doc:  {doc}")
    print()

    # Both blocks insert immediately AFTER the same anchor, so the LAST one
    # applied ends up FIRST in the file. Apply the drift note first so the
    # finished order reads: anchor → contract → drift note.
    for marker, block in (
        (_MARKER_DRIFT, _BLOCK_DRIFT),
        (_MARKER_GUTTER, _BLOCK_GUTTER),
    ):
        print(" ", _apply(doc, marker, _ANCHOR_LIVENESS, block, check=check))

    print()
    print("done." if not check else "check complete (no writes).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
