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
    has_leading_gutter,
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


def _gutter_pane(question_lines: list[str]) -> str:
    """A synthetic 2.1.237-shaped multi-tab picker whose question sits behind the
    box gutter, carrying the fixture's own option labels."""
    return "\n".join(
        [
            "←  ☐ Dynamics  ☐ Todo scope  ✔ Submit  →",
            "",
            *(f"│ {line}" for line in question_lines),
            "",
            *(
                f"{'❯' if i == 1 else ' '} {i}. {label}"
                for i, label in enumerate(_PANE_LABELS, start=1)
            ),
            f"  {len(_PANE_LABELS) + 1}. Type something.",
            "─" * 40,
            f"  {len(_PANE_LABELS) + 2}. Chat about this",
            "",
            "Enter to select · Tab/Arrow keys to navigate · Esc to cancel",
        ]
    )


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

    def test_a_long_boxed_question_is_never_silently_truncated(self) -> None:
        """P3: NO artificial line cap.

        An 8-line cap silently dropped the tail of a longer boxed question that
        the 📋 card then presented as "full details" — a quiet correctness loss
        in the one surface whose entire job is completeness. 12 gutter lines
        here; all 12 must survive, in order.
        """
        sentences = [f"sentence {i} of the question." for i in range(1, 13)]
        pane = "\n".join(
            [
                "←  ☐ Alpha  ☐ Beta  ✔ Submit  →",
                "",
                *(f"│ {s}" for s in sentences),
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
        assert form.pane_question_display_text == " ".join(sentences)
        # The identity field still keeps exactly ONE physical line.
        assert form.current_question_title == "│ sentence 1 of the question."

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
            ("│  Which direction?", "Which direction?"),  # >1 space
            ("│ │  Which direction?", "Which direction?"),  # repeated run
            ("   │ Which direction?", "Which direction?"),  # indented box
            ("Which direction?", "Which direction?"),  # no gutter → untouched
            ("  padded  ", "padded"),  # always whitespace-normalized
            ("", ""),
        ],
    )
    def test_leading_runs_only(self, raw: str, expected: str) -> None:
        assert strip_leading_gutter(raw) == expected

    def test_interior_glyphs_are_never_touched(self) -> None:
        """A glyph INSIDE the text (a table row, a shell pipeline in a label) is
        content, not chrome — stripping it would corrupt the comparison."""
        assert (
            strip_leading_gutter("│ run `ls | wc -l` │ then stop")
            == "run `ls | wc -l` │ then stop"
        )

    def test_a_chrome_only_title_keeps_its_glyph(self) -> None:
        """Stripping to nothing is refused: a title that is ALL chrome carries no
        question text, and collapsing it to "" would silently flip a caller's
        "is there a title?" guard into the skip branch."""
        assert strip_leading_gutter("│ ") == "│"
        assert strip_leading_gutter("│ │ ") == "│ │"


# ── (P1-A) INJECTIVITY: the canonicalizer must not merge distinct content ────


class TestCanonicalizerInjectivity:
    """The accepted delimiter set is the BOX-DRAWING glyphs ``│``/``┃`` ONLY,
    each followed by at least one space.

    ASCII ``|`` is excluded on purpose. Accepting it — worse, with no required
    whitespace — made the canonicalizer NON-INJECTIVE against legitimate
    question CONTENT: a question that genuinely starts with a pipe would
    canonicalize onto the same value as the same text WITHOUT the pipe, so a
    stale side file whose labels happened to line up could be trusted for a
    DIFFERENT question (a wrong-question card).
    """

    @pytest.mark.parametrize(
        "content",
        [
            "| jq . to pretty-print it?",  # a shell pipeline
            "| col a | col b |",  # a markdown table row
            "|| fallback to the default?",  # a shell OR
            "|",
        ],
    )
    def test_ascii_pipe_content_is_never_stripped(self, content: str) -> None:
        assert strip_leading_gutter(content) == content
        assert has_leading_gutter(content) is False

    @pytest.mark.parametrize(
        ("with_pipe", "without_pipe"),
        [
            ("| jq . to pretty-print it?", "jq . to pretty-print it?"),
            ("| col a | col b |", "col a | col b |"),
        ],
    )
    def test_pipe_and_pipeless_content_do_not_collide(
        self, with_pipe: str, without_pipe: str
    ) -> None:
        """THE collision test: two DIFFERENT questions must not share a canonical
        value. Under the pre-fold alphabet both sides collapsed to the same
        string and compared EQUAL."""
        assert strip_leading_gutter(with_pipe) != strip_leading_gutter(without_pipe)

    def test_box_glyph_without_a_space_is_not_chrome(self) -> None:
        """``│Foo`` is not a shape the CC TUI emits; requiring the separator
        keeps the accepted alphabet as narrow as the OBSERVED chrome."""
        assert strip_leading_gutter("│Foo") == "│Foo"
        assert has_leading_gutter("│Foo") is False

    def test_markdown_table_question_stays_fail_closed_against_the_pane(self) -> None:
        """End-to-end injectivity: a record question that begins with a markdown
        table row does NOT become consistent with the real gutter pane."""
        form = parse_ask_user_question(_pane_text())
        assert form is not None
        consistent, reason = auq_source._record_consistent_with_pane(
            _record("| col a | col b |\nWhich direction?"), form
        )
        assert (consistent, reason) == (False, "title_mismatch")

    def test_the_real_fixture_still_resolves_consistent(self) -> None:
        """…and the narrowing did not cost the fix: the real 2.1.237 pane still
        reconciles with its record."""
        form = parse_ask_user_question(_pane_text())
        assert form is not None
        assert auq_source._record_consistent_with_pane(
            _record(_FULL_QUESTION), form
        ) == (True, "ok")

    def test_a_stale_gutter_authority_cannot_borrow_the_panes_chrome_strip(
        self,
    ) -> None:
        """THE r2 P1 collision, stated as the attack it enables.

        Live question is ``Foo``; the pane renders it as ``│ Foo``. A STALE
        record whose authored question is literally ``│ Foo`` is a DIFFERENT
        question. Under the two-sided strip both collapsed to ``Foo`` and
        compared EQUAL — with matching labels that is a trusted ``side_file_ok``
        for the wrong question. One-sided + ambiguous-authority-fail-closed
        keeps them distinct.
        """
        live_question = "Which rollout cadence do you prefer?"
        pane = _gutter_pane([live_question])
        form = parse_ask_user_question(pane)
        assert form is not None
        # Premise: the pane really does render the live question behind a gutter.
        assert form.current_question_title == f"│ {live_question}"

        # The genuine record (authored, gutterless) matches.
        assert auq_source._record_consistent_with_pane(
            _record(live_question), form
        ) == (True, "ok")

        # The stale look-alike, whose AUTHORED text carries the glyph, does not.
        consistent, reason = auq_source._record_consistent_with_pane(
            _record(f"│ {live_question}"), form
        )
        assert (consistent, reason) == (False, "title_mismatch")


