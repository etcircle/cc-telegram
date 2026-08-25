"""CC 2.1.237 multi-question AUQ gutter layout — the hotfix's unit floor.

Claude Code 2.1.237 draws a MULTI-question AskUserQuestion's question text
inside a left ``│`` gutter box, wrapped across physical lines::

    ←  ☐ Dynamics  ☐ Todo scope  ☐ #387 ops fix  ✔ Submit  →

    │ Dynamics CRM integration: which direction? (Full memo in temp/… Credits on
    │ the client tenant unless users hold D365 … regardless.)

    ❯ 1. Option A spike (Recommended)

Two faces of one bug followed:

  1. the parser's multi-tab title scan keeps the FIRST physical line, whitespace
     -stripped only, so ``current_question_title`` became
     ``"│ Dynamics CRM integration: … Credits on"`` — gutter kept, question
     clipped mid-sentence;
  2. ``auq_source._record_consistent_with_pane`` step 5.b requires a prefix
     relation between the side-file question and that pane title. The leading
     ``"│ "`` breaks it in BOTH directions → ``title_mismatch`` → the render
     resolver bails → the 📋 details card is NEVER posted, and the picker
     preamble falls back to the same clipped, gutter-prefixed line.

Single-question AUQs draw no tab header, so ``current_question_title`` stays
None, step 5.b is skipped, and they were immune — which is why this bit EVERY
multi-question AUQ and no single-question one.

Every test here drives the REAL captured pane
(``fixtures/auq_multiq_gutter_pane_v2.1.237.txt``), not a hand-written mock.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cctelegram import terminal_parser
from cctelegram.handlers import auq_source
from cctelegram.handlers.auq_source import PreToolAskRecord
from cctelegram.terminal_parser import (
    AskOption,
    AskQuestion,
    parse_ask_user_question,
    strip_leading_gutter,
)

_FIXTURES = Path(__file__).parent / "fixtures"
_GUTTER_PANE = _FIXTURES / "auq_multiq_gutter_pane_v2.1.237.txt"

# The pane's OWN option labels (what the 2.1.237 capture renders under the
# "Dynamics" tab). The side-file record must agree with these by slot for the
# label leg (step 5.c) to pass — the title leg is what this hotfix repairs.
_PANE_LABELS = [
    "Option A spike",
    "Option B direct",
    "Entra SSO variant first",
    "Park it",
]

# The FULL question as Claude Code wrote it into the tool_input — the pane
# renders exactly this text, wrapped behind the gutter.
_FULL_QUESTION = (
    "Dynamics CRM integration: which direction? (Full memo in "
    "temp/dynamics_mcp_auth_options_memo.md. Note: metering — Dataverse MCP "
    "calls bill Copilot Credits on the client tenant unless users hold D365 "
    "Premium/M365 Copilot licenses; worth checking their posture regardless.)"
)

# The one-physical-line CLIP the parser keeps as ``current_question_title``.
# Byte-pinned: this is an IDENTITY field (it feeds ``fingerprint()``), so the
# hotfix must leave it EXACTLY as main produced it.
_CLIPPED_TITLE_WITH_GUTTER = (
    "│ Dynamics CRM integration: which direction? (Full memo in "
    "temp/dynamics_mcp_auth_options_memo.md. Note: metering — Dataverse MCP "
    "calls bill Copilot Credits on"
)

# The form fingerprint this pane produced BEFORE the hotfix. Pinned so any
# future change that lets a display-only field leak into the identity canonical
# fails loudly here (a rotated fingerprint pops every live pick token).
_PRE_FIX_FINGERPRINT = "c5d50e5fb1c168a3"


def _opt(number: int, label: str) -> AskOption:
    return AskOption(label=label, recommended=False, cursor=False, number=number)


def _pane_text() -> str:
    return _GUTTER_PANE.read_text()


def _record(question: str, *, tool_use_id: str = "toolu_gutter_1") -> PreToolAskRecord:
    """A PreToolUse side-file record shaped like the real 2.1.237 incident."""
    tool_input: dict[str, Any] = {
        "questions": [
            {
                "question": question,
                "header": "Dynamics",
                "multiSelect": False,
                "options": [
                    {"label": label, "description": f"description for {label}"}
                    for label in _PANE_LABELS
                ],
            },
            {
                "question": "How should we scope the todo list?",
                "header": "Todo scope",
                "multiSelect": False,
                "options": [
                    {"label": "Everything", "description": ""},
                    {"label": "This sprint only", "description": ""},
                ],
            },
        ]
    }
    return PreToolAskRecord(
        tool_input=tool_input,
        session_id="11111111-1111-4111-8111-111111111111",
        tool_use_id=tool_use_id,
        written_at=0.0,
        input_fingerprint="0" * 16,
    )


# ── fixture premise guards ───────────────────────────────────────────────────


class TestFixturePremise:
    def test_fixture_carries_the_gutter_lines(self) -> None:
        """The capture really is the 2.1.237 gutter layout (not a re-render)."""
        text = _pane_text()
        assert "│ Dynamics CRM integration" in text
        assert "│ the client tenant unless users hold D365" in text
        assert "←  ☐ Dynamics" in text  # multi-question tab header

    def test_pane_parses_as_a_complete_multi_tab_picker(self) -> None:
        """Premise for the bug: a COMPLETE picker → the resolver's TRUSTED bail
        branch, which is the one that posts no ctx card at all."""
        form = parse_ask_user_question(_pane_text())
        assert form is not None
        assert len(form.tabs) == 4  # 3 questions + Submit
        assert [o.label for o in form.options] == _PANE_LABELS
        assert form.options_complete is True
        assert auq_source.pane_form_is_complete_picker(form) is True


# ── (c) Part A non-mutation: the identity fields are byte-identical ──────────


class TestIdentityFieldsUnchanged:
    """Part A canonicalizes at COMPARISON time only. ``current_question_title``
    and every fingerprint derived from it must be byte-identical to main."""

    def test_current_question_title_still_carries_the_gutter_clip(self) -> None:
        form = parse_ask_user_question(_pane_text())
        assert form is not None
        assert form.current_question_title == _CLIPPED_TITLE_WITH_GUTTER

    def test_form_fingerprint_is_byte_identical_to_pre_fix(self) -> None:
        form = parse_ask_user_question(_pane_text())
        assert form is not None
        assert form.fingerprint() == _PRE_FIX_FINGERPRINT

    def test_display_text_is_excluded_from_the_identity_canonical(self) -> None:
        """The display field must not reach ``_canonical_repr`` — a display-only
        field inside the identity canonical would rotate live pick tokens."""
        form = parse_ask_user_question(_pane_text())
        assert form is not None
        assert form.pane_question_display_text is not None
        assert form.pane_question_display_text not in form._canonical_repr()

    def test_no_fingerprint_or_dedup_site_references_the_display_field(self) -> None:
        """Source-level guard (Part B's explicit requirement): grep every
        fingerprint / signature / dedup construction for the new field name."""
        src = Path(terminal_parser.__file__).parent
        offenders: list[str] = []
        for path in sorted(src.rglob("*.py")):
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if "pane_question_display_text" not in line:
                    continue
                lowered = line.lower()
                if any(
                    needle in lowered
                    for needle in ("fingerprint", "dedup_key", "signature", "digest")
                ):
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
        assert offenders == []


# ── (b) Part B: the join recovers the FULL question ─────────────────────────


class TestQuestionDisplayJoin:
    def test_join_recovers_the_whole_two_line_question(self) -> None:
        form = parse_ask_user_question(_pane_text())
        assert form is not None
        assert form.pane_question_display_text == _FULL_QUESTION

    def test_join_drops_the_gutter_and_keeps_the_clipped_tail(self) -> None:
        """The clip's missing tail — the concrete user-visible symptom."""
        form = parse_ask_user_question(_pane_text())
        assert form is not None
        display = form.pane_question_display_text
        assert display is not None
        assert "│" not in display
        # The tail that the one-line clip lost.
        assert "worth checking their posture regardless." in display
        assert not display.endswith("Credits on")

    def test_non_gutter_layout_leaves_the_display_field_none(self) -> None:
        """Every pre-2.1.237 layout must render byte-identically to today."""
        pane = "\n".join(
            [
                "←  ☐ Alpha  ☐ Beta  ✔ Submit  →",
                "",
                "Which approach should we take?",
                "",
                "❯ 1. Do it now",
                "  2. Do it later",
                "  3. Type something.",
                "─" * 40,
                "  4. Chat about this",
                "",
                "Enter to select · Tab/Arrow keys to navigate · Esc to cancel",
            ]
        )
        form = parse_ask_user_question(pane)
        assert form is not None
        assert form.current_question_title == "Which approach should we take?"
        assert form.pane_question_display_text is None


# ── Part A: the shared canonicalizer's own contract ─────────────────────────


class TestStripLeadingGutter:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("│ Which direction?", "Which direction?"),
            ("┃ Which direction?", "Which direction?"),
            ("| Which direction?", "Which direction?"),
            ("│ │  Which direction?", "Which direction?"),  # repeated run
            ("   │ Which direction?", "Which direction?"),  # indented box
            ("Which direction?", "Which direction?"),  # no gutter → untouched
            ("│", ""),  # gutter-only carries no question text
            ("│   ", ""),
            ("", ""),
        ],
    )
    def test_leading_runs_only(self, raw: str, expected: str) -> None:
        assert strip_leading_gutter(raw) == expected

    def test_interior_glyphs_are_never_touched(self) -> None:
        """A pipe INSIDE the text (a table row, a shell pipeline in a label) is
        content, not chrome — stripping it would corrupt the comparison."""
        assert (
            strip_leading_gutter("│ run `ls | wc -l` │ then stop")
            == "run `ls | wc -l` │ then stop"
        )


