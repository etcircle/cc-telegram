"""The GH #50 PR-2 free-text executor: nav → verify → type → verify → Enter.

Driven against a FAKE tmux whose captures are the REAL 2.1.207 rig fixtures, so
the state machine is exercised on the exact bytes Claude Code renders.

The THREE invariants every test here defends:

  1. **The lane is purely ADDITIVE.** Every bail BEFORE the first keystroke
     returns ``None`` and the caller falls through to PR-1's gate — so the lane
     can only turn a REFUSED message into a delivered ANSWER, never the reverse.
  2. **The commit boundary is strict.** Nothing typed ⇒ ``None``. Typed but not
     committed ⇒ ``DRAFT_WRITTEN`` + the stranded-draft brake. Enter sent but
     unproven ⇒ ``COMMIT_UNKNOWN``, reported honestly, NEVER auto-retried.
  3. **The Enter never lands on the WRONG CARD.** The pane can be in exactly the
     right STATE and still be the wrong SURFACE — another controller resolves
     card A and renders card B while we navigate or type, and B holds our text in
     ITS free-text row. Identity is captured pre-key and re-checked after the nav
     and again in the final pre-Enter capture. See ``tests/free_text_frames.py``
     for the real card generations these tests cross.

SCOPE: AskUserQuestion ONLY. ExitPlanMode had its own free-text lane through
peer-review round 3; the owner dropped it on 2026-07-12, so an EPM card falls
through to PR-1's refusal. It survives here only as the surface a card can turn
over INTO — the most dangerous swap there is (its option 1 is "Yes, and bypass
permissions"), and identity must refuse it.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from cctelegram import delivery, terminal_parser, tmux_manager as tmux_mod
from cctelegram.delivery import DeliveryOutcome, UserTurnStamp
from cctelegram.handlers import auq_source, free_text
from tests.free_text_frames import (
    AUQ_RESOLVED,
    AUQ_X_ANSWER,
    AUQ_X_LANDED,
    AUQ_X_LIVE,
    AUQ_X_OVERFLOW,
    AUQ_X_TYPED,
    AUQ_X_TYPED_BIG,
    AUQ_Y_LIVE,
    AUQ_Y_TYPED,
    BIG_ANSWER,
    EPM_LIVE,
    OVERFLOW_ANSWER,
    plain,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"
V = "v2.1.207"


def _fx(name: str) -> str:
    return (FIXTURES / name).read_text()


WINDOW = "@0"
STAMP = UserTurnStamp(user_id=1, thread_id=42, window_id=WINDOW)
PAYLOAD = "I would prefer a deep teal, actually"


class FakePane:
    """Scripts the pane's ANSI captures across the transaction's phases.

    ``captures`` is consumed in order; the LAST one repeats (so a bounded retry
    or the confirm poll keeps seeing the terminal state).
    """

    def __init__(self, captures: list[str], *, cc_version: str = "2.1.207"):
        self.captures = list(captures)
        self.cc_version = cc_version
        self.keys: list[tuple[str, bool, bool]] = []
        self.send_ok = True
        self.enter_ok = True

    async def capture(self, window_id: str, *, with_ansi: bool = False) -> str:
        return self.captures.pop(0) if len(self.captures) > 1 else self.captures[0]

    async def pane_current_command(self, window_id: str) -> str | None:
        return self.cc_version

    async def find_window_by_id(self, window_id: str):
        class W:
            pass

        w = W()
        w.window_id = window_id  # type: ignore[attr-defined]
        return w

    async def send_keys(
        self, window_id: str, text: str, enter: bool = True, literal: bool = True
    ) -> bool:
        self.keys.append((text, enter, literal))
        if enter and not text:
            return self.enter_ok
        return self.send_ok

    @property
    def arrows(self) -> list[str]:
        return [t for t, e, lit in self.keys if not lit and not e]

    @property
    def literal_writes(self) -> list[str]:
        return [t for t, e, lit in self.keys if lit]

    @property
    def enter_sent(self) -> bool:
        return any(e and not t for t, e, _ in self.keys)


@pytest.fixture(autouse=True)
def _reset():
    free_text.reset_for_tests()
    tmux_mod.tmux_manager.reset_stranded_drafts_for_tests()
    tmux_mod.tmux_manager.reset_window_send_locks_for_tests()
    yield
    free_text.reset_for_tests()
    tmux_mod.tmux_manager.reset_stranded_drafts_for_tests()


@pytest.fixture
def stamped(monkeypatch):
    """Capture the pre-commit user-turn stamp (plan §2.4 [r5 P1-1])."""
    seen: list[tuple[int, int | None, str]] = []
    import cctelegram.handlers.message_queue as mq

    monkeypatch.setattr(
        mq,
        "set_route_user_turn_at",
        lambda u, t, w: seen.append((u, t, w)),
    )
    return seen


# The REAL anchor reader, captured at import — BEFORE the autouse stub below
# replaces it. The round-4 P1 test drives the genuine ``auq_source`` reader
# against a real session_map + side files, so it must be able to restore this.
_REAL_AUQ_ANCHOR = auq_source.peek_surface_identity_for_window


@pytest.fixture(autouse=True)
def auq_anchor(monkeypatch):
    """The AUQ out-of-band anchor (the PreToolUse side file's occurrence id).

    AUTOUSE, because the anchor is MANDATORY (peer-review round-2 P1): without a
    live side file the lane DECLINES, so every test needs one — and the tests
    that assert the decline set it to ``None`` themselves.

    A mutable one-element list, so a test can ROTATE the anchor mid-flight —
    which is what a genuinely new AUQ does (its hook rewrites the side file) — or
    drop it to ``None`` (the card resolved and its file was unlinked).

    The value's SHAPE mirrors production (round-4 P1): the anchor embeds the
    window's FRESHLY-resolved session id, so a session rotation is itself an
    anchor change. ``TestTheSessionGenerationIsPartOfTheAnchor`` exercises the
    real reader end-to-end rather than this stub.
    """
    box: list[str | None] = ["auq:sid:SESSION_A:tu:toolu_CARD_X"]
    monkeypatch.setattr(
        auq_source, "peek_surface_identity_for_window", lambda _w: box[0]
    )
    return box


def _wire(monkeypatch, pane: FakePane) -> None:
    tm = tmux_mod.tmux_manager
    monkeypatch.setattr(tm, "capture_pane_cancellation_safe", pane.capture)
    monkeypatch.setattr(tm, "pane_current_command", pane.pane_current_command)
    monkeypatch.setattr(tm, "find_window_by_id", pane.find_window_by_id)
    monkeypatch.setattr(tm, "send_keys", pane.send_keys)
    monkeypatch.setattr(free_text, "NAV_SETTLE_S", 0)
    monkeypatch.setattr(free_text, "TEXT_SETTLE_S", 0)
    monkeypatch.setattr(free_text, "COMMIT_SETTLE_S", 0)
    # The 👤-echo dedup needs a bound window; not the subject here.
    monkeypatch.setattr(free_text, "_record_bot_sent", lambda w, p: None)


class TestAuqHappyPath:
    @pytest.mark.asyncio
    async def test_navigates_types_verifies_and_commits(self, monkeypatch, stamped):
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(
            WINDOW, AUQ_X_ANSWER, user_turn=STAMP, display="proj"
        )

        assert result is not None and result.ok, result
        assert result.outcome is DeliveryOutcome.DELIVERED
        # N = 3 real options ⇒ the free-text row is 4 ⇒ 3 Downs (cursor starts at 1).
        assert pane.arrows == ["Down", "Down", "Down"]
        assert pane.literal_writes == [AUQ_X_ANSWER]
        assert pane.enter_sent is True
        # The Enter carried the user-turn stamp — PR-2 is the FIFTH commit path.
        assert stamped == [(1, 42, WINDOW)]
        # Committed cleanly ⇒ no stranded-draft brake.
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is False

    @pytest.mark.asyncio
    async def test_the_typed_state_proof_is_what_authorizes_the_enter(
        self, monkeypatch, stamped
    ):
        """If the row is STILL the dim placeholder after the write, nothing landed
        — so the Enter must be withheld (it would commit the free-text row EMPTY)."""
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_LANDED, AUQ_X_LANDED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is not None
        assert result.outcome is DeliveryOutcome.DRAFT_WRITTEN
        assert result.reason == delivery.REASON_FREE_TEXT_VERIFY_FAILED
        assert pane.enter_sent is False
        assert stamped == []  # a withheld Enter is NEVER stamped
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is True


class TestTheCursorIsAlreadyOnTheFreeTextRow:
    """peer-review P2 — the card's OWN ↑/↓ buttons put it there.

    ``_parse_numbered_options`` DROPS the affordance row and clears every real
    option's cursor when the ❯ is parked on it, so the form reports NO cursor at
    all. Reading that as "we can't find the cursor" and declining meant the most
    natural gesture the card invites — nav to ``Type something.``, then send
    prose — was the one gesture that got REFUSED.
    """

    @pytest.mark.asyncio
    async def test_zero_nav_keystrokes_and_still_commits(self, monkeypatch, stamped):
        # AUQ_X_LANDED IS the "cursor already on row 4" capture, straight from the rig.
        pane = FakePane([AUQ_X_LANDED, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is not None and result.ok, result
        assert pane.arrows == [], "the cursor is already there — nav must be a no-op"
        assert pane.literal_writes == [AUQ_X_ANSWER]
        assert pane.enter_sent is True
        assert stamped == [(1, 42, WINDOW)]

    @pytest.mark.asyncio
    async def test_the_landing_proof_still_runs(self, monkeypatch):
        """Zero nav does NOT mean zero proof: the row must still be SGR-2 dim
        before a single byte is typed into it."""
        # A pane whose row 4 carries the cursor but is ALREADY typed (a leftover
        # draft) — no dim ⇒ the landing proof fails ⇒ decline, nothing typed.
        pane = FakePane([AUQ_X_TYPED, AUQ_X_TYPED, AUQ_X_TYPED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, "something else", user_turn=STAMP)

        assert result is None  # pre-write bail ⇒ PR-1 owns it
        assert pane.literal_writes == []

    @pytest.mark.asyncio
    async def test_a_cursor_on_the_chat_about_this_row_is_not_the_target(
        self, monkeypatch
    ):
        """Row 5 (``Chat about this``) is ALSO an affordance, so it too clears the
        real-option cursors — but it is not row 4, and we must not silently treat
        it as if it were."""
        from tests.free_text_frames import move_cursor_to_row

        pane = FakePane([move_cursor_to_row(AUQ_X_LANDED, 5)])
        _wire(monkeypatch, pane)

        assert await free_text.try_answer(WINDOW, PAYLOAD, user_turn=STAMP) is None
        assert pane.keys == []


class TestTheWrongCard:
    """peer-review P1 — the pane can be in the right STATE and be the wrong CARD.

    Every OTHER leg of the proof set is satisfied by a successor card holding our
    text: it owns the pane (no input box), our bytes ARE on it, its row N+1 carries
    our cursor and our text at normal intensity, and its footer says the free-text
    row is active. Only IDENTITY says no.
    """

    @pytest.mark.asyncio
    async def test_auq_replaced_by_a_same_geometry_auq_before_the_enter(
        self, monkeypatch, stamped
    ):
        """Card X is planned; card Y — a DIFFERENT question with the SAME 3-option
        geometry and the SAME typed row — is live by the final capture. REAL rig
        bytes for both."""
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_Y_TYPED, AUQ_Y_TYPED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is not None
        assert result.outcome is DeliveryOutcome.DRAFT_WRITTEN
        assert result.reason == delivery.REASON_FREE_TEXT_VERIFY_FAILED
        assert pane.enter_sent is False, "the answer must NOT be submitted to card Y"
        assert stamped == []
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is True

    @pytest.mark.asyncio
    async def test_auq_replaced_by_an_EXITPLANMODE_before_the_enter(
        self, monkeypatch, stamped
    ):
        """AUQ → ExitPlanMode: the worst available swap.

        EPM's option 1 is "Yes, and bypass permissions", and EPM is no longer a
        free-text surface at all (owner decision 2026-07-12) — so a transaction
        that planned against an AUQ and finds a plan prompt on the pane must
        WITHHOLD the Enter, not "fall back" to anything. Leg C's
        ``extract_interactive_content`` surface check is what says no."""
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, EPM_LIVE, EPM_LIVE])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is not None
        assert result.outcome is DeliveryOutcome.DRAFT_WRITTEN
        assert pane.enter_sent is False
        assert stamped == []
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is True

    @pytest.mark.asyncio
    async def test_the_swap_is_caught_AFTER_THE_NAV_TOO_with_nothing_typed(
        self, monkeypatch
    ):
        """The identity is re-checked at BOTH points that bracket a keystroke. A
        swap during the NAV is caught before a single byte is written, so it is a
        clean pre-write bail: fall through to PR-1, no draft, no brake."""
        pane = FakePane([AUQ_X_LIVE, AUQ_Y_LIVE, AUQ_Y_LIVE])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is None
        assert pane.literal_writes == []
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is False

    @pytest.mark.asyncio
    async def test_a_ROTATED_auq_side_file_refuses_even_when_the_pane_agrees(
        self, monkeypatch, auq_anchor, stamped
    ):
        """The out-of-band anchor is an INDEPENDENT gate. Even if the successor
        card were byte-identical on the pane (a re-asked question), its PreToolUse
        hook rewrote the side file with a new ``tool_use_id`` — and that alone is
        enough to refuse."""
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_X_TYPED])
        _wire(monkeypatch, pane)

        calls = {"n": 0}
        real_capture = pane.capture

        async def capture_and_rotate(window_id, *, with_ansi=False):
            out = await real_capture(window_id, with_ansi=with_ansi)
            calls["n"] += 1
            # Rotate after capture 2 (the landing), so the PRE-ENTER observation
            # reads the successor's id — the anchor is read BEFORE its own pane
            # capture, and in reality the successor's PreToolUse hook fires before
            # its prompt renders, so that is the true ordering.
            if calls["n"] >= 2:
                auq_anchor[0] = "auq:sid:SESSION_A:tu:toolu_CARD_X_REASKED"  # a NEW AUQ
            return out

        monkeypatch.setattr(
            tmux_mod.tmux_manager, "capture_pane_cancellation_safe", capture_and_rotate
        )

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is not None and not result.ok
        assert pane.enter_sent is False
        assert stamped == []

    @pytest.mark.asyncio
    async def test_a_VANISHED_auq_side_file_refuses(
        self, monkeypatch, auq_anchor, stamped
    ):
        """The side file is unlinked at the AUQ's ``tool_result``. Its absence is
        proof the card we planned against RESOLVED — never a licence to proceed."""
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_X_TYPED])
        _wire(monkeypatch, pane)

        calls = {"n": 0}
        real_capture = pane.capture

        async def capture_and_drop(window_id, *, with_ansi=False):
            out = await real_capture(window_id, with_ansi=with_ansi)
            calls["n"] += 1
            if calls["n"] >= 2:
                auq_anchor[0] = None
            return out

        monkeypatch.setattr(
            tmux_mod.tmux_manager, "capture_pane_cancellation_safe", capture_and_drop
        )

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is None or not result.ok
        assert pane.enter_sent is False
        assert stamped == []


class TestTheStrandedDraftBrake:
    """peer-review P1 — the free-text lane must not be a way AROUND PR-1's brake."""

    @pytest.mark.asyncio
    async def test_a_braked_window_declines_with_zero_keystrokes(self, monkeypatch):
        """An earlier delivery may have left a payload UNSENT in this pane — or a
        COMMIT_UNKNOWN whose Enter actually landed and advanced Claude to another
        live card. Navigating and typing into whatever is on the pane NOW is exactly
        the append-and-commit chain the brake exists to break."""
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_RESOLVED])
        _wire(monkeypatch, pane)
        tmux_mod.tmux_manager.mark_window_stranded_draft(WINDOW)

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is None, "DECLINE — PR-1 owns the single refusal + notice"
        assert pane.keys == []

    @pytest.mark.asyncio
    async def test_the_lane_never_clears_the_brake(self, monkeypatch):
        """Its release rules (an empty-input-row capture, or confirmed window
        death) are PR-1's, and they are the only proofs that mean anything."""
        pane = FakePane([AUQ_X_LIVE])
        _wire(monkeypatch, pane)
        tmux_mod.tmux_manager.mark_window_stranded_draft(WINDOW)

        await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is True


