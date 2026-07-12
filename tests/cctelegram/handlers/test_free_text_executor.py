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
_REAL_AUQ_ANCHOR = auq_source.peek_surface_anchor_for_window

# The PreToolUse record card X's picker was rendered FROM — the same content the
# real hook would have written. The anchor carries it (round-5 P1-B) so the
# executor can BIND the record to the pane it captured instead of trusting read
# order; every default-anchor test therefore agrees with the card-X frames.
CARD_X_TOOL_INPUT = {
    "questions": [
        {
            "question": "What's your favorite color?",
            "header": "Color",
            "options": [{"label": "Blue"}, {"label": "Green"}, {"label": "Red"}],
            "multiSelect": False,
        }
    ]
}


def _anchor(key: str, tool_input: dict | None = None) -> auq_source.SurfaceAnchor:
    return auq_source.SurfaceAnchor(
        key=key, tool_input=tool_input if tool_input is not None else CARD_X_TOOL_INPUT
    )


@pytest.fixture(autouse=True)
def auq_anchor(monkeypatch):
    """The AUQ out-of-band anchor (the PreToolUse side file's occurrence id).

    AUTOUSE, because the anchor is MANDATORY (peer-review round-2 P1): without a
    live side file the lane DECLINES, so every test needs one — and the tests
    that assert the decline set it to ``None`` themselves.

    A mutable one-element list, so a test can ROTATE the anchor mid-flight —
    which is what a genuinely new AUQ does (its hook rewrites the side file) — or
    drop it to ``None`` (the card resolved and its file was unlinked).

    The value's SHAPE mirrors production: the KEY embeds the window's
    FRESHLY-resolved session id (round-4 P1), so a session rotation is itself an
    anchor change, and the record's CONTENT rides along (round-5 P1-B) so the
    executor can bind it to the pane. ``TestTheSessionGenerationIsPartOfTheAnchor``
    exercises the real reader end-to-end rather than this stub.
    """
    box: list[auq_source.SurfaceAnchor | None] = [
        _anchor("auq:sid:SESSION_A:tu:toolu_CARD_X")
    ]
    monkeypatch.setattr(auq_source, "peek_surface_anchor_for_window", lambda _w: box[0])
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
            # Rotate at the PRE-ENTER observation (capture 3), i.e. AFTER the
            # payload is already in the row — so this pins the POST-write refusal
            # specifically: the Enter is withheld and the brake goes up. (A
            # rotation landing EARLIER is caught pre-write by the round-5 anchor
            # sandwich — ``test_an_anchor_that_MOVES_across_one_observation``.)
            if calls["n"] >= 3:
                auq_anchor[0] = _anchor(
                    "auq:sid:SESSION_A:tu:toolu_CARD_X_REASKED"
                )  # a NEW AUQ
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
            # At the PRE-ENTER observation (capture 3) — the payload is already in
            # the row, so this pins the post-write refusal: the re-asked question
            # must NOT be answered, and the draft is disclosed + braked.
            if calls["n"] >= 3:
                auq_anchor[0] = _anchor("tu:toolu_CARD_X_REASKED")
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
                auq_anchor[0] = _anchor("tu:toolu_A_DIFFERENT_CARD")
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
            anchor=_anchor("auq:sid:SESSION_A:tu:toolu_CARD_X"),
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
                auq_anchor[0] = _anchor("auq:sid:SESSION_A:tu:toolu_CARD_X2")
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
            "peek_surface_anchor_for_window",
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
            auq_source, "peek_surface_anchor_for_window", _REAL_AUQ_ANCHOR
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
            auq_source, "peek_surface_anchor_for_window", _REAL_AUQ_ANCHOR
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


# ── peer-review round 5 ───────────────────────────────────────────────────

# Card B: a DIFFERENT question with BYTE-IDENTICAL option rows. This is the card
# the reviewer's interleaving would have us answer, and it is the hardest shape
# there is — the pane component cannot see the difference (a pure-pane parse
# carries no title and no option descriptions), so the ONLY pane-observable thing
# that separates B from card X is the question text itself.
CARD_B_TOOL_INPUT = {
    "questions": [
        {
            "question": "Which color do you HATE?",
            "header": "Color",
            "options": [{"label": "Blue"}, {"label": "Green"}, {"label": "Red"}],
            "multiSelect": False,
        }
    ]
}


