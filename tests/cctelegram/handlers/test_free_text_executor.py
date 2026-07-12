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
"""

from __future__ import annotations

import asyncio
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
    EPM_OVERFLOW,
    EPM_P_ANSWER,
    EPM_P_LANDED,
    EPM_P_LIVE,
    EPM_P_PLAN_PATH,
    EPM_P_TYPED,
    EPM_Q_PLAN_PATH,
    EPM_Q_TYPED,
    EPM_Q_TYPED_AT_P_PATH,
    EPM_RESOLVED,
    EPM_S_PLAN_PATH,
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


@pytest.fixture(autouse=True)
def auq_anchor(monkeypatch):
    """The AUQ out-of-band anchor (the PreToolUse side file's occurrence id).

    AUTOUSE, because the anchor is MANDATORY (peer-review round-2 P1): without a
    live side file the AUQ lane DECLINES, so every AUQ test needs one — and the
    tests that assert the decline set it to ``None`` themselves.

    A mutable one-element list, so a test can ROTATE the anchor mid-flight —
    which is what a genuinely new AUQ does (its hook rewrites the side file) — or
    drop it to ``None`` (the card resolved and its file was unlinked).
    """
    box: list[str | None] = ["tu:toolu_CARD_X"]
    monkeypatch.setattr(
        auq_source, "peek_surface_identity_for_window", lambda _w: box[0]
    )
    return box


# The plan bodies behind the corpus's real EPM footers. The EPM anchor is the
# plan file's CONTENT (hashed), not its path, so these files must EXIST — and a
# test that revises a plan simply rewrites one of them.
EPM_PLAN_BODIES = {
    EPM_P_PLAN_PATH: "# Plan P\n\nWrite goodbye.txt with one paragraph.\n",
    EPM_Q_PLAN_PATH: "# Plan Q\n\n1. Create goodbye.txt\n2. Verify it\n",
    EPM_S_PLAN_PATH: "# Plan S\n\nA very short warm plan.\n",
}


@pytest.fixture(autouse=True)
def epm_plans(monkeypatch, tmp_path):
    """The real filesystem substrate the EPM anchor reads: ``~/.claude/plans/``.

    HOME is redirected, so ``Path.home()`` and ``Path("~/…").expanduser()`` — the
    exact seams ``free_text._epm_plan_generation`` uses, traversal guard included
    — resolve here. No handler internals are stubbed.

    Returns the plans DIRECTORY so a test can REVISE a plan in place (the same
    slug, new bytes: the round-2 P1 shape).
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    plans = tmp_path / ".claude" / "plans"
    plans.mkdir(parents=True)
    for path, body in EPM_PLAN_BODIES.items():
        (plans / Path(path).name).write_text(body)
    return plans


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
    async def test_auq_replaced_by_an_epm_before_the_enter(self, monkeypatch, stamped):
        """AUQ → ExitPlanMode. The worst available outcome: EPM's option 1 is
        "Yes, and bypass permissions"."""
        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, EPM_P_TYPED, EPM_P_TYPED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, AUQ_X_ANSWER, user_turn=STAMP)

        assert result is not None
        assert result.outcome is DeliveryOutcome.DRAFT_WRITTEN
        assert pane.enter_sent is False
        assert stamped == []

    @pytest.mark.asyncio
    async def test_epm_replaced_by_an_auq_before_the_enter(self, monkeypatch, stamped):
        pane = FakePane([EPM_P_LIVE, EPM_P_LANDED, AUQ_X_TYPED, AUQ_X_TYPED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, EPM_P_ANSWER, user_turn=STAMP)

        assert result is not None
        assert result.outcome is DeliveryOutcome.DRAFT_WRITTEN
        assert pane.enter_sent is False
        assert stamped == []

    @pytest.mark.asyncio
    async def test_epm_replaced_by_a_DIFFERENT_PLAN_before_the_enter(
        self, monkeypatch, stamped
    ):
        """The hardest one. EVERY ExitPlanMode prompt renders the SAME three real
        options, so the pane-derived identity CANNOT tell plan P from plan Q — the
        ``~/.claude/plans/<slug>.md`` footer is the only discriminator, which is
        precisely why the EPM anchor is mandatory. Both frames are real rig bytes.
        """
        assert terminal_parser.free_text_surface_identity(
            plain(EPM_P_TYPED), surface="ExitPlanMode", target_row=4
        ) == terminal_parser.free_text_surface_identity(
            plain(EPM_Q_TYPED), surface="ExitPlanMode", target_row=4
        ), "premise: two EPM plans are INDISTINGUISHABLE by their option rows"

        pane = FakePane([EPM_P_LIVE, EPM_P_LANDED, EPM_Q_TYPED, EPM_Q_TYPED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, EPM_P_ANSWER, user_turn=STAMP)

        assert result is not None
        assert result.outcome is DeliveryOutcome.DRAFT_WRITTEN
        assert pane.enter_sent is False, "plan Q must not receive plan P's feedback"
        assert stamped == []

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
            if calls["n"] >= 3:  # capture 1 = plan, 2 = landing, 3 = pre-Enter
                auq_anchor[0] = "tu:toolu_CARD_X_REASKED"  # a NEW AUQ, same content
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
                plain(AUQ_X_OVERFLOW), surface="AskUserQuestion", target_row=4
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
            plain(AUQ_X_LIVE), surface="AskUserQuestion", target_row=4
        ) == terminal_parser.free_text_surface_identity(
            plain(AUQ_X_TYPED), surface="AskUserQuestion", target_row=4
        ), "premise: the two occurrences are INDISTINGUISHABLE by their option rows"

        pane = FakePane([AUQ_X_LIVE, AUQ_X_LANDED, AUQ_X_TYPED, AUQ_X_TYPED])
        _wire(monkeypatch, pane)

        calls = {"n": 0}
        real_capture = pane.capture

        async def capture_and_reask(window_id, *, with_ansi=False):
            out = await real_capture(window_id, with_ansi=with_ansi)
            calls["n"] += 1
            if calls["n"] >= 3:  # 1 = plan, 2 = landing, 3 = the pre-Enter capture
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


class TestEpm:
    @pytest.mark.asyncio
    async def test_feedback_commits_and_reports_as_plan_feedback(
        self, monkeypatch, stamped
    ):
        pane = FakePane([EPM_P_LIVE, EPM_P_LANDED, EPM_P_TYPED, EPM_RESOLVED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(
            WINDOW, EPM_P_ANSWER, user_turn=STAMP, display="proj"
        )

        assert result is not None and result.ok, result
        assert "plan feedback" in result.message
        assert pane.arrows == ["Down", "Down", "Down"]  # row 1 → row 4
        assert pane.enter_sent is True
        assert stamped == [(1, 42, WINDOW)]

    @pytest.mark.asyncio
    async def test_a_numbered_plan_BODY_no_longer_kills_the_lane(self):
        """Most plans render a numbered list of steps, and that list used to hijack
        the top-down option walk so the option block was never reached — the EPM
        lane silently declined on the common shape. The block is now delimited by
        the prompt's own ``UIPattern`` anchors."""
        assert "1. Create goodbye.txt" in plain(EPM_Q_TYPED)  # a numbered plan body
        assert (
            terminal_parser.free_text_surface_identity(
                plain(EPM_Q_TYPED), surface="ExitPlanMode", target_row=4
            )
            is not None
        )

    @pytest.mark.asyncio
    async def test_epm_overflow_REFUSES_rather_than_guess(self, monkeypatch, stamped):
        """The EPM prompt grows DOWNWARD, so a long draft pushes its FOOTER off the
        bottom — and the footer carries the ``~/.claude/plans/<slug>.md`` path that
        is the ONLY thing distinguishing one plan from another. With it gone the
        card is unidentifiable, so the feedback is NOT committed. Option 1 is "Yes,
        and bypass permissions"; there is no acceptable guess here."""
        assert terminal_parser.extract_epm_plan_file_path(plain(EPM_OVERFLOW)) is None
        pane = FakePane([EPM_P_LIVE, EPM_P_LANDED, EPM_OVERFLOW, EPM_OVERFLOW])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, EPM_P_ANSWER, user_turn=STAMP)

        assert result is not None
        assert result.outcome is DeliveryOutcome.DRAFT_WRITTEN
        assert pane.enter_sent is False
        assert stamped == []
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is True

    @pytest.mark.asyncio
    async def test_an_epm_with_no_visible_plan_footer_never_starts(self, monkeypatch):
        """The anchor is MANDATORY at plan time too — no footer, no lane. A clean
        pre-write decline (PR-1 refuses), never a typed draft."""
        pane = FakePane([EPM_OVERFLOW])
        _wire(monkeypatch, pane)

        assert await free_text.try_answer(WINDOW, PAYLOAD, user_turn=STAMP) is None
        assert pane.keys == []

    @pytest.mark.asyncio
    async def test_an_epm_whose_plan_FILE_is_gone_never_starts(
        self, monkeypatch, epm_plans
    ):
        """The anchor is the plan's CONTENT. An unreadable plan file is not a
        licence to proceed on the pane alone — every EPM pane looks the same."""
        (epm_plans / Path(EPM_P_PLAN_PATH).name).unlink()
        pane = FakePane([EPM_P_LIVE, EPM_P_LANDED, EPM_P_TYPED, EPM_RESOLVED])
        _wire(monkeypatch, pane)

        assert await free_text.try_answer(WINDOW, EPM_P_ANSWER, user_turn=STAMP) is None
        assert pane.keys == []


class TestARevisedPlanKeepsTheSameSlug:
    """peer-review round-2 P1 — the EPM anchor was a PATH, not an OCCURRENCE.

    Re-entering ExitPlanMode after REVISING a plan commonly keeps the SAME slug
    (Claude rewrites the file in place), and every EPM prompt renders the SAME
    three real options. So plan P and its revision matched on BOTH identity
    components, and a mid-transaction successor received the previous plan's
    feedback — on the surface whose option 1 is "Yes, and bypass permissions".

    The anchor is now the plan-file's CONTENT. Different plan ⇒ different bytes ⇒
    different anchor ⇒ refuse.
    """

    @pytest.mark.asyncio
    async def test_the_pane_AND_the_path_both_match_across_the_two_plans(self):
        """The premise, stated as an assertion: neither pre-fix component sees it."""
        assert terminal_parser.free_text_surface_identity(
            plain(EPM_P_TYPED), surface="ExitPlanMode", target_row=4
        ) == terminal_parser.free_text_surface_identity(
            plain(EPM_Q_TYPED_AT_P_PATH), surface="ExitPlanMode", target_row=4
        ), "every EPM renders the same three real options"
        assert (
            terminal_parser.extract_epm_plan_file_path(plain(EPM_P_TYPED))
            == terminal_parser.extract_epm_plan_file_path(plain(EPM_Q_TYPED_AT_P_PATH))
            == EPM_P_PLAN_PATH
        ), "a revision keeps the slug — the path is a NAME, not an occurrence"

    @pytest.mark.asyncio
    async def test_the_revised_plan_does_NOT_receive_the_previous_plans_feedback(
        self, monkeypatch, epm_plans, stamped
    ):
        """Plan P is live; we navigate and type. Claude revises the plan IN PLACE
        (same file, new bytes) and re-presents it. By the pre-Enter capture the
        pane is the revision — same options, same footer path — and only the
        CONTENT generation refuses."""
        plan_file = epm_plans / Path(EPM_P_PLAN_PATH).name
        pane = FakePane(
            [EPM_P_LIVE, EPM_P_LANDED, EPM_Q_TYPED_AT_P_PATH, EPM_Q_TYPED_AT_P_PATH]
        )
        _wire(monkeypatch, pane)

        calls = {"n": 0}
        real_capture = pane.capture

        async def capture_and_revise(window_id, *, with_ansi=False):
            out = await real_capture(window_id, with_ansi=with_ansi)
            calls["n"] += 1
            if calls["n"] >= 3:  # 1 = plan, 2 = landing, 3 = the pre-Enter capture
                plan_file.write_text(
                    "# Plan P, REVISED\n\nA different plan entirely.\n"
                )
            return out

        monkeypatch.setattr(
            tmux_mod.tmux_manager, "capture_pane_cancellation_safe", capture_and_revise
        )

        result = await free_text.try_answer(WINDOW, EPM_P_ANSWER, user_turn=STAMP)

        assert result is not None
        assert result.outcome is DeliveryOutcome.DRAFT_WRITTEN
        assert result.reason == delivery.REASON_FREE_TEXT_VERIFY_FAILED
        assert pane.enter_sent is False, "the REVISED plan must not get P's feedback"
        assert stamped == []
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is True

    @pytest.mark.asyncio
    async def test_a_revision_during_the_NAV_is_caught_with_nothing_typed(
        self, monkeypatch, epm_plans, stamped
    ):
        """The identity is re-checked at BOTH points bracketing a keystroke, so a
        revision that lands during the NAV is a clean pre-write bail: fall through
        to PR-1, no draft, no brake."""
        plan_file = epm_plans / Path(EPM_P_PLAN_PATH).name
        pane = FakePane([EPM_P_LIVE, EPM_P_LANDED, EPM_P_TYPED, EPM_RESOLVED])
        _wire(monkeypatch, pane)

        calls = {"n": 0}
        real_capture = pane.capture

        async def capture_and_revise(window_id, *, with_ansi=False):
            out = await real_capture(window_id, with_ansi=with_ansi)
            calls["n"] += 1
            if calls["n"] >= 2:  # revised before the post-nav landing check
                plan_file.write_text(
                    "# Plan P, REVISED\n\nA different plan entirely.\n"
                )
            return out

        monkeypatch.setattr(
            tmux_mod.tmux_manager, "capture_pane_cancellation_safe", capture_and_revise
        )

        result = await free_text.try_answer(WINDOW, EPM_P_ANSWER, user_turn=STAMP)

        assert result is None, "pre-write bail ⇒ PR-1 owns the refusal"
        assert pane.literal_writes == []
        assert stamped == []
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is False

    @pytest.mark.asyncio
    async def test_an_UNCHANGED_plan_still_commits(self, monkeypatch, stamped):
        """The non-regression the content generation must not break: the ordinary
        flow re-reads the SAME unchanged plan file at every observation point, so
        the anchor is stable and the feedback commits."""
        pane = FakePane([EPM_P_LIVE, EPM_P_LANDED, EPM_P_TYPED, EPM_RESOLVED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, EPM_P_ANSWER, user_turn=STAMP)

        assert result is not None and result.ok, result
        assert pane.enter_sent is True
        assert stamped == [(1, 42, WINDOW)]


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
            plain(AUQ_X_LIVE), surface="AskUserQuestion", target_row=4
        )
        post = terminal_parser.free_text_surface_identity(
            plain(AUQ_X_LANDED), surface="AskUserQuestion", target_row=4
        )
        assert pre is not None and pre == post

    def test_typing_into_the_row_does_not_move_the_identity(self):
        """The load-bearing one. ``_parse_numbered_options`` DROPS an affordance
        row, so the instant our text lands in row 4 it stops being an affordance
        and parses as a FOURTH REAL OPTION — a naive form fingerprint moves. The
        identity is TARGET-ROW-BLIND, so it does not."""
        base = terminal_parser.free_text_surface_identity(
            plain(AUQ_X_LANDED), surface="AskUserQuestion", target_row=4
        )
        for typed in (AUQ_X_TYPED, AUQ_X_TYPED_BIG):
            assert (
                terminal_parser.free_text_surface_identity(
                    plain(typed), surface="AskUserQuestion", target_row=4
                )
                == base
            )

    def test_a_different_question_has_a_different_identity(self):
        assert terminal_parser.free_text_surface_identity(
            plain(AUQ_X_LANDED), surface="AskUserQuestion", target_row=4
        ) != terminal_parser.free_text_surface_identity(
            plain(AUQ_Y_LIVE), surface="AskUserQuestion", target_row=4
        )

    def test_an_incomplete_option_block_is_UNRECOVERABLE_not_weaker(self):
        """It must never silently degrade to a shorter, weaker prefix."""
        assert (
            terminal_parser.free_text_surface_identity(
                plain(AUQ_X_OVERFLOW), surface="AskUserQuestion", target_row=4
            )
            is None
        )