class TestTheOwnersActualUseCase:
    """A LARGE (voice-note-shaped, reply-quoted) free-text answer — the primary path."""

    @pytest.mark.asyncio
    async def test_947_char_multiline_payload_commits(self, monkeypatch, stamped):
        # The real 947-char, 9-line payload whose typed render IS the fixture.
        assert len(BIG_ANSWER) > 800
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED_BIG, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, BIG_ANSWER, user_turn=STAMP)

        assert result is not None and result.ok, result
        # ONE literal write — exactly what the bot's send_keys does, and what the
        # rig proved does NOT paste-collapse on an affordance row.
        assert pane.literal_writes == [BIG_ANSWER]
        assert pane.enter_sent is True
        assert stamped == [(1, 42, WINDOW)]

    @pytest.mark.asyncio
    async def test_overflow_commits_on_the_footer_proof_PLUS_the_anchor(
        self, monkeypatch, auq_anchor, stamped
    ):
        """A ~5.3 k answer scrolls the whole option block — the ``❯ 4.`` row
        INCLUDED — off the pane (a TUI has no scrollback), so the pane-derived
        identity is gone. The footer's ``ctrl+g to edit`` still proves WHICH ROW,
        and the side-file anchor still proves WHICH CARD. Both, or nothing."""
        assert (
            terminal_parser.free_text_surface_identity(
                plain(AUQ_X_OVERFLOW), target_row=4
            )
            is None
        ), "premise: the option block is genuinely unrecoverable from this pane"

        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_OVERFLOW, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, OVERFLOW_ANSWER, user_turn=STAMP)

        assert result is not None and result.ok, result
        assert pane.enter_sent is True
        assert terminal_parser.parse_free_text_row(AUQ_X_OVERFLOW, number=4) is None