class TestTheAnchorIsBoundToThePane:
    """peer-review round-5 P1-B — "anchor BEFORE pane" is not enough.

    Round 3 argued the dangerous chimera ``(OLD pane, NEW anchor)`` was
    unreachable, because "a live prompt means Claude is BLOCKED on it, so the side
    file must be its own". The unstated premise is that a card the user ALREADY
    ANSWERED stops looking live on the pane — and ``PreToolUse`` writes card B's
    record BEFORE B renders, so there is a window in which the side file names B
    while the pane still renders A. Two AUQs can carry identical option labels, so
    that chimera then matches every later observation and the Enter commits onto
    B: THE WRONG CARD.

    Three folds, none of them a bet on read order:

      * the card must OWN the pane (no input box — a resolved AUQ restores it);
      * each capture is SANDWICHED between two equal anchor reads (``_observe``);
      * the anchor RECORD's CONTENT must AGREE with the pane it is paired with.
    """

    @pytest.mark.asyncio
    async def test_the_reviewers_interleaving_never_answers_the_SUCCESSOR_card(
        self, monkeypatch, auq_anchor, stamped
    ):
        """THE ROUND-5 P1-B, reduced to its mechanism — RED before the fix.

        The reviewer's exact interleaving: card A is on the pane; another
        controller resolves it; B's ``PreToolUse`` hook writes B's record; B has
        not drawn yet, so the pane still holds A's complete picker; the initial
        observation therefore mints ``(pane A, anchor B)``; and B then renders with
        IDENTICAL option geometry, so every later observation agrees with the
        chimera and the Enter lands on B.

        Card B differs from card X ONLY in its question text — which is precisely
        why the pane-identity component (option rows) cannot refuse it, and why
        binding the record's QUESTION to the pane is the leg that must.

        RED before the fix (verified by removing the question binding from
        ``plan_from_pane``): the transaction runs to completion and returns
        DELIVERED — the user's answer to "What's your favorite color?" is committed
        as the answer to "Which color do you HATE?".
        """
        auq_anchor[0] = _anchor("auq:sid:SESSION_A:tu:toolu_CARD_B", CARD_B_TOOL_INPUT)
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        # The premise the pane component CANNOT see: B's rows are card X's rows.
        assert terminal_parser.free_text_surface_identity(
            plain(AUQ_X_LIVE), target_row=4
        ) == terminal_parser.free_text_surface_identity(
            plain(AUQ_X_TYPED), target_row=4
        )

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert pane.enter_sent is False, (
            "the answer was committed onto the SUCCESSOR card — the chimera is back"
        )
        assert result is None, "declines BEFORE a keystroke; PR-1 owns the refusal"
        assert pane.keys == []
        assert stamped == []
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is False

    def test_the_LABEL_comparison_alone_cannot_tell_those_two_cards_apart(self):
        """The measured limit of the reused helper — NOT assumed (this repo's
        "verify the reuse claim on the live call path" rule).

        ``_record_consistent_with_pane`` DOES reject a record whose option labels
        differ from the pane, and DOES NOT reject one whose labels are identical
        but whose QUESTION differs: a pure-pane parse yields
        ``current_question_title is None`` (so the title check is skipped) and
        empty option descriptions. Hence the question-text leg above — and hence
        the honest residual: two AUQs with the same labels AND the same question
        are the same card in every pane-observable respect.
        """
        pane = plain(AUQ_X_LIVE)
        assert (
            auq_source.anchor_pane_agreement(
                CARD_B_TOOL_INPUT, pane, target_row=4, bind_question_text=False
            )
            == auq_source.ANCHOR_MATCH
        ), "labels alone ACCEPT the successor — this is the hole"
        assert (
            auq_source.anchor_pane_agreement(
                CARD_B_TOOL_INPUT, pane, target_row=4, bind_question_text=True
            )
            == auq_source.ANCHOR_MISMATCH
        ), "the question text is what separates them"

    @pytest.mark.asyncio
    async def test_an_anchor_whose_LABELS_disagree_with_the_pane_declines(
        self, monkeypatch, auq_anchor, stamped
    ):
        """The easier half of the same hole: a record for a different question with
        different options. Caught at plan time, before a keystroke."""
        auq_anchor[0] = _anchor(
            "auq:sid:SESSION_A:tu:toolu_PETS",
            {
                "questions": [
                    {
                        "question": "Pick a pet",
                        "options": [
                            {"label": "Cat"},
                            {"label": "Dog"},
                            {"label": "Bird"},
                        ],
                    }
                ]
            },
        )
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        assert await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP) is None
        assert pane.keys == []
        assert stamped == []

    @pytest.mark.asyncio
    async def test_an_anchor_that_MOVES_across_one_observation_declines(
        self, monkeypatch, auq_anchor, stamped
    ):
        """THE SANDWICH. The side file is re-read AFTER the capture and must be
        UNCHANGED. A hook write landing inside an observation means the pane cannot
        be attributed to either record — so it is DETECTED, not reasoned about."""
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        real_capture = pane.capture

        async def rotate_inside_the_first_capture(window_id, *, with_ansi=False):
            out = await real_capture(window_id, with_ansi=with_ansi)
            # The successor's hook fires while our capture is in flight.
            auq_anchor[0] = _anchor("auq:sid:SESSION_A:tu:toolu_CARD_X2")
            return out

        monkeypatch.setattr(
            tmux_mod.tmux_manager,
            "capture_pane_cancellation_safe",
            rotate_inside_the_first_capture,
        )

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is None
        assert pane.keys == [], "the observation was torn — not one keystroke"
        assert stamped == []

    @pytest.mark.asyncio
    async def test_a_RESOLVED_card_still_rendered_is_never_planned_against(
        self, monkeypatch, auq_anchor, stamped
    ):
        """THE MISSING PREMISE, made an explicit REQUIREMENT — and honest about
        being defence in depth.

        The reviewer's step 3 — "the pane still holds A's complete picker" — is
        only reachable for a card that has ALREADY been answered, and a live
        blocking prompt REPLACES the input box while a resolved one RESTORES it.
        The lane therefore now demands that the card OWN the pane.

        WHAT THIS LEG ACTUALLY BUYS. It is NOT the only thing standing here:
        MEASURED, this same pane already declines without it, because appending a
        restored input box makes ``parse_ask_user_question`` report
        ``is_free_text=False`` (the affordance row stops being detected) and
        ``_auq_shape`` bails. And on the REAL 2.1.207 captures a resolved AUQ
        collapses entirely — the four ``auq_after_answer_t*`` frames do not even
        extract as AskUserQuestion. Both of those are properties of TODAY's TUI
        and TODAY's parser, i.e. accidents. This leg turns the premise the round-3
        argument silently relied on into a cheap, STATED requirement, so a TUI
        drift that leaves a resolved picker looking free-text-capable cannot
        quietly re-open the chimera.
        """
        resolved_but_rendered = AUQ_X_LIVE + "\n" + _fx(f"inputbox_idle_{V}.txt")

        # The reviewer's step-3 shape: the picker is still RENDERED (it extracts
        # as AskUserQuestion) but the card no longer OWNS the pane.
        content = terminal_parser.extract_interactive_content(
            plain(resolved_but_rendered)
        )
        assert content is not None and content.name == "AskUserQuestion"
        assert terminal_parser.pane_input_box_present(plain(resolved_but_rendered))

        pane = FakePane(
            [resolved_but_rendered, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_RESOLVED]
        )
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is None
        assert pane.keys == [], "the card does not own the pane — nothing is typed"
        assert stamped == []

    @pytest.mark.asyncio
    async def test_the_happy_path_is_unaffected_by_the_binding(
        self, monkeypatch, stamped
    ):
        """The non-regression that matters: a GENUINE record for the card on the
        pane still commits — including the 947-char multi-line answer, whose typed
        row parses as a FOURTH option (hence the target-row-blind truncation)."""
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED_BIG, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, BIG_ANSWER, user_turn=STAMP)

        assert result is not None and result.ok, result
        assert pane.enter_sent is True
        assert stamped == [(1, 42, WINDOW)]