# ── (r2 P1) ASYMMETRY: only the PANE is de-chromed, never the authority ──────


class TestOnlyThePaneIsDeChromed:
    def test_a_gutter_record_never_matches_a_gutterless_pane(self) -> None:
        """The AUTHORITY is never de-chromed, so a record question that begins
        with a box gutter cannot match a pane that has none. A gutterless pane
        de-chromes to itself, so the comparison is byte-identical to its
        pre-2.1.237 behaviour.
        """
        pane = "\n".join(
            [
                "←  ☐ Alpha  ☐ Beta  ✔ Submit  →",
                "",
                "Which approach should we take?",
                "",
                "❯ 1. Option A spike",
                "  2. Option B direct",
                "  3. Entra SSO variant first",
                "  4. Park it",
                "  5. Type something.",
                "─" * 40,
                "  6. Chat about this",
                "",
                "Enter to select · Tab/Arrow keys to navigate · Esc to cancel",
            ]
        )
        form = parse_ask_user_question(pane)
        assert form is not None
        assert has_leading_gutter(form.current_question_title or "") is False

        # Same text, but the RECORD carries the gutter. The authority is never
        # de-chromed ⇒ mismatch (the pre-2.1.237 answer).
        consistent, reason = auq_source._record_consistent_with_pane(
            _record("│ Which approach should we take?"), form
        )
        assert (consistent, reason) == (False, "title_mismatch")

        # The gutterless record still matches, unchanged.
        assert auq_source._record_consistent_with_pane(
            _record("Which approach should we take?"), form
        ) == (True, "ok")

    def test_tab_inference_also_refuses_a_gutter_prefixed_authority(self) -> None:
        """The SAME asymmetry at the other call site. A JSONL question whose own
        title carries the glyph must not be pinned by a de-chromed pane title —
        otherwise a stale tab could be selected for the live one."""
        live_title = "Which rollout cadence do you prefer?"
        form = parse_ask_user_question(_gutter_pane([live_title]))
        assert form is not None
        assert form.current_question_title == f"│ {live_title}"

        # Authority WITHOUT the glyph == the live question ⇒ pinned.
        genuine = (
            AskQuestion(
                title="Something else",
                header="Todo scope",
                options=(_opt(1, "Unrelated"),),
            ),
            AskQuestion(
                title=live_title, header="Dynamics", options=(_opt(1, "Unrelated2"),)
            ),
        )
        assert terminal_parser._infer_current_tab_idx(genuine, form) == (1, True)

        # Authority WITH the glyph is a DIFFERENT title ⇒ no title pin. The
        # option labels are non-overlapping too, so the secondary leg cannot
        # rescue it and inference fails closed.
        stale = (
            AskQuestion(
                title="Something else",
                header="Todo scope",
                options=(_opt(1, "Unrelated"),),
            ),
            AskQuestion(
                title=f"│ {live_title}",
                header="Dynamics",
                options=(_opt(1, "Unrelated2"),),
            ),
        )
        assert terminal_parser._infer_current_tab_idx(stale, form) == (0, False)


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

    def test_a_gutter_prefixed_record_question_FAILS_CLOSED(self) -> None:
        """ASYMMETRY (codex r2 P1) — the inverse of what an earlier round pinned.

        The gutter is PANE-RENDERING CHROME; authored question text never carries
        it. Stripping BOTH sides was non-injective one level up: a stale record
        ``"│ Foo"`` canonicalized to ``"Foo"`` and matched a live pane
        ``"│ Foo"`` whose real question is ``"Foo"``, so with coincidentally
        matching labels the stale record was served as a TRUSTED source for a
        DIFFERENT live question.

        An authority that begins with what looks like chrome is AMBIGUOUS, so
        the comparison runs RAW and bails.
        """
        form = parse_ask_user_question(_pane_text())
        assert form is not None
        consistent, reason = auq_source._record_consistent_with_pane(
            _record(f"│ {_FULL_QUESTION}"), form
        )
        assert (consistent, reason) == (False, "title_mismatch")

    def test_the_gutterless_record_still_matches_the_gutter_pane(self) -> None:
        """…and the asymmetry did not cost the fix: the REAL 2.1.237 shape —
        authored text without a gutter, pane with one — still reconciles."""
        form = parse_ask_user_question(_pane_text())
        assert form is not None
        assert auq_source._record_consistent_with_pane(
            _record(_FULL_QUESTION), form
        ) == (True, "ok")


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
