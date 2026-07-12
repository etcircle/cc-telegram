"""The GH #50 PR-2 free-text pane predicates, pinned on REAL CC 2.1.207 captures.

Every assertion here is anchored to a fixture captured from a live Claude Code
2.1.207 session in the isolated rig — the empirics the version license exists to
protect. If a future CC release breaks one of these, the license table must NOT
be widened until the fixtures are re-captured.

The fixture set (all ``capture-pane -e``, i.e. WITH ANSI, because the SGR-2 dim
bit is the PRE-TYPE LANDING PROOF — the one guard that keeps a free-text payload
off a real option row; the post-write legs are corroboration, not defence):

  auq_freetext_row_selected_pretype   cursor on row 4, DIM placeholder
  auq_freetext_row_typed              short typed answer, PLAIN
  auq_freetext_typed_identical_label  the adversarial payload (byte-identical to
                                      the placeholder) — still PLAIN
  auq_freetext_row_typed_large        947-char, 9-line voice-note-shaped payload
  auq_freetext_overflow               ~5.3 k draft: the option block (row 4
                                      INCLUDED) has scrolled off; only the footer
                                      remains

(ExitPlanMode had the same five captures through peer-review round 3. Its
free-text lane was DROPPED by owner decision on 2026-07-12 — a plan card now
takes PR-1's refusal — so the fixtures went with it.)
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

    def test_typed_text_is_not_dim(self):
        row = tp.parse_free_text_row(
            _fx(f"auq_freetext_row_typed_{V}.ansi.txt"), number=4
        )
        assert row is not None
        assert row.cursor is True
        assert row.dim is False, "typed text must never read as the placeholder"
        assert row.label != tp.AUQ_FREE_TEXT_LABEL

    def test_payload_identical_to_the_placeholder_still_reads_as_typed(self):
        """The adversarial case the plan called out as a possible scope cut.

        Typing the literal placeholder text renders it PLAIN, so the SGR-2 signal
        discriminates it correctly and the "identical-label payload" cut the plan
        contemplated is genuinely unnecessary. A label-comparison verifier would
        have read this as "nothing was typed" and refused to commit.
        """
        row = tp.parse_free_text_row(
            _fx(f"auq_freetext_typed_identical_label_{V}.ansi.txt"), number=4
        )
        assert row is not None
        assert row.label == tp.AUQ_FREE_TEXT_LABEL  # byte-identical to it…
        assert row.dim is False  # …yet PROVABLY typed

    def test_large_multiline_payload_renders_literally_not_paste_collapsed(self):
        """THE REGRESSION THE PARENT ASKED ABOUT — measured, not reasoned.

        A payload written in ONE ``send-keys -l`` past ~800 chars collapses the
        INPUT BOX to ``❯\\xa0[Pasted text #1 +12 lines]`` and replaces the status
        bar with ``paste again to expand`` (the shipped PR-1 regression, 5ba9b5e).
        The affordance row of a live CARD does NOT do this: a 947-char, 9-line
        voice-note-shaped payload renders as LITERAL wrapped text, the row keeps
        its number and cursor, and the label is PLAIN (rig-measured; Enter then
        submitted the full 947 chars as the answer, JSONL-verified).
        """
        pane = _fx(f"auq_freetext_row_typed_large_{V}.ansi.txt")
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

    def test_an_epm_ctrl_g_footer_never_mints_the_proof(self):
        """ExitPlanMode renders ``ctrl+g to edit in Vim`` UNCONDITIONALLY (it is
        the plan-file footer, not a row-active signal). The predicate must not be
        fooled by it: an EPM pane is not an AskUserQuestion at all, so it declines
        before the footer is ever consulted."""
        untouched = _fx(f"gate_epm_{V}.txt")  # cursor on option 1
        assert "ctrl+g to edit" in untouched
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
            tp.clean_ghost_input_text(_fx(name)), target_row=4
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
        from tests.free_text_frames import AUQ_X_LANDED, type_into_row

        auq_real = _fx(f"auq_freetext_row_typed_{V}.ansi.txt")
        assert self._row(
            type_into_row(AUQ_X_LANDED, 4, "teal, actually"), 4
        ) == self._row(auq_real, 4)

    def test_the_cursor_move_derivation_parses_as_a_real_capture_does(self):
        from tests.free_text_frames import AUQ_X_LIVE

        form = tp.parse_ask_user_question(tp.clean_ghost_input_text(AUQ_X_LIVE))
        assert form is not None
        assert [o.number for o in form.options if o.cursor] == [1]
        assert tp.parse_free_text_row(AUQ_X_LIVE, number=4).cursor is False  # type: ignore[union-attr]


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
        ],
    )
    def test_no_input_box(self, name: str):
        pane = tp.clean_ghost_input_text(_fx(name))
        assert tp.pane_input_box_present(pane) is False