class TestRawControlBytesAreNeverTyped:
    """peer-review round-5 P1-A — ``send-keys -l`` is not a sanitizer.

    ``-l`` stops tmux interpreting KEY NAMES. It does NOT make C0/ESC bytes inert
    to the TUI: RIG-CONFIRMED (``tmux -L ccrig``, ``cat -v`` in the pane) that a
    payload built with ``printf 'A\\033[B2B'`` lands as the literal bytes
    ``A^[[B2B`` — Claude's TUI sees ``A``, a CURSOR-DOWN escape sequence, then
    ``2``. On a single-select-shaped surface that digit is a HOTKEY that COMMITS
    with no Enter — fired DURING the write, before anything re-observes the pane.
    """

    def test_the_byte_set(self):
        assert delivery.unsafe_control_char("\x1b[B2") == "\x1b"  # ESC
        assert delivery.unsafe_control_char("a\tb") == "\t"  # TAB — a live TUI key
        assert delivery.unsafe_control_char("a\rb") == "\r"  # CR — Enter at the pty
        assert delivery.unsafe_control_char("a\x00b") == "\x00"
        assert delivery.unsafe_control_char("a\x7fb") == "\x7f"  # DEL
        assert delivery.unsafe_control_char("a\x9bb") == "\x9b"  # C1 CSI
        # LF is ALLOWED and load-bearing: a paste-shaped multi-line payload is the
        # lane's primary flow (a voice note with a reply-context quote).
        assert delivery.unsafe_control_char("line one\nline two\n") is None
        assert delivery.unsafe_control_char(BIG_ANSWER) is None

    @pytest.mark.asyncio
    async def test_the_lane_declines_an_escape_sequence_with_zero_keystrokes(
        self, monkeypatch, stamped
    ):
        """The free-text lane DECLINES (PR-1's step 0b owns the single refusal —
        one rule, one owner, never two ❌)."""
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(
            WINDOW, "my answer is\x1b[B2 teal", user_turn=STAMP
        )

        assert result is None
        assert pane.keys == [], "an escape sequence must never reach a live card"
        assert stamped == []

    def test_the_refusal_copy_is_actionable(self):
        assert delivery.REASON_CONTROL_CHARS in delivery.REFUSAL_COPY
        assert delivery.REASON_CONTROL_CHARS in delivery.DELIVERY_REFUSAL_REASONS