class TestTheAuqAnchorIsMANDATORY:
    """peer-review round-2 P1 — the AUQ anchor was OPTIONAL, so identity silently
    degraded to the PANE, which cannot tell two same-shaped cards apart.

    Worse, an identity captured with ``anchor=None`` IGNORED a successor's
    non-``None`` anchor rather than treating it as a mismatch — so card A's text
    could be committed onto card B. The anchor is now mandatory on BOTH sides of
    every comparison, and its absence declines BEFORE the first keystroke.
    """

    @pytest.mark.asyncio
    async def test_no_side_file_anchor_DECLINES_with_zero_keystrokes(
        self, monkeypatch, auq_anchor, stamped
    ):
        """No PreToolUse side file (the hook isn't installed / hasn't fired / was
        GC'd) ⇒ nothing OCCURRENCE-unique identifies the card, so the lane never
        starts. PR-1 owns the single refusal; the user still gets a clean
        "answer the card" notice, and NOTHING is typed."""
        auq_anchor[0] = None
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is None, "DECLINE — never commit on the pane identity alone"
        assert pane.keys == [], "not one keystroke may reach the pane"
        assert stamped == []
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is False

    @pytest.mark.asyncio
    async def test_two_AUQs_with_IDENTICAL_options_are_told_apart_by_the_anchor(
        self, monkeypatch, auq_anchor, stamped
    ):
        """The hardest AUQ case, and the reason the pane alone is not enough: the
        successor is BYTE-IDENTICAL on the pane (a re-asked question — same
        options, and a pure-pane parse carries no title), so its pane identity
        MATCHES. Only its side file's fresh ``tool_use_id`` says it is a different
        occurrence."""
        assert terminal_parser.free_text_surface_identity(
            plain(AUQ_X_LIVE), target_row=4
        ) == terminal_parser.free_text_surface_identity(
            plain(AUQ_X_TYPED), target_row=4
        ), "premise: the two occurrences are INDISTINGUISHABLE by their option rows"

        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_X_TYPED])
        _wire(monkeypatch, pane)

        calls = {"n": 0}
        real_capture = pane.capture

        async def capture_and_reask(window_id, *, with_ansi=False):
            out = await real_capture(window_id, with_ansi=with_ansi)
            calls["n"] += 1
            # After capture 2 (the landing): the pre-Enter observation reads its
            # anchor BEFORE its own capture, and the successor's hook fires before
            # the successor's prompt renders.
            if calls["n"] >= 2:
                auq_anchor[0] = "tu:toolu_CARD_X_REASKED"
            return out

        monkeypatch.setattr(
            tmux_mod.tmux_manager, "capture_pane_cancellation_safe", capture_and_reask
        )

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is not None
        assert result.outcome is DeliveryOutcome.DRAFT_WRITTEN
        assert pane.enter_sent is False, "the re-asked question must not be answered"
        assert stamped == []
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is True

    @pytest.mark.asyncio
    async def test_a_CAPTURED_none_anchor_can_never_accept_a_LATER_one(
        self, monkeypatch, auq_anchor, stamped
    ):
        """The precise old hole: with ``anchor=None`` captured, ``still_holds``
        skipped the anchor check entirely, so a successor's non-``None`` anchor was
        IGNORED instead of refused. It is now unreachable BY CONSTRUCTION — an
        anchor-less pane never yields an identity, so the lane never starts."""
        auq_anchor[0] = None
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_X_TYPED])
        _wire(monkeypatch, pane)

        calls = {"n": 0}
        real_capture = pane.capture

        async def capture_and_appear(window_id, *, with_ansi=False):
            out = await real_capture(window_id, with_ansi=with_ansi)
            calls["n"] += 1
            if calls["n"] >= 2:
                auq_anchor[0] = "tu:toolu_A_DIFFERENT_CARD"
            return out

        monkeypatch.setattr(
            tmux_mod.tmux_manager, "capture_pane_cancellation_safe", capture_and_appear
        )

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is None
        assert pane.keys == []
        assert stamped == []


