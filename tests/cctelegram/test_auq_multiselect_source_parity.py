"""Source-parity regression tests for the multi-select AUQ "fast button".

Root-cause coverage for the silent multi-select toggle failure: when the bot's
AUQ source flips from the PreToolUse side file to the raw pane between render
(mint) and tap (validate), two parser asymmetries used to break the toggle's
fingerprint match:

  1. ``_parse_numbered_options`` INCLUDED the "Type something" / "Chat about
     this" free-text affordances as real numbered options, while the side-file
     source and the pane-signal classifiers EXCLUDED them — so a pure-pane parse
     carried N+1 options vs the side file's N → fingerprint mismatch → the
     toggle gate rejected the tap with no digit sent.
  2. ``parse_ask_user_question`` hardcoded ``options_complete=False`` on the
     pure-pane path, so even with the option count reconciled the toggle gate
     (``not current_form.options_complete``) and the mint suppress gate hid the
     buttons on re-render.

These tests pin the corrected parser behavior using the REAL captured fixture
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

    def test_source_flip_fingerprint_parity(self):
        # THE regression test for the bug. A render→tap source flip (side file →
        # pure pane) used to diverge on FOUR things: the affordance count
        # (Part A), the hardcoded ``options_complete=False`` (Part B), the
        # single-question tab strip, and the trailing ``?`` on the pane title
        # (Part D — ``_canonical_repr`` now omits ``TABS`` for single-question
        # forms and normalizes the title). With all four closed, the FULL form
        # fingerprint is byte-identical across the flip, so a toggle minted from
        # the side-file render validates cleanly against a pure-pane tap.
        pane = _multiselect_pane()
        side_form = resolve_ask_form(_SIDE_FILE, pane)
        pane_form = resolve_ask_form(None, pane)
        assert side_form is not None
        assert pane_form is not None
        assert len(side_form.options) == 4
        assert len(pane_form.options) == 4
        assert side_form.options_complete is True
        assert pane_form.options_complete is True
        assert side_form.select_mode == "multi"
        assert pane_form.select_mode == "multi"

        # Full canonical + fingerprint parity across the source flip — the whole
        # point of the fix. A mismatch here is the silent-toggle-reject bug.
        assert side_form._canonical_repr() == pane_form._canonical_repr()
        assert side_form.fingerprint() == pane_form.fingerprint()

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