class TestArrowKeysAreNotADraft:
    """peer-review round-5 P2 — the brake is about a stranded DRAFT, not any key.

    A post-nav bail has moved the cursor and typed NOTHING. Arming the
    stranded-draft brake there would be actively harmful: while a card owns the
    pane there is no input row, so ``pane_input_row_empty`` is INDETERMINATE and
    the brake's only release proof can never fire — the topic would be WEDGED
    (every later message refused with "an earlier message is still sitting UNSENT
    in this window's input box", which would be a lie) until the window is killed.

    The real protection is PR-1: the card is still live, so the fall-through
    ``deliver_to_window`` REFUSES the next payload at its input-box gate. There is
    no append-and-commit chain to break, because there is nothing to append TO.
    """

    @pytest.mark.asyncio
    async def test_a_post_nav_bail_leaves_no_draft_no_brake_and_PR1_still_refuses(
        self, monkeypatch, auq_anchor, stamped
    ):
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LIVE, AUQ_X_LIVE])  # the nav never lands
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is None
        assert pane.arrows == ["Down", "Down", "Down"], "arrows WERE sent"
        assert pane.literal_writes == [], "…but nothing was typed — no draft exists"
        assert stamped == []
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is False

        # …and the fall-through is PR-1, which refuses on the still-live card. This
        # is the AUTHORITY ``deliver_to_window`` consults, so the next message
        # cannot be typed onto the pane the arrows touched.
        assert terminal_parser.classify_input_box_failure(plain(AUQ_X_LIVE)) is not None


# ── peer-review round 6 ───────────────────────────────────────────────────

# THE ROUND-6 P1. The question binding used to be a WHOLE-PANE substring search
# over the whitespace-SQUASHED pane, so the record's question only had to occur
# SOMEWHERE — inside an option label, an option description, the user's own
# prompt in the scrollback, even the picker's footer. Whitespace removal made it
# worse: it destroys line and token boundaries, so the match was far wider than
# equality.
#
# Card BLUE is the reviewer's reduction: its question IS one of card X's option
# labels, and its option labels are card X's labels. Every leg of the agreement
# then passed on card X's pane, so the executor typed the user's answer to
# "What's your favorite color?" and committed it onto card BLUE.
CARD_BLUE_TOOL_INPUT = {
    "questions": [
        {
            "question": "Blue",
            "header": "Color",
            "options": [{"label": "Blue"}, {"label": "Green"}, {"label": "Red"}],
            "multiSelect": False,
        }
    ]
}

# The question row as Claude Code renders it on card X (bold, column 0).
_Q_ROW = "\x1b[1m\x1b[38;5;231mWhat's your favorite color?\x1b[0m"