class TestOverflowWithoutAnAnchorNeverStarts:
    @pytest.mark.asyncio
    async def test_no_side_file_means_no_lane_at_all(
        self, monkeypatch, auq_anchor, stamped
    ):
        """Pre-fix this typed the payload and only THEN discovered — when the
        option block scrolled away — that nothing identified the card, stranding
        the draft and braking the topic. With the anchor mandatory at plan time
        the lane declines up front: strictly better, and the payload still reaches
        the user's eyes through PR-1's refusal."""
        auq_anchor[0] = None
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_OVERFLOW, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, OVERFLOW_ANSWER, user_turn=STAMP)

        assert result is None
        assert pane.keys == []
        assert stamped == []
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is False


class TestTheAnchorIsReadBeforeThePane:
    """peer-review round-3 P1, the OTHER half — the ORDERING.

    Swapping the anchor's SOURCE does NOT by itself close the finding, because the
    finding is a TOCTOU: the identity was minted from a pane captured at t1 and an
    anchor read at t2 > t1. A card turning over inside that gap yields
    `(OLD pane, NEW anchor)` — and since the pane component is degenerate across
    same-shaped occurrences (a re-asked question renders byte-identical rows), that
    chimera MATCHES every later observation and the Enter commits onto the
    successor.

    Reading the anchor STRICTLY BEFORE the pane makes the only reachable chimera
    `(NEWER pane, OLDER anchor)`, which fails closed on the anchor comparison.
    """

    def test_derive_identity_does_not_read_an_anchor_itself(self):
        """The structural pin: the anchor is an ARGUMENT. A ``derive_identity``
        that reads its own anchor is reading it AFTER its caller captured the
        pane — the ordering bug, reintroduced."""
        ident = free_text.derive_identity(
            plain(AUQ_X_LIVE),
            surface=free_text.SURFACE_AUQ,
            target_row=4,
            anchor="auq:sid:SESSION_A:tu:toolu_CARD_X",
        )
        assert ident is not None
        assert ident.anchor == "auq:sid:SESSION_A:tu:toolu_CARD_X"

        assert (
            free_text.derive_identity(
                plain(AUQ_X_LIVE),
                surface=free_text.SURFACE_AUQ,
                target_row=4,
                anchor=None,
            )
            is None
        ), "no occurrence anchor ⇒ no identity, ever"

    @pytest.mark.asyncio
    async def test_a_card_that_turns_over_INSIDE_the_capture_never_mints_a_chimera(
        self, monkeypatch, auq_anchor, stamped
    ):
        """THE ROUND-3 P1, reduced to its mechanism.

        Card X's picker is what we capture; the successor's hook fires DURING that
        capture (the pane bytes we hold are X's, the side file is already the
        successor's).

        Under the OLD ordering — pane captured first, anchor read after — the
        identity minted here is the CHIMERA ``(X's pane, the successor's anchor)``.
        Every later observation sees ``(successor's pane, successor's anchor)``,
        and because a re-asked question renders BYTE-IDENTICAL option rows, the
        pane halves match too. Both components agree, the transaction types into
        the successor and presses Enter — answering the WRONG QUESTION. (Verified
        RED by restoring the old order.)

        Reading the anchor FIRST mints ``(X's pane, X's anchor)`` instead, so the
        very next observation compares X's anchor against the successor's and
        refuses.

        The frames are the ORDINARY happy-path sequence on purpose: every other
        leg (landing, SGR-2 dim, typed-state, payload tail) PASSES, so identity is
        the only thing that can refuse — which is precisely the point.
        """
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        calls = {"n": 0}
        real_capture = pane.capture

        async def rotate_during_the_first_capture(window_id, *, with_ansi=False):
            out = await real_capture(window_id, with_ansi=with_ansi)
            calls["n"] += 1
            if calls["n"] == 1:
                # The successor's PreToolUse hook fires while our capture is in
                # flight: the bytes we just took are X's, the side file is now
                # the successor's.
                auq_anchor[0] = "auq:sid:SESSION_A:tu:toolu_CARD_X2"
            return out

        monkeypatch.setattr(
            tmux_mod.tmux_manager,
            "capture_pane_cancellation_safe",
            rotate_during_the_first_capture,
        )

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert pane.enter_sent is False, (
            "the answer was committed onto the successor card — the chimera is back"
        )
        assert stamped == []
        # Caught BEFORE a byte is typed (the post-nav identity check), so the lane
        # declines and PR-1 owns the single refusal.
        assert result is None
        assert pane.literal_writes == []
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is False

    @pytest.mark.asyncio
    async def test_every_pane_capture_is_preceded_by_an_anchor_read(
        self, monkeypatch, auq_anchor
    ):
        """The ordering, observed on the real transaction: every capture has an
        anchor read strictly before it."""
        events: list[str] = []
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        real_capture = pane.capture

        async def traced_capture(window_id, *, with_ansi=False):
            events.append("capture")
            return await real_capture(window_id, with_ansi=with_ansi)

        monkeypatch.setattr(
            tmux_mod.tmux_manager, "capture_pane_cancellation_safe", traced_capture
        )
        monkeypatch.setattr(
            auq_source,
            "peek_surface_identity_for_window",
            lambda _w: (events.append("anchor"), auq_anchor[0])[1],
        )

        await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        # Drop the confirm-advance captures that trail the Enter (no identity is
        # derived from those) by pairing from the front.
        assert events[0] == "anchor", "the FIRST observation reads the anchor first"
        for i, ev in enumerate(events):
            if ev == "capture":
                assert "anchor" in events[:i], (
                    "a pane capture with no anchor read before it — the chimera "
                    "window is open"
                )
                break
        # Each of the three identity observations is anchor-then-capture.
        assert events[:2] == ["anchor", "capture"]