# ── (a) Part A: the record/pane consistency check accepts the gutter pane ───


class TestRecordConsistentWithGutterPane:
    def test_real_record_shape_is_consistent_with_the_real_gutter_pane(self) -> None:
        """THE core RED test. On main this returns ``(False, 'title_mismatch')``
        because neither ``"│ Dynamics…"`` nor ``"Dynamics…"`` prefixes the
        other."""
        form = parse_ask_user_question(_pane_text())
        assert form is not None
        consistent, reason = auq_source._record_consistent_with_pane(
            _record(_FULL_QUESTION), form
        )
        assert (consistent, reason) == (True, "ok")

    def test_a_genuinely_different_question_still_mismatches(self) -> None:
        """The canonicalizer must not turn the title check into a rubber stamp:
        a DIFFERENT question with the same labels still rejects."""
        form = parse_ask_user_question(_pane_text())
        assert form is not None
        consistent, reason = auq_source._record_consistent_with_pane(
            _record("An entirely unrelated question about deployment cadence"),
            form,
        )
        assert (consistent, reason) == (False, "title_mismatch")

    def test_a_gutter_prefixed_record_question_also_matches(self) -> None:
        """SYMMETRY: canonicalization is applied to BOTH sides, so a record whose
        own text somehow carried a gutter matches too."""
        form = parse_ask_user_question(_pane_text())
        assert form is not None
        consistent, reason = auq_source._record_consistent_with_pane(
            _record(f"│ {_FULL_QUESTION}"), form
        )
        assert (consistent, reason) == (True, "ok")