# A LONG question, and the physical rows CC soft-wraps it into. Joining the rows
# with a single space reproduces the record's question EXACTLY — which is what a
# word-wrap does, and what the binding must tolerate.
WRAPPED_Q = (
    "Which of these three colors would you pick for the primary accent of the "
    "new dashboard theme, given that it has to stay legible on both the light "
    "and the dark background?"
)
WRAPPED_ROWS = [
    "Which of these three colors would you pick for the primary accent of the",
    "new dashboard theme, given that it has to stay legible on both the light",
    "and the dark background?",
]

# The frame's WRAP COLUMN, measured from the frame itself (160 — the picker's own
# full-width ``────`` box rules), exactly as ``pane_wrap_column`` measures it.
WRAP = max(len(line) for line in plain(AUQ_X_LIVE).split("\n"))

# A question carrying a token LONGER than the wrap column: CC HARD-BREAKS it, so
# the rows do NOT rejoin on a space boundary. The binding tolerates that — but
# only because the broken row is FULL, which is the geometry that proves a break
# is even possible. (Round 7: the rows used to be 55 chars wide, i.e. a hard
# break the pane could never have rendered; the old whitespace-SQUASH matched
# them anyway, which is exactly the hole that got closed.)
_HB_HEAD = "Which color suits the URL https://example.com/"
HARD_BROKEN_Q = f"{_HB_HEAD}{'a' * 200} best?"
HARD_BROKEN_ROWS = [HARD_BROKEN_Q[:WRAP], HARD_BROKEN_Q[WRAP:]]


def _rewrap_question(ansi: str, rows: list[str]) -> str:
    """Re-render card X's question as the physical ROWS CC would wrap it into.

    Mechanical and byte-level, exactly like ``free_text_frames``' derivations: CC
    renders the question at column 0 and soft-wraps it across physical rows, and
    tmux's capture emits one line per row (it does not re-indent continuations).
    Nothing else on the frame is touched.
    """
    assert _Q_ROW in ansi
    return ansi.replace(
        _Q_ROW, "\n".join(f"\x1b[1m\x1b[38;5;231m{r}\x1b[0m" for r in rows)
    )


def _tool_input(question: str) -> dict:
    return {
        "questions": [
            {
                "question": question,
                "header": "Color",
                "options": [{"label": "Blue"}, {"label": "Green"}, {"label": "Red"}],
                "multiSelect": False,
            }
        ]
    }