class TestTheAdditiveInvariant:
    """Every pre-write bail returns None ⇒ the caller falls through to PR-1."""

    @pytest.mark.asyncio
    async def test_flag_off_declines(self, monkeypatch):
        free_text.set_enabled(False)
        pane = FakePane([AUQ_X_LIVE])
        _wire(monkeypatch, pane)
        assert await free_text.try_answer(WINDOW, PAYLOAD, user_turn=STAMP) is None
        assert pane.keys == []

    @pytest.mark.asyncio
    async def test_unlicensed_cc_version_declines(self, monkeypatch):
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED], cc_version="2.1.208")
        _wire(monkeypatch, pane)
        assert await free_text.try_answer(WINDOW, PAYLOAD, user_turn=STAMP) is None
        assert pane.keys == [], "an unlicensed version must never send a keystroke"

    @pytest.mark.asyncio
    async def test_lone_digit_payload_declines_without_a_capture(self, monkeypatch):
        """A bare digit is a live HOTKEY on these surfaces — it must never be
        typed. Falling through is correct AND sufficient: PR-1's step 0 applies
        the SAME rule and owns the refusal, so the user gets exactly one message."""
        pane = FakePane([AUQ_X_LIVE])
        _wire(monkeypatch, pane)
        assert await free_text.try_answer(WINDOW, "3", user_turn=STAMP) is None
        assert pane.keys == []

    @pytest.mark.asyncio
    async def test_multiselect_declines(self, monkeypatch):
        pane = FakePane([_fx(f"auq_multi_picker_{V}.txt")])
        _wire(monkeypatch, pane)
        assert await free_text.try_answer(WINDOW, PAYLOAD, user_turn=STAMP) is None
        assert pane.keys == []

    @pytest.mark.asyncio
    async def test_folder_trust_declines(self, monkeypatch):
        pane = FakePane([_fx("folder_trust_arrival_plain_v2.1.207.txt")])
        _wire(monkeypatch, pane)
        assert await free_text.try_answer(WINDOW, PAYLOAD, user_turn=STAMP) is None
        assert pane.keys == []

    @pytest.mark.asyncio
    async def test_switch_model_declines(self, monkeypatch):
        pane = FakePane([_fx("decision_switch_model_v2.1.200.txt")])
        _wire(monkeypatch, pane)
        assert await free_text.try_answer(WINDOW, PAYLOAD, user_turn=STAMP) is None
        assert pane.keys == []

    @pytest.mark.asyncio
    async def test_permission_and_workflow_gates_decline(self, monkeypatch):
        for name in (f"gate_permission_{V}.txt", f"gate_workflow_{V}.txt"):
            pane = FakePane([_fx(name)])
            _wire(monkeypatch, pane)
            assert await free_text.try_answer(WINDOW, PAYLOAD, user_turn=STAMP) is None
            assert pane.keys == [], name

    @pytest.mark.asyncio
    async def test_not_claude_declines(self, monkeypatch):
        pane = FakePane([AUQ_X_LIVE], cc_version="zsh")
        _wire(monkeypatch, pane)
        assert await free_text.try_answer(WINDOW, PAYLOAD, user_turn=STAMP) is None
        assert pane.keys == []

    @pytest.mark.asyncio
    async def test_landing_unproven_declines_with_NOTHING_typed(self, monkeypatch):
        """The nav didn't land (the post-nav capture still shows the cursor on
        option 1). Nothing was typed ⇒ fall through, clean."""
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LIVE, AUQ_X_LIVE])
        _wire(monkeypatch, pane)
        result = await free_text.try_answer(WINDOW, PAYLOAD, user_turn=STAMP)
        assert result is None
        assert pane.literal_writes == []
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is False


