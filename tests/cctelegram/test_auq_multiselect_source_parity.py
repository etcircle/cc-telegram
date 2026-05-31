"""Source-stickiness regression tests for the multi-select AUQ "fast button".

Root-cause coverage for the silent multi-select toggle failure: when the bot's
AUQ source flips from the PreToolUse side file to the raw pane between render
(mint) and tap (validate), the toggle gate rejected the tap with no digit sent
("fast buttons dead / options unselectable"). Two parser asymmetries are now
closed at the parse layer:

  1. ``_parse_numbered_options`` INCLUDED the "Type something" / "Chat about
     this" free-text affordances as real numbered options, while the side-file
     source and the pane-signal classifiers EXCLUDED them — so a pure-pane parse
     carried N+1 options vs the side file's N. (Part A — kept.)
  2. ``parse_ask_user_question`` hardcoded ``options_complete=False`` on the
     pure-pane path, so even with the option count reconciled the toggle gate
     (``not current_form.options_complete``) and the mint suppress gate hid the
     buttons on re-render. (Part B — kept.)

But the side-file form and the pure-pane form for the SAME single question still
LEGITIMATELY differ in their full fingerprint (the live TUI paints a one-cell
``TABS:`` strip and a trailing ``?`` on the title that the JSONL/side-file
question text omits). The robust fix is NOT to force those two parser-only paths
to fingerprint-match (two prior dual-reviews failed that approach — the pane
simply cannot reconstruct the side file's full form). Instead, at TAP the
``aqt:`` handler PINS the source the button was minted against
(``auq_source.peek_sticky_source``): if the side file is still live and
unchanged, the tap re-resolves the SAME side-file source the render used, so the
tap-time form fingerprint equals the render-time form fingerprint and the toggle
dispatches. A genuinely-changed question fingerprints differently → no pin →
fall back to ``resolve_auq_source`` → the staleness gate still catches the real
change.

These tests pin the corrected parser behavior + the source-stickiness invariant
using the REAL captured fixture
``auq_multiselect_long_scrolled_toggled_S500.txt`` (a long multi-select picker
scrolled to cursor=4 with options 1,2 toggled ``[✔]``; the "Type something" /
"Chat about this" affordances trail the four real options).
"""

from __future__ import annotations

from pathlib import Path

from cctelegram.terminal_parser import parse_ask_user_question, resolve_ask_form

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_MULTISELECT_PANE = _FIXTURE_DIR / "auq_multiselect_long_scrolled_toggled_S500.txt"
_SINGLE_AFFORDANCE_PANE = _FIXTURE_DIR / "auq_single_select_with_affordances_pane.txt"

# The PreToolUse side file carries exactly the four visible REAL labels — no
# "Type something" / "Chat about this" (those are picker-internal affordances
# that Claude Code never writes into the tool_use payload).
_SIDE_FILE = {
    "questions": [
        {
            "question": "Select safeguards to ship",
            "header": "Safeguards",
            "multiSelect": True,
            "options": [
                {"label": "A) Verify cursor row"},
                {"label": "B) Keep side file alive"},
                {"label": "C) Suppress tabbed forms"},
                {"label": "D) Ledger the final Enter"},
            ],
        }
    ]
}


def _multiselect_pane() -> str:
    return _MULTISELECT_PANE.read_text()


class TestMultiSelectSourceParity:
    def test_parse_excludes_type_something_affordance(self):
        # Part A: the pure-pane parse drops the free-text affordances and keeps
        # exactly the four real options, contiguous 1..4, multi-select.
        form = parse_ask_user_question(_multiselect_pane())
        assert form is not None
        assert [o.number for o in form.options] == [1, 2, 3, 4]
        labels = [o.label for o in form.options]
        assert "Type something" not in labels
        assert "Chat about this" not in labels
        assert form.select_mode == "multi"
        assert form.is_free_text is True

    def test_pane_multiselect_options_complete_true(self):
        # Part B: a contiguous-from-1 picker that also shows the bottom-of-list
        # "Type something" affordance (is_free_text) is "complete".
        form = parse_ask_user_question(_multiselect_pane())
        assert form is not None
        assert form.options_complete is True

    def test_source_stickiness_pins_side_file_form_across_tap(self):
        # THE regression test for the bug, re-cast for the source-stickiness fix.
        #
        # The render mints the toggle against the SIDE-FILE form
        # (``resolve_ask_form(_SIDE_FILE, pane)``). At tap, the ``aqt:`` handler
        # PINS that same side-file source (``peek_sticky_source`` returns the
        # unchanged ``_SIDE_FILE`` tool_input), so the tap re-resolves with the
        # SAME ``_SIDE_FILE`` payload. That makes the tap-time form fingerprint
        # IDENTICAL to the render-time form fingerprint — which is exactly what
        # keeps the toggle gate from rejecting (the gate compares
        # ``current_form.fingerprint() != entry.fingerprint``).
        pane = _multiselect_pane()
        render_form = resolve_ask_form(_SIDE_FILE, pane)
        assert render_form is not None
        assert len(render_form.options) == 4
        assert render_form.options_complete is True
        assert render_form.select_mode == "multi"

        # Tap with the SAME pinned side-file source (what Part E guarantees) →
        # byte-identical fingerprint vs render. A mismatch here would be the
        # silent-toggle-reject bug.
        tap_form_sticky = resolve_ask_form(_SIDE_FILE, pane)
        assert tap_form_sticky is not None
        assert tap_form_sticky.fingerprint() == render_form.fingerprint()

        # WHY stickiness is needed: the pure-pane tap path (the flip the bug
        # hit) legitimately DIVERGES from the side-file render fingerprint —
        # the live single-question TUI paints a ``TABS:`` strip + trailing
        # ``?`` that the side-file question text omits. Without the pin, this
        # divergence is the dead button. (Parser-only parity is intentionally
        # NOT enforced; the pin is the fix.)
        pane_form = resolve_ask_form(None, pane)
        assert pane_form is not None
        assert len(pane_form.options) == 4
        assert pane_form.options_complete is True
        assert pane_form.select_mode == "multi"
        assert pane_form.fingerprint() != render_form.fingerprint()

    def test_overlay_still_correct(self):
        # Guards against Part A regressing the cursor/selection overlay: the
        # side-file resolution must reflect the fixture's visible pane state —
        # cursor on option 4; selected 1=True,2=True,3=False,4=False.
        form = resolve_ask_form(_SIDE_FILE, _multiselect_pane())
        assert form is not None
        assert [o.number for o in form.options if o.cursor] == [4]
        assert [(o.number, o.selected) for o in form.options] == [
            (1, True),
            (2, True),
            (3, False),
            (4, False),
        ]

    def test_single_select_pane_excludes_type_something(self):
        # Focused single-select check: the affordance-bearing single-select pane
        # ("4. Type something.", "5. Chat about this") parses to four real
        # options only, none of them an affordance.
        form = parse_ask_user_question(_SINGLE_AFFORDANCE_PANE.read_text())
        assert form is not None
        labels = [o.label for o in form.options]
        assert "Type something" not in labels
        assert "Type something." not in labels
        assert "Chat about this" not in labels
        assert [o.number for o in form.options] == [1, 2, 3]
        assert form.select_mode == "single"