class TestTheQuestionBindingTargetsThePanesQUESTION:
    """round-6 P1 — the binding must name the pane's QUESTION, not its CONTENTS.

    The pane's question is the block ADJACENT to the live option block. A match
    anywhere else — an option label, an option description, prose in the
    scrollback, the picker footer — is not evidence that the record describes
    this card, and card BLUE proves it: its question is card X's option label, so
    the whole-pane substring search accepted it and the answer was committed onto
    the WRONG CARD.

    SCOPE: this tightening lives in the FREE-TEXT ANCHOR path only. The shared
    ``_record_consistent_with_pane`` (render decisions, ``aqp:`` dispatch, the
    source-drift re-mint, the GH #48 recap) never had a question leg and is
    byte-untouched — see ``test_the_SHARED_predicate_is_byte_untouched``.
    """

    @pytest.mark.asyncio
    async def test_a_successor_whose_question_is_an_OPTION_LABEL_never_gets_the_answer(
        self, monkeypatch, auq_anchor, stamped
    ):
        """RED before the fix: ``pane.enter_sent`` is True and the result is
        DELIVERED — the user's answer to "What's your favorite color?" is committed
        as the answer to the card whose question is "Blue"."""
        auq_anchor[0] = _anchor(
            "auq:sid:SESSION_A:tu:toolu_CARD_BLUE", CARD_BLUE_TOOL_INPUT
        )
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert pane.enter_sent is False, (
            "the answer was committed onto the WRONG CARD — the record's question "
            "only occurred inside an OPTION LABEL"
        )
        assert result is None, "declines BEFORE a keystroke; PR-1 owns the refusal"
        assert pane.keys == []
        assert stamped == []
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is False

    @pytest.mark.parametrize(
        "question",
        [
            pytest.param("Blue", id="an_option_label"),
            pytest.param(
                "Calm, cool, and the most commonly cited favorite color worldwide.",
                id="an_option_description",
            ),
            pytest.param(
                "ask me a single-select question with 3 options about my favorite "
                "color",
                id="the_users_own_prompt_in_the_scrollback",
            ),
            pytest.param("Enter to select", id="the_picker_footer"),
            pytest.param("Color", id="the_tab_header"),
            pytest.param(
                # ONLY the whitespace squash could ever have matched this: it
                # spans the question's TAIL and the first OPTION ROW, so it
                # exists only once the LINE boundary between them is destroyed.
                "favorite color? 1. Blue",
                id="a_span_across_a_destroyed_line_boundary",
            ),
        ],
    )
    def test_incidental_pane_text_never_satisfies_the_binding(self, question):
        assert (
            auq_source.anchor_pane_agreement(
                _tool_input(question),
                plain(AUQ_X_LIVE),
                target_row=4,
                bind_question_text=True,
            )
            == auq_source.ANCHOR_MISMATCH
        )

    def test_the_GENUINE_question_still_matches(self):
        assert (
            auq_source.anchor_pane_agreement(
                CARD_X_TOOL_INPUT,
                plain(AUQ_X_LIVE),
                target_row=4,
                bind_question_text=True,
            )
            == auq_source.ANCHOR_MATCH
        )

    def test_a_SOFT_WRAPPED_question_still_matches(self):
        """The non-regression that keeps the feature alive: a long question wraps
        across physical rows, and the binding must rejoin them."""
        pane = plain(_rewrap_question(AUQ_X_LIVE, WRAPPED_ROWS))
        assert (
            auq_source.anchor_pane_agreement(
                _tool_input(WRAPPED_Q), pane, target_row=4, bind_question_text=True
            )
            == auq_source.ANCHOR_MATCH
        )

    def test_a_HARD_BROKEN_token_still_matches(self):
        """A token longer than the wrap column is broken MID-TOKEN, so the rows do
        not rejoin on a space. The binding tolerates it — as an EQUALITY against
        the question region, never a substring of the pane."""
        pane = plain(_rewrap_question(AUQ_X_LIVE, HARD_BROKEN_ROWS))
        assert (
            auq_source.anchor_pane_agreement(
                _tool_input(HARD_BROKEN_Q), pane, target_row=4, bind_question_text=True
            )
            == auq_source.ANCHOR_MATCH
        )

    def test_a_wrapped_question_that_disagrees_still_REFUSES(self):
        """Wrap tolerance is not a licence: the region must still BE the question."""
        pane = plain(_rewrap_question(AUQ_X_LIVE, WRAPPED_ROWS))
        assert (
            auq_source.anchor_pane_agreement(
                _tool_input("Which color do you HATE?"),
                pane,
                target_row=4,
                bind_question_text=True,
            )
            == auq_source.ANCHOR_MISMATCH
        )

    @pytest.mark.asyncio
    async def test_a_wrapped_question_still_COMMITS_end_to_end(
        self, monkeypatch, auq_anchor, stamped
    ):
        """The false-refusal guard on the live call path: a long, wrapped question
        is the common case, and it must still deliver."""
        auq_anchor[0] = _anchor(
            "auq:sid:SESSION_A:tu:toolu_CARD_WRAPPED", _tool_input(WRAPPED_Q)
        )
        pane = FakePane(
            [
                _rewrap_question(AUQ_X_LIVE, WRAPPED_ROWS),
                _rewrap_question(AUQ_X_LANDED, WRAPPED_ROWS),
                _rewrap_question(AUQ_X_TYPED, WRAPPED_ROWS),
                AUQ_RESOLVED,
            ]
        )
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is not None and result.ok, result
        assert result.outcome is DeliveryOutcome.DELIVERED
        assert pane.enter_sent is True
        assert stamped == [(1, 42, WINDOW)]

    def test_the_SHARED_predicate_is_byte_untouched(self):
        """The scoping choice, pinned — with the RATIONALE CORRECTED (round-7 P3).

        ``_record_consistent_with_pane`` is consumed by the picker RENDER path, the
        ``aqp:`` dispatch's ``validate_and_consume``, the source-drift re-mint and
        the GH #48 recap identity. The round-6 text claimed those consumers "cannot
        commit a keystroke". **That is FALSE**: ``validate_and_consume`` calls
        ``resolve_auq_source`` and its caller goes on to the ``aqp:``
        navigate→Enter dispatch. What actually makes the scoping safe is that the
        ``aqp:`` lane is protected INDEPENDENTLY — it re-validates the EXACT minted
        form fingerprint and the minted SOURCE fingerprint against the live pane
        before any key, so a successor card whose question differs cannot be
        dispatched into regardless of this predicate (Codex looked for an exploit
        there and found none).

        So the decision STANDS on its own merits: the predicate never had a
        question leg, and tightening it would flip render decisions
        (``side_file_ok`` → ``bail``/``rescue``), dropping the AUQ context card or
        suppressing pick buttons on real cards — a real cost, bought for a lane
        that already has its own proof. The question binding therefore stays in the
        ANCHOR path, where the wrong-card COMMIT hazard actually lives.
        """
        pane_form = terminal_parser.parse_ask_user_question(plain(AUQ_X_LIVE))
        assert pane_form is not None
        record = auq_source.PreToolAskRecord(
            tool_input=CARD_BLUE_TOOL_INPUT,
            session_id="",
            tool_use_id="",
            written_at=0.0,
            input_fingerprint="",
        )
        assert auq_source._record_consistent_with_pane(record, pane_form) == (
            True,
            "ok",
        ), "the SHARED predicate accepts it exactly as before — labels agree"

        # …and the anchor lane, which is the one that can commit a keystroke,
        # refuses the very same record.
        assert (
            auq_source.anchor_pane_agreement(
                CARD_BLUE_TOOL_INPUT,
                plain(AUQ_X_LIVE),
                target_row=4,
                bind_question_text=True,
            )
            == auq_source.ANCHOR_MISMATCH
        )