class TestTheCommitBoundary:
    @pytest.mark.asyncio
    async def test_a_failed_enter_is_commit_unknown_and_KEEPS_its_stamp(
        self, monkeypatch, stamped
    ):
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_X_TYPED])
        pane.enter_ok = False
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is not None
        assert result.outcome is DeliveryOutcome.COMMIT_UNKNOWN
        # A tmux failure does NOT prove the key never reached the pty, so a
        # possibly-committed turn keeps its boundary (the r2 F3 invariant).
        assert stamped == [(1, 42, WINDOW)]
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is True

    @pytest.mark.asyncio
    async def test_enter_sent_but_surface_still_up_is_commit_unconfirmed(
        self, monkeypatch, stamped
    ):
        """Never auto-retried — the Enter cannot be un-sent."""
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_X_TYPED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is not None
        assert result.outcome is DeliveryOutcome.COMMIT_UNKNOWN
        assert result.reason == delivery.REASON_FREE_TEXT_COMMIT_UNCONFIRMED
        assert pane.keys.count(("", True, False)) == 1  # exactly ONE Enter

    @pytest.mark.asyncio
    async def test_stamp_exception_withholds_the_enter(self, monkeypatch):
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        def boom(*_a):
            raise RuntimeError("stamp exploded")

        monkeypatch.setattr(free_text, "_stamp", boom)

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is not None
        assert result.outcome is DeliveryOutcome.DRAFT_WRITTEN
        assert result.reason == delivery.REASON_STAMP_FAILED
        assert pane.enter_sent is False

    @pytest.mark.asyncio
    async def test_a_cancellation_after_the_write_arms_the_brake_and_reraises(
        self, monkeypatch
    ):
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        real_verify = free_text._verify_typed

        async def cancel_mid_verify(*a, **k):
            raise asyncio.CancelledError()

        monkeypatch.setattr(free_text, "_verify_typed", cancel_mid_verify)

        with pytest.raises(asyncio.CancelledError):
            await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        # The payload may be sitting in the affordance row ⇒ the brake goes up,
        # and the cancellation PROPAGATES (never swallowed into a DeliveryResult).
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is True
        assert real_verify is not None


class TestTheIdentityIsStableAcrossOurOWNMutations:
    """The drift trap: the executor MUTATES the pane it must re-identify.

    If the identity moved when the cursor moved, or when the payload replaced the
    affordance label, EVERY commit would refuse — the check would be a
    self-inflicted denial of service rather than a safety property.
    """

    def test_the_cursor_move_does_not_move_the_identity(self):
        pre = terminal_parser.free_text_surface_identity(
            plain(AUQ_X_LIVE), target_row=4
        )
        post = terminal_parser.free_text_surface_identity(
            plain(AUQ_X_LANDED), target_row=4
        )
        assert pre is not None and pre == post

    def test_typing_into_the_row_does_not_move_the_identity(self):
        """The load-bearing one. ``_parse_numbered_options`` DROPS an affordance
        row, so the instant our text lands in row 4 it stops being an affordance
        and parses as a FOURTH REAL OPTION — a naive form fingerprint moves. The
        identity is TARGET-ROW-BLIND, so it does not."""
        base = terminal_parser.free_text_surface_identity(
            plain(AUQ_X_LANDED), target_row=4
        )
        for typed in (AUQ_X_TYPED, AUQ_X_TYPED_BIG):
            assert (
                terminal_parser.free_text_surface_identity(plain(typed), target_row=4)
                == base
            )

    def test_a_different_question_has_a_different_identity(self):
        assert terminal_parser.free_text_surface_identity(
            plain(AUQ_X_LANDED), target_row=4
        ) != terminal_parser.free_text_surface_identity(plain(AUQ_Y_LIVE), target_row=4)

    def test_an_incomplete_option_block_is_UNRECOVERABLE_not_weaker(self):
        """It must never silently degrade to a shorter, weaker prefix."""
        assert (
            terminal_parser.free_text_surface_identity(
                plain(AUQ_X_OVERFLOW), target_row=4
            )
            is None
        )


