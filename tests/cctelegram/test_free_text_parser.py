"""The GH #50 PR-2 free-text pane predicates, pinned on REAL CC 2.1.207 captures.

Every assertion here is anchored to a fixture captured from a live Claude Code
2.1.207 session in the isolated rig — the empirics the version license exists to
protect. If a future CC release breaks one of these, the license table must NOT
be widened until the fixtures are re-captured.

The fixture set (all ``capture-pane -e``, i.e. WITH ANSI, because the SGR-2 dim
bit IS the typed-state proof):

  auq_freetext_row_selected_pretype   cursor on row 4, DIM placeholder
  auq_freetext_row_typed              short typed answer, PLAIN
  auq_freetext_typed_identical_label  the adversarial payload (byte-identical to
                                      the placeholder) — still PLAIN
  auq_freetext_row_typed_large        947-char, 9-line voice-note-shaped payload
  auq_freetext_overflow               ~5.3 k draft: the option block (row 4
                                      INCLUDED) has scrolled off; only the footer
                                      remains
  epm_*                               the same five states for ExitPlanMode, whose
                                      overflow scrolls the FOOTER off instead
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cctelegram import terminal_parser as tp

FIXTURES = Path(__file__).parent / "fixtures"


def _fx(name: str) -> str:
    return (FIXTURES / name).read_text()


V = "v2.1.207"


class TestParseFreeTextRow:
    """The SGR-2 typed-state discriminator (plan §2.1 [§5-E11])."""

    def test_auq_selected_placeholder_is_dim(self):
        row = tp.parse_free_text_row(
            _fx(f"auq_freetext_row_selected_pretype_{V}.ansi.txt"), number=4
        )
        assert row is not None
        assert row.number == 4
        assert row.cursor is True
        assert row.label == tp.AUQ_FREE_TEXT_LABEL
        # THE PROOF: an untyped, selected affordance row is DIM.
        assert row.dim is True

    def test_epm_selected_placeholder_is_dim(self):
        row = tp.parse_free_text_row(
            _fx(f"epm_freetext_row_selected_pretype_{V}.ansi.txt"), number=4
        )
        assert row is not None
        assert row.cursor is True
        assert row.label == tp.EPM_FREE_TEXT_LABEL
        assert row.dim is True

    @pytest.mark.parametrize("surface", ["auq", "epm"])
    def test_typed_text_is_not_dim(self, surface: str):
        row = tp.parse_free_text_row(
            _fx(f"{surface}_freetext_row_typed_{V}.ansi.txt"), number=4
        )
        assert row is not None
        assert row.cursor is True
        assert row.dim is False, "typed text must never read as the placeholder"
        assert row.label not in (tp.AUQ_FREE_TEXT_LABEL, tp.EPM_FREE_TEXT_LABEL)

    @pytest.mark.parametrize(
        "surface,label",
        [("auq", tp.AUQ_FREE_TEXT_LABEL), ("epm", tp.EPM_FREE_TEXT_LABEL)],
    )
    def test_payload_identical_to_the_placeholder_still_reads_as_typed(
        self, surface: str, label: str
    ):
        """The adversarial case the plan called out as a possible scope cut.

        Typing the literal placeholder text renders it PLAIN, so the SGR-2 signal
        discriminates it correctly and the "identical-label payload" cut the plan
        contemplated is genuinely unnecessary. A label-comparison verifier would
        have read this as "nothing was typed" and refused to commit.
        """
        row = tp.parse_free_text_row(
            _fx(f"{surface}_freetext_typed_identical_label_{V}.ansi.txt"), number=4
        )
        assert row is not None
        assert row.label == label  # byte-identical to the placeholder…
        assert row.dim is False  # …yet PROVABLY typed

    @pytest.mark.parametrize("surface", ["auq", "epm"])
    def test_large_multiline_payload_renders_literally_not_paste_collapsed(
        self, surface: str
    ):
        """THE REGRESSION THE PARENT ASKED ABOUT — measured, not reasoned.

        A payload written in ONE ``send-keys -l`` past ~800 chars collapses the
        INPUT BOX to ``❯\\xa0[Pasted text #1 +12 lines]`` and replaces the status
        bar with ``paste again to expand`` (the shipped PR-1 regression, 5ba9b5e).
        The affordance row of a live CARD does NOT do this: a 947-char, 9-line
        voice-note-shaped payload renders as LITERAL wrapped text, the row keeps
        its number and cursor, and the label is PLAIN (rig-measured; Enter then
        submitted the full 947 chars as the answer, JSONL-verified).
        """
        pane = _fx(f"{surface}_freetext_row_typed_large_{V}.ansi.txt")
        assert "Pasted text #" not in pane
        assert "paste again to expand" not in pane
        row = tp.parse_free_text_row(pane, number=4)
        assert row is not None
        assert row.cursor is True
        assert row.dim is False
        # The row shows the FIRST visual line of the wrapped draft.
        assert row.label.startswith('> Re: "the picker card')

    def test_row_absent_returns_none(self):
        assert tp.parse_free_text_row("nothing here", number=4) is None
        assert tp.parse_free_text_row(None, number=4) is None

    def test_plain_capture_cannot_prove_dim(self):
        """A capture WITHOUT ANSI has thrown the proof away — it must read as
        NOT dim, so the pre-type landing gate (which REQUIRES dim) fails closed
        rather than typing on an unverified row."""
        ansi = _fx(f"auq_freetext_row_selected_pretype_{V}.ansi.txt")
        plain = tp.clean_ghost_input_text(ansi)
        row = tp.parse_free_text_row(plain, number=4)
        assert row is not None and row.dim is False


class TestAuqFreeTextRowActive:
    """The AUQ overflow proof: ``ctrl+g to edit`` ⟺ the free-text row is active."""

    def test_absent_when_the_cursor_is_on_a_real_option(self):
        # The baseline picker fixture: cursor on option 1, footer has NO ctrl+g.
        assert tp.auq_free_text_row_active(_fx(f"auq_single_picker_{V}.txt")) is False

    def test_present_when_the_free_text_row_is_selected(self):
        pane = tp.clean_ghost_input_text(
            _fx(f"auq_freetext_row_selected_pretype_{V}.ansi.txt")
        )
        assert tp.auq_free_text_row_active(pane) is True

    def test_survives_the_overflow_that_scrolls_the_row_off(self):
        """The whole point: a ~5.3 k draft scrolls the option block — the ``❯ 4.``
        cursor row INCLUDED — off the top of a 50-row pane, and a TUI has no
        scrollback (rig: ``capture-pane -S -300`` returned 51 lines). The row is
        genuinely unobservable, but the footer still proves Enter will commit the
        free-text row."""
        pane = _fx(f"auq_freetext_overflow_{V}.txt")
        assert tp.parse_free_text_row(pane, number=4) is None  # row is GONE
        assert tp.auq_free_text_row_active(pane) is True  # …but still provable

    def test_epm_ctrl_g_is_NOT_a_row_active_proof(self):
        """[r4 P1-4] EPM renders ``ctrl+g to edit in Vim`` unconditionally (it is
        the plan-file footer), so it proves nothing about the cursor. That is why
        the EPM lane REQUIRES the row itself and refuses an overflowed pane —
        EPM's option 1 is "Yes, and bypass permissions"."""
        untouched = _fx(f"gate_epm_{V}.txt")  # cursor on option 1
        assert "ctrl+g to edit" in untouched
        # …and it is not an AUQ picker at all, so the AUQ proof declines.
        assert tp.auq_free_text_row_active(untouched) is False

    def test_a_STALE_footer_above_the_live_one_cannot_mint_the_proof(self):
        """peer-review P1 — the scoping that matters.

        The first cut was an OR over the WHOLE pane: "does SOME line carry
        ``Enter to select`` and, on that line, ``ctrl+g``?". So a footer left in
        SCROLLBACK by an EARLIER picker (or a transcript the user pasted into the
        conversation) satisfied it — while the LIVE picker's own footer, sitting
        below it with the cursor parked on option 1, said the opposite. A positive
        proof that arbitrary pane text can mint is not a proof.

        The live footer is the BOTTOM-MOST one (a TUI renders the live picker at
        the bottom and its scrollback is frozen above), and that is now the only
        one consulted.
        """
        live = _fx(f"auq_single_picker_{V}.txt")  # cursor on option 1 — NO ctrl+g
        assert "ctrl+g to edit" not in live
        stale_above = (
            "⏺ Earlier I asked you this:\n"
            "\n"
            "  1. Red\n"
            "❯ 4. Type something.\n"
            "Enter to select · ↑/↓ to navigate · ctrl+g to edit in Vim · Esc to cancel\n"
            "\n"
        ) + live

        assert "ctrl+g to edit" in stale_above  # the marker IS on the pane…
        assert tp.auq_free_text_row_active(stale_above) is False  # …but not LIVE

    def test_a_non_auq_pane_declines(self):
        """The pane must extract as a LIVE AskUserQuestion before its footer is
        consulted at all — the predicate never speaks for a surface it is not
        looking at."""
        for name in (
            f"gate_epm_{V}.txt",
            f"gate_permission_{V}.txt",
            "folder_trust_arrival_plain_v2.1.207.txt",
            "inputbox_idle_v2.1.207.txt",
        ):
            assert tp.auq_free_text_row_active(_fx(name)) is False, name


class TestTheStableSurfaceIdentity:
    """peer-review P1 — WHICH CARD, across the mutations the executor itself makes.

    THE DRIFT TRAP: the transaction moves the cursor onto the affordance row and
    then REPLACES that row's label with the user's prose. ``_parse_numbered_options``
    DROPS a row whose label is an affordance ("Type something."), so the moment our
    text lands there it stops being an affordance and parses as a FOURTH REAL
    OPTION — a naive form fingerprint moves under us and every commit would refuse.
    The identity is CURSOR-BLIND (inherited) and TARGET-ROW-BLIND (built), so what
    it hashes is exactly the part of the card the transaction never touches.
    """

    def _auq(self, name: str) -> str | None:
        return tp.free_text_surface_identity(
            tp.clean_ghost_input_text(_fx(name)),
            surface="AskUserQuestion",
            target_row=4,
        )

    def _epm(self, name: str) -> str | None:
        return tp.free_text_surface_identity(
            tp.clean_ghost_input_text(_fx(name)),
            surface="ExitPlanMode",
            target_row=4,
        )

    def test_stable_across_the_cursor_move_and_the_typed_text(self):
        pretype = self._auq(f"auq_freetext_row_selected_pretype_{V}.ansi.txt")
        typed_big = self._auq(f"auq_freetext_row_typed_large_{V}.ansi.txt")
        assert pretype is not None
        assert pretype == typed_big, "the identity must not move when WE mutate it"

    def test_a_different_question_has_a_different_identity(self):
        card_x = self._auq(f"auq_freetext_row_selected_pretype_{V}.ansi.txt")
        card_y = self._auq(f"auq_single_picker_{V}.txt")
        assert card_x is not None and card_y is not None
        assert card_x != card_y

    def test_the_payload_byte_identical_to_the_placeholder_is_still_stable(self):
        """The adversarial one: the user types the literal ``Type something.``, so
        the row parses as an affordance AGAIN and is dropped again. Identity holds
        either way — it never depends on the target row."""
        base = self._auq(f"auq_freetext_row_selected_pretype_{V}.ansi.txt")
        assert self._auq(f"auq_freetext_typed_identical_label_{V}.ansi.txt") is not None
        assert base is not None

    def test_an_unrecoverable_option_block_is_None_not_a_shorter_prefix(self):
        """It must FAIL CLOSED, never silently degrade to a weaker identity."""
        assert self._auq(f"auq_freetext_overflow_{V}.txt") is None

    def test_every_epm_plan_shares_one_pane_identity(self):
        """The reason the EPM lane's out-of-band anchor (the ``~/.claude/plans/
        <slug>.md`` footer path) is MANDATORY: every ExitPlanMode prompt renders
        the SAME three real options, so the pane alone cannot tell plan P from
        plan Q. If this ever stops being true, the EPM anchor could be relaxed —
        until then, relaxing it would be a wrong-card commit onto a plan-approval
        surface."""
        p = self._epm(f"epm_freetext_row_selected_pretype_{V}.ansi.txt")
        q = self._epm(f"epm_freetext_row_typed_{V}.ansi.txt")
        s = self._epm(f"gate_epm_{V}.txt")
        assert p is not None and p == q == s
        # …and the anchor DOES discriminate them.
        paths = {
            tp.extract_epm_plan_file_path(tp.clean_ghost_input_text(_fx(n)))
            for n in (
                f"epm_freetext_row_selected_pretype_{V}.ansi.txt",
                f"epm_freetext_row_typed_{V}.ansi.txt",
                f"gate_epm_{V}.txt",
            )
        }
        assert len(paths) == 3 and None not in paths

    def test_a_numbered_plan_BODY_no_longer_hijacks_the_option_walk(self):
        """Most plans render a numbered list of steps. That list used to capture
        ``_parse_numbered_options``' top-down walk, so the real option block was
        never reached and the ENTIRE EPM free-text lane silently declined on the
        common shape. The block is now delimited by the prompt's own ``UIPattern``
        anchors. (Fail-closed, so it was never dangerous — just dead.)"""
        pane = tp.clean_ghost_input_text(_fx(f"epm_freetext_row_typed_{V}.ansi.txt"))
        assert "1. Create goodbye.txt" in pane  # the numbered plan body
        assert self._epm(f"epm_freetext_row_typed_{V}.ansi.txt") is not None


class TestTheDerivedFramesAreFaithful:
    """``tests/free_text_frames`` derives two frames the rig corpus lacks (a
    "cursor on row 1" pre-nav frame and a SHORT typed frame). The derivation is
    not trusted — it is PINNED byte-identical against the real captures."""

    def _row(self, ansi: str, number: int) -> str:
        import re

        found = ""
        for line in ansi.split("\n"):
            visible = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", line)
            if re.match(rf"\s*[❯›▶*]?\s*{number}\.\s", visible):
                found = line
        return found

    def test_the_typed_derivation_is_byte_identical_to_the_real_capture(self):
        from tests.free_text_frames import AUQ_X_LANDED, EPM_P_LANDED, type_into_row

        auq_real = _fx(f"auq_freetext_row_typed_{V}.ansi.txt")
        assert self._row(
            type_into_row(AUQ_X_LANDED, 4, "teal, actually"), 4
        ) == self._row(auq_real, 4)

        epm_real = _fx(f"epm_freetext_row_typed_{V}.ansi.txt")
        epm_derived = type_into_row(
            EPM_P_LANDED, 4, "please name it farewell.txt instead"
        )
        assert self._row(epm_derived, 4) == self._row(epm_real, 4)

    def test_the_cursor_move_derivation_parses_as_a_real_capture_does(self):
        from tests.free_text_frames import AUQ_X_LIVE, EPM_P_LIVE

        form = tp.parse_ask_user_question(tp.clean_ghost_input_text(AUQ_X_LIVE))
        assert form is not None
        assert [o.number for o in form.options if o.cursor] == [1]
        assert tp.parse_free_text_row(AUQ_X_LIVE, number=4).cursor is False  # type: ignore[union-attr]

        epm = tp.parse_exit_plan_form(tp.clean_ghost_input_text(EPM_P_LIVE))
        assert epm is not None
        assert [o.number for o in epm.options if o.cursor] == [1]


class TestParseExitPlanForm:
    def test_parses_the_live_epm_option_block(self):
        form = tp.parse_exit_plan_form(_fx(f"gate_epm_{V}.txt"))
        assert form is not None
        assert [o.number for o in form.options] == [1, 2, 3, 4]
        assert form.options[-1].label == tp.EPM_FREE_TEXT_LABEL
        assert form.options[0].cursor is True  # the default landing row
        assert form.is_free_text is True

    def test_cursor_tracks_the_selected_row(self):
        pane = tp.clean_ghost_input_text(
            _fx(f"epm_freetext_row_selected_pretype_{V}.ansi.txt")
        )
        form = tp.parse_exit_plan_form(pane)
        assert form is not None
        assert [o.number for o in form.options if o.cursor] == [4]

    def test_declines_a_pane_without_the_feedback_affordance(self):
        # An AUQ picker is not an EPM prompt — the last row must be the EPM
        # affordance label or the parse returns None (never a guessed row index).
        assert tp.parse_exit_plan_form(_fx(f"auq_single_picker_{V}.txt")) is None
        assert tp.parse_exit_plan_form("") is None

    def test_epm_overflow_keeps_the_row_visible(self):
        """EPM overflows the OTHER way (rig-measured): its prompt grows DOWNWARD,
        so a long draft pushes the FOOTER off the bottom while the ``❯ 4.`` row
        stays on the pane. So the EPM lane's row-based verifier still works
        exactly where the AUQ lane's footer-based one would not."""
        ansi = _fx(f"epm_freetext_overflow_{V}.ansi.txt")
        pane = tp.clean_ghost_input_text(ansi)
        assert "ctrl+g to edit" not in pane  # footer is GONE
        row = tp.parse_free_text_row(ansi, number=4)
        assert row is not None and row.cursor is True and row.dim is False


class TestTheInputBoxIsNeverPresentOnThese:
    """Leg B of the typed-state verifier, on every real capture.

    A live card REPLACES the input box, so ``pane_input_box_present`` must be
    False on all of them — including the two overflow shapes. If it were ever
    True, the verifier could not tell "our draft is in the card" from "the card
    resolved and our text is in the input box".
    """

    @pytest.mark.parametrize(
        "name",
        [
            f"auq_freetext_row_selected_pretype_{V}.ansi.txt",
            f"auq_freetext_row_typed_large_{V}.ansi.txt",
            f"auq_freetext_overflow_{V}.txt",
            f"epm_freetext_row_selected_pretype_{V}.ansi.txt",
            f"epm_freetext_row_typed_large_{V}.ansi.txt",
            f"epm_freetext_overflow_{V}.ansi.txt",
        ],
    )
    def test_no_input_box(self, name: str):
        pane = tp.clean_ghost_input_text(_fx(name))
        assert tp.pane_input_box_present(pane) is False