# ── peer-review round 7 ───────────────────────────────────────────────────

# THE ROUND-7 P1-1. Round 6 targeted the question binding at the pane's QUESTION
# REGION — but kept a fallback: "if the space-join fails, compare both sides with
# ALL whitespace removed", justified as safe "because it is an equality against a
# tight region". Equality does not make a lossy transformation injective, and
# Codex demolished it in one counterexample: the pane asks "Is nowhere safe?",
# the successor record asks "Is now here safe?" — two DIFFERENT questions, and
# with the SAME option labels the squash makes them EQUAL, so the successor's
# anchor binds to the live card and the answer commits onto the WRONG CARD.
NOWHERE_ROWS = ["Is nowhere safe?"]
NOWHERE_Q = "Is nowhere safe?"
NOW_HERE_Q = "Is now here safe?"

# The FALSIFIED PREMISE, pinned. "A row that ends exactly at the wrap column was
# hard-broken mid-token" is FALSE for Claude Code: in the real 160-column capture
# ``auq_longlabel_160x50_v2.1.198`` the question's rows 2 and 4 are EXACTLY 160
# characters and both end at a WORD boundary ("…peer reviewers," / "…choice
# trades"). So the geometry may only VETO the hard-break reading of a boundary —
# never force it. A join rule that assumed otherwise would glue "reviewers,"
# onto "but" and false-refuse every long question in the corpus.
_EXACT_LEAD = "Which colour should the primary accent be"
EXACT_COL_ROWS = [
    f"{_EXACT_LEAD} {'x' * (WRAP - len(_EXACT_LEAD) - 1)}",
    "and nothing?",
]
EXACT_COL_Q = " ".join(EXACT_COL_ROWS)
# Its pane-indistinguishable twin: the pane renders these two questions
# IDENTICALLY (a word wrap consumes the space; a hard break has none), so the
# walk accepts both. Disclosed residual, not a hole the walk introduces.
EXACT_COL_TWIN_Q = "".join(EXACT_COL_ROWS)

# THE ROUND-7 P1-2. ``auq_question_region`` stopped collecting at 12 rows and
# RETURNED the partial region — a strictly WEAKER, SUFFIX identity — while its
# comment claimed truncation failed closed. Codex reproduced the consequence with
# a parseable 13-row question: the region came back as rows 2..13, and a
# successor record whose ENTIRE question equals that 12-row suffix bound to the
# pane and committed onto the WRONG CARD.
THIRTEEN_ROWS = [
    f"Row {i} of a question that will not fit inside the cap" for i in range(1, 14)
]
THIRTEEN_Q = " ".join(THIRTEEN_ROWS)
TWELVE_ROW_SUFFIX_Q = " ".join(THIRTEEN_ROWS[1:])


def _agreement(question: str, rows: list[str]) -> str:
    return auq_source.anchor_pane_agreement(
        _tool_input(question),
        plain(_rewrap_question(AUQ_X_LIVE, rows)),
        target_row=4,
        bind_question_text=True,
    )