# ── (d) Part A: tab inference pins the active tab by exact title ────────────


class TestInferCurrentTabOnGutterPane:
    def test_exact_title_leg_pins_the_tab_through_the_gutter(self) -> None:
        """On main the gutter breaks the exact-title leg, which then silently
        degrades to option-label overlap; with labels that do NOT overlap the
        pane, main returns ``(0, False)`` (no tab pinned) while the fix pins the
        right tab via the title.

        The expected index is 1, not 0, so a defaulted ``idx=0`` fallthrough can
        never masquerade as a pass.
        """
        form = parse_ask_user_question(_pane_text())
        assert form is not None
        pane_title = strip_leading_gutter(form.current_question_title or "")
        assert pane_title  # premise: the pane really does carry a title

        questions = (
            AskQuestion(
                title="A different question entirely",
                header="Todo scope",
                options=(_opt(1, "Unrelated label one"),),
            ),
            AskQuestion(
                # Exactly what the pane shows (a SHORT 2.1.237 question fits on
                # one gutter line, so pane title == JSONL title after
                # canonicalization).
                title=pane_title,
                header="Dynamics",
                options=(_opt(1, "Unrelated label two"),),
            ),
        )

        idx, inferred = terminal_parser._infer_current_tab_idx(questions, form)
        assert (idx, inferred) == (1, True)

    def test_label_overlap_leg_is_unchanged(self) -> None:
        """GREEN-must-stay: the secondary leg still works when titles disagree."""
        form = parse_ask_user_question(_pane_text())
        assert form is not None
        questions = (
            AskQuestion(
                title="Nothing like the pane",
                header="Todo scope",
                options=(_opt(1, "Unrelated label"),),
            ),
            AskQuestion(
                title="Also nothing like the pane",
                header="Dynamics",
                options=tuple(
                    _opt(i, label) for i, label in enumerate(_PANE_LABELS, start=1)
                ),
            ),
        )
        idx, inferred = terminal_parser._infer_current_tab_idx(questions, form)
        assert (idx, inferred) == (1, True)