class TestTheSessionGenerationIsPartOfTheAnchor:
    """peer-review round-4 P1 — THE STALE SESSION CACHE DEFEATED THE ANCHOR.

    The anchor is only as good as the SESSION it is resolved for. It used to be
    resolved through the CACHED ``WindowState.session_id`` — a mirror of the
    hook-written ``session_map.json`` that is only refreshed when the monitor's
    poll loop reloads it, so it LAGS by up to a poll cycle (longer under load).

    The interleaving that exploits the lag (Codex's, verbatim):

      1. card A is live in window @N (session A);
      2. the user ``/clear``s — ``SessionStart`` writes session B into the map;
      3. session B renders its OWN AskUserQuestion card;
      4. the bot's CACHED ``WindowState.session_id`` still says A;
      5. so all three observations read session A's side file while capturing
         session B's pane. They AGREE WITH EACH OTHER — a self-consistent
         fiction — and a re-asked question is pane-degenerate, so nothing
         refuses;
      6. Enter commits the user's answer onto card B: THE WRONG QUESTION.

    A per-window predicate could not have caught it either: both sessions occupy
    the SAME tmux window, so any ``window_key`` check matches.

    These tests drive the GENUINE ``auq_source`` reader over a GENUINE
    ``session_map.json`` + real side files — the stub anchor fixture is restored
    to the real function — because the bug lives precisely in which source the
    reader consults.
    """

    _SESSION_A = "550e8400-e29b-41d4-a716-4466554400aa"
    _SESSION_B = "550e8400-e29b-41d4-a716-4466554400bb"

    _TOOL_INPUT = {
        "questions": [
            {
                "question": "What's your favorite color?",
                "header": "Color",
                "options": [{"label": "Blue"}, {"label": "Green"}, {"label": "Red"}],
                "multiSelect": False,
            }
        ]
    }

    def _map(self, cc_dir: Path, session_id: str) -> None:
        """What ``SessionStart`` writes — the AUTHORITY on which session a window
        is running, and the thing the cache merely mirrors."""
        from cctelegram.config import config

        (cc_dir / "session_map.json").write_text(
            json.dumps(
                {
                    f"{config.tmux_session_name}:{WINDOW}": {
                        "session_id": session_id,
                        "cwd": "/repo",
                        "window_name": "repo",
                    }
                }
            )
        )

    def _side_file(self, cc_dir: Path, session_id: str, tool_use_id: str) -> Path:
        """What ``PreToolUse(AskUserQuestion)`` writes before a picker renders."""
        pending = cc_dir / "auq_pending"
        pending.mkdir(mode=0o700, exist_ok=True)
        path = pending / f"{session_id}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": session_id,
                    "tool_use_id": tool_use_id,
                    "tool_input": self._TOOL_INPUT,
                    "written_at": time.time(),
                    "input_fingerprint": "",
                    "transcript_path": "",
                    "cwd": "/repo",
                }
            )
        )
        return path

    @pytest.fixture
    def real_anchor(self, monkeypatch, tmp_path):
        """Restore the REAL reader (the autouse fixture stubs it) and give it a
        real config dir to read."""
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        monkeypatch.setattr(
            auq_source, "peek_surface_identity_for_window", _REAL_AUQ_ANCHOR
        )
        auq_source.reset_for_tests()
        yield tmp_path
        auq_source.reset_for_tests()

    def _bind_stale_cache(self, monkeypatch, session_id: str) -> None:
        from cctelegram.session import WindowState, session_manager

        monkeypatch.setitem(
            session_manager.window_states,
            WINDOW,
            WindowState(cwd="/repo", session_id=session_id),
        )

    @pytest.mark.asyncio
    async def test_a_session_rotation_mid_transaction_never_answers_the_NEW_CARD(
        self, monkeypatch, real_anchor, stamped
    ):
        """THE ROUND-4 P1, end to end, on the real reader.

        The transaction plans against session A's card. Mid-flight the user
        ``/clear``s: the map flips to session B, B's PreToolUse hook writes B's
        side file, and B renders a picker whose option rows are BYTE-IDENTICAL
        (a re-asked question — the pane component cannot see the difference).

        Session A's side file is STILL ON DISK — the monitor unlinks it on its
        own poll cycle, and this whole transaction runs inside that window. That
        is exactly what made the stale cache lethal rather than merely stale:
        the cached read RESOLVES, consistently, to a card that is no longer on
        the pane.

        RED before the fix (verified by reverting ``auq_source`` to
        ``peek_session_id_for_window``): all three observations return session
        A's ``tu:toolu_CARD_A``, identity holds, and the Enter FIRES — the user's
        answer is committed onto session B's question.
        """
        cc_dir = real_anchor
        self._map(cc_dir, self._SESSION_A)
        a_side_file = self._side_file(cc_dir, self._SESSION_A, "toolu_CARD_A")
        self._bind_stale_cache(monkeypatch, self._SESSION_A)  # the MIRROR, lagging

        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        calls = {"n": 0}
        real_capture = pane.capture

        async def clear_after_the_first_capture(window_id, *, with_ansi=False):
            out = await real_capture(window_id, with_ansi=with_ansi)
            calls["n"] += 1
            if calls["n"] == 1:
                # /clear: SessionStart rewrites the map, PreToolUse writes B's
                # side file, B's picker renders. The bot's WindowState cache is
                # NOT updated — the monitor's poll loop has not run yet.
                self._map(cc_dir, self._SESSION_B)
                self._side_file(cc_dir, self._SESSION_B, "toolu_CARD_B")
            return out

        monkeypatch.setattr(
            tmux_mod.tmux_manager,
            "capture_pane_cancellation_safe",
            clear_after_the_first_capture,
        )

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert a_side_file.exists(), (
            "premise: the OLD session's side file is still on disk — the monitor "
            "reaps it on its own poll cycle, and this transaction runs inside "
            "that window"
        )
        assert pane.enter_sent is False, (
            "the answer was committed onto the NEW session's card — the stale "
            "session cache defeated the anchor"
        )
        assert stamped == []
        # Caught by the post-nav identity check ⇒ a clean pre-write decline, so
        # PR-1 owns the single refusal and nothing is stranded.
        assert result is None
        assert pane.literal_writes == []
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is False

    @pytest.mark.asyncio
    async def test_the_anchor_tracks_the_MAP_not_the_cached_WindowState(
        self, monkeypatch, real_anchor
    ):
        """The mechanism, isolated: the cache says A, the map says B. The anchor
        must name B — and, since B has no side file yet, DECLINE rather than fall
        back to A's."""
        cc_dir = real_anchor
        self._side_file(cc_dir, self._SESSION_A, "toolu_CARD_A")
        self._bind_stale_cache(monkeypatch, self._SESSION_A)

        self._map(cc_dir, self._SESSION_A)
        anchor_a = auq_source.peek_surface_identity_for_window(WINDOW)
        assert anchor_a is not None and self._SESSION_A in anchor_a

        # The session rotates. A's side file is untouched on disk.
        self._map(cc_dir, self._SESSION_B)
        assert auq_source.peek_surface_identity_for_window(WINDOW) is None, (
            "no side file for the CURRENT session ⇒ DECLINE; never fall back to "
            "the previous session's record"
        )

        # …and once B's hook fires, the anchor is B's — a DIFFERENT value, so a
        # mid-transaction rotation can only ever refuse.
        self._side_file(cc_dir, self._SESSION_B, "toolu_CARD_B")
        anchor_b = auq_source.peek_surface_identity_for_window(WINDOW)
        assert anchor_b is not None and self._SESSION_B in anchor_b
        assert anchor_b != anchor_a

    @pytest.mark.asyncio
    async def test_a_window_absent_from_the_map_declines(
        self, monkeypatch, real_anchor
    ):
        """Fail-closed: an unresolvable session generation is not a licence to
        use the cached one."""
        cc_dir = real_anchor
        self._side_file(cc_dir, self._SESSION_A, "toolu_CARD_A")
        self._bind_stale_cache(monkeypatch, self._SESSION_A)
        (cc_dir / "session_map.json").write_text(json.dumps({}))

        assert auq_source.peek_surface_identity_for_window(WINDOW) is None