class TestTheQuestionBindingIsInjective:
    """Round-7 P1-1: the whitespace-removed fallback equated DIFFERENT questions."""

    def test_a_space_moved_INSIDE_a_word_never_binds(self):
        """Codex's exact case — RED before the fix.

        ``_squash_ws("Is nowhere safe?") == _squash_ws("Is now here safe?")``, so
        the round-6 fallback returned MATCH and the free-text executor committed
        the user's answer onto the successor card.
        """
        assert _agreement(NOW_HERE_Q, NOWHERE_ROWS) == auq_source.ANCHOR_MISMATCH
        # …and the pane's OWN question still binds, so this is not a blanket
        # refusal of everything that region can hold.
        assert _agreement(NOWHERE_Q, NOWHERE_ROWS) == auq_source.ANCHOR_MATCH

    @pytest.mark.asyncio
    async def test_the_successor_card_is_never_ANSWERED(
        self, monkeypatch, auq_anchor, stamped
    ):
        """The same case on the LIVE call path: nothing is typed, nothing commits."""
        auq_anchor[0] = _anchor(
            "auq:sid:SESSION_A:tu:toolu_CARD_NOW_HERE", _tool_input(NOW_HERE_Q)
        )
        live = _rewrap_question(AUQ_X_LIVE, NOWHERE_ROWS)
        pane = FakePane([live, live, live, live])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is None, "the lane must DECLINE and fall through to PR-1"
        assert pane.enter_sent is False
        assert pane.literal_writes == []
        assert stamped == []
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is False

    def test_a_row_AT_the_wrap_column_may_still_be_a_WORD_wrap(self):
        """The falsified premise. Fixture-pinned on the real 160-column capture.

        A join rule that read "row length == wrap column" as "hard-broken
        mid-token" would reconstruct ``…xxand nothing?`` and refuse the genuine
        question — killing the lane for the most common long-question shape.
        """
        assert len(EXACT_COL_ROWS[0]) == WRAP
        assert _agreement(EXACT_COL_Q, EXACT_COL_ROWS) == auq_source.ANCHOR_MATCH

    def test_the_exact_column_boundary_is_a_DISCLOSED_residual(self):
        """The pane renders both of these questions with the SAME rows, so no
        reading of the pane can separate them (re-wrapping the record's question
        to the same width produces the same rows too). Accepted and documented —
        the same class as "same question + same labels"."""
        assert _agreement(EXACT_COL_TWIN_Q, EXACT_COL_ROWS) == auq_source.ANCHOR_MATCH

    def test_a_hard_break_is_only_credible_on_a_FULL_row(self):
        """The geometry VETO. A missing space at a SHORT row boundary is not a
        wrap artefact — it is a different question, and it refuses."""
        short = ["Which colour", "should we use?"]
        assert len(short[0]) < WRAP
        assert _agreement("Which colourshould we use?", short) == (
            auq_source.ANCHOR_MISMATCH
        )
        assert _agreement("Which colour should we use?", short) == (
            auq_source.ANCHOR_MATCH
        )


class TestTheRegionCapFailsClosed:
    """Round-7 P1-2: the cap returned a truncated SUFFIX, a weaker identity."""

    def test_an_over_long_region_is_None_not_a_suffix(self):
        pane = plain(_rewrap_question(AUQ_X_LIVE, THIRTEEN_ROWS))
        assert terminal_parser.auq_question_region(pane) is None

    def test_the_twelve_row_SUFFIX_never_binds(self):
        """RED before the fix: the region came back as the 12-row suffix, a
        successor record whose whole question IS that suffix compared EQUAL, and
        the answer committed onto the wrong card."""
        assert _agreement(TWELVE_ROW_SUFFIX_Q, THIRTEEN_ROWS) == (
            auq_source.ANCHOR_MISMATCH
        )

    def test_the_GENUINE_over_long_question_also_refuses(self):
        """Fail-closed, and disclosed: a question past the cap cannot be bound at
        all, so the lane declines and PR-1 refuses the message (a false refusal,
        never a wrong-card commit). The largest REAL capture is 9 rows."""
        assert _agreement(THIRTEEN_Q, THIRTEEN_ROWS) == auq_source.ANCHOR_MISMATCH

    def test_a_region_that_ENDS_at_the_cap_is_complete(self):
        """The cap is a boundary, not an off-by-one: exactly 12 rows still bind."""
        rows = THIRTEEN_ROWS[:12]
        assert _agreement(" ".join(rows), rows) == auq_source.ANCHOR_MATCH