class TestAnEmptyToolUseIdDeclines:
    """peer-review round-4 P2 — a missing occurrence witness was SYNTHESIZED.

    ``hook.py`` writes ``tool_use_id: ""`` when the payload carries none, and the
    anchor path then built one out of ``(written_at, content fingerprint)``. That
    is not an occurrence witness: it is a wall-clock stamp plus a content hash —
    two same-session siblings can share a clock quantum, and (there being no
    read-TTL by design) a stale record stays "valid" forever. On the only
    licensed CC version the rig confirms the id is ALWAYS present, so its absence
    is a broken contract, not a degradation to paper over.

    Scoped to the ANCHOR path: the GH #48 recap surface-identity lane builds its
    own composite from ``read_side_file_for_recovery`` and is untouched — a guessy
    identity there costs a duplicate recap, not a wrong keystroke.
    """

    @pytest.mark.asyncio
    async def test_the_anchor_is_None_when_the_hook_captured_no_id(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
        monkeypatch.setattr(
            auq_source, "peek_surface_identity_for_window", _REAL_AUQ_ANCHOR
        )
        auq_source.reset_for_tests()
        session_id = "550e8400-e29b-41d4-a716-4466554400cc"

        from cctelegram.config import config
        from cctelegram.session import WindowState, session_manager

        monkeypatch.setitem(
            session_manager.window_states,
            WINDOW,
            WindowState(cwd="/repo", session_id=session_id),
        )
        (tmp_path / "session_map.json").write_text(
            json.dumps(
                {
                    f"{config.tmux_session_name}:{WINDOW}": {
                        "session_id": session_id,
                        "cwd": "/repo",
                        "window_name": "repo",
                    }
                }
            )
        )
        pending = tmp_path / "auq_pending"
        pending.mkdir(mode=0o700)
        record = {
            "schema_version": 1,
            "session_id": session_id,
            "tool_use_id": "",  # what hook.py writes when the payload has none
            "tool_input": TestTheSessionGenerationIsPartOfTheAnchor._TOOL_INPUT,
            "written_at": time.time(),
            "input_fingerprint": "",
            "transcript_path": "",
            "cwd": "/repo",
        }
        (pending / f"{session_id}.json").write_text(json.dumps(record))

        assert auq_source.peek_surface_identity_for_window(WINDOW) is None

        # …and the recap lane's own composite still works off the SAME record.
        recovered = auq_source.read_side_file_for_recovery(session_id)
        assert recovered is not None
        assert recovered.tool_use_id == ""
        assert recovered.source_fingerprint  # the GH #48 identity input

        auq_source.reset_for_tests()

    @pytest.mark.asyncio
    async def test_the_lane_declines_with_zero_keystrokes(
        self, monkeypatch, auq_anchor, stamped
    ):
        auq_anchor[0] = None
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        assert await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP) is None
        assert pane.keys == []
        assert stamped == []
