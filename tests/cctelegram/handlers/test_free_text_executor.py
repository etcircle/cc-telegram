"""The GH #50 PR-2 free-text executor: nav → verify → type → verify → Enter.

Driven against a FAKE tmux whose captures are the REAL 2.1.207 rig fixtures, so
the state machine is exercised on the exact bytes Claude Code renders.

The two invariants every test here defends:

  1. **The lane is purely ADDITIVE.** Every bail BEFORE the first keystroke
     returns ``None`` and the caller falls through to PR-1's gate — so the lane
     can only turn a REFUSED message into a delivered ANSWER, never the reverse.
  2. **The commit boundary is strict.** Nothing typed ⇒ ``None``. Typed but not
     committed ⇒ ``DRAFT_WRITTEN`` + the stranded-draft brake. Enter sent but
     unproven ⇒ ``COMMIT_UNKNOWN``, reported honestly, NEVER auto-retried.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cctelegram import delivery, terminal_parser, tmux_manager as tmux_mod
from cctelegram.delivery import DeliveryOutcome, UserTurnStamp
from cctelegram.handlers import free_text

FIXTURES = Path(__file__).parent.parent / "fixtures"
V = "v2.1.207"


def _fx(name: str) -> str:
    return (FIXTURES / name).read_text()


WINDOW = "@0"
STAMP = UserTurnStamp(user_id=1, thread_id=42, window_id=WINDOW)
PAYLOAD = "I would prefer a deep teal, actually"
BIG_PAYLOAD = _fx(f"auq_freetext_row_typed_large_{V}.ansi.txt")  # only for a tail


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


# The four rig captures the AUQ happy path walks through, in order.
AUQ_LIVE = _fx(f"auq_single_picker_{V}.txt")  # cursor row 1
AUQ_LANDED = _fx(f"auq_freetext_row_selected_pretype_{V}.ansi.txt")  # cursor row 4, DIM
AUQ_TYPED = _fx(f"auq_freetext_row_typed_{V}.ansi.txt")  # PLAIN
AUQ_TYPED_BIG = _fx(f"auq_freetext_row_typed_large_{V}.ansi.txt")
AUQ_OVERFLOW = _fx(f"auq_freetext_overflow_{V}.txt")
AUQ_RESOLVED = _fx("auq_after_answer_t5_v2.1.207.txt")  # the surface is GONE

EPM_LIVE = _fx(f"gate_epm_{V}.txt")
EPM_LANDED = _fx(f"epm_freetext_row_selected_pretype_{V}.ansi.txt")
EPM_TYPED = _fx(f"epm_freetext_row_typed_{V}.ansi.txt")
EPM_RESOLVED = _fx("epm_after_approve_t5_v2.1.207.txt")

# The payloads whose text is actually IN those fixtures (so the tail probe and
# the authorship prefix check see the truth).
AUQ_TYPED_TEXT = "teal, actually"
EPM_TYPED_TEXT = "please name it farewell.txt instead"


class TestAuqHappyPath:
    @pytest.mark.asyncio
    async def test_navigates_types_verifies_and_commits(self, monkeypatch, stamped):
        pane = FakePane([AUQ_LIVE, AUQ_LANDED, AUQ_TYPED, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(
            WINDOW, AUQ_TYPED_TEXT, user_turn=STAMP, display="proj"
        )

        assert result is not None and result.ok
        assert result.outcome is DeliveryOutcome.DELIVERED
        # N = 3 real options ⇒ the free-text row is 4 ⇒ 3 Downs (cursor starts at 1).
        assert pane.arrows == ["Down", "Down", "Down"]
        assert pane.literal_writes == [AUQ_TYPED_TEXT]
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
        pane = FakePane([AUQ_LIVE, AUQ_LANDED, AUQ_LANDED, AUQ_LANDED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, AUQ_TYPED_TEXT, user_turn=STAMP)

        assert result is not None
        assert result.outcome is DeliveryOutcome.DRAFT_WRITTEN
        assert result.reason == delivery.REASON_FREE_TEXT_VERIFY_FAILED
        assert pane.enter_sent is False
        assert stamped == []  # a withheld Enter is NEVER stamped
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is True


class TestTheOwnersActualUseCase:
    """A LARGE (voice-note-shaped) free-text answer — the primary path."""

    @pytest.mark.asyncio
    async def test_947_char_multiline_payload_commits(self, monkeypatch, stamped):
        # The real 947-char, 9-line payload whose typed render IS the fixture.
        payload = (
            '> Re: "the picker card you posted a moment ago"\n>\n'
            "> Claude asked: What's your favorite color?\n\n"
            "OK so about the colour question, I have been thinking about this for "
            "a while and I want to give you the full reasoning rather than just "
            "picking one of the three options you offered, because none of them is "
            "quite right on its own.\n\n"
            "I would actually prefer a deep teal, somewhere between blue and green, "
            "because it reads well on both light and dark backgrounds and it does "
            "not fight with the orange accent we already use in the header. Blue on "
            "its own is too corporate and cold, green on its own reads too much "
            "like a success state, and red is completely out of the question for "
            "anything that is not an error.\n\n"
            "So please go with teal as the primary, keep the existing orange as the "
            "accent, and make sure the contrast ratio stays above four point five "
            "to one for body text. If teal is impossible for some reason, fall back "
            "to blue, but tell me why first.\n"
        )
        assert len(payload) > 800
        pane = FakePane([AUQ_LIVE, AUQ_LANDED, AUQ_TYPED_BIG, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, payload, user_turn=STAMP)

        assert result is not None and result.ok, result
        # ONE literal write — exactly what the bot's send_keys does, and what the
        # rig proved does NOT paste-collapse on an affordance row.
        assert pane.literal_writes == [payload]
        assert pane.enter_sent is True
        assert stamped == [(1, 42, WINDOW)]

    @pytest.mark.asyncio
    async def test_overflow_commits_on_the_footer_proof(self, monkeypatch, stamped):
        """A ~5.3 k answer scrolls the ``❯ 4.`` row off the pane entirely (a TUI
        has no scrollback). The footer's ``ctrl+g to edit`` still proves the
        free-text row is the ACTIVE row, so the answer commits instead of being
        stranded inside a live card."""
        # The overflow fixture's draft tail — the "our bytes landed" probe.
        payload = (
            "Paragraph six. I want to walk you through the reasoning in detail "
            "because the short answer is misleading and I would rather you "
            "understand the constraints than guess at them from a single word. The "
            "palette has to work on light and dark, it has to survive being printed "
            "in greyscale, and it has to keep a contrast ratio above four point five "
            "to one for body text everywhere it is used, including the small print "
            "in the footer."
        )
        pane = FakePane([AUQ_LIVE, AUQ_LANDED, AUQ_OVERFLOW, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, payload, user_turn=STAMP)

        assert result is not None and result.ok, result
        assert pane.enter_sent is True
        assert terminal_parser.parse_free_text_row(AUQ_OVERFLOW, number=4) is None


class TestEpm:
    @pytest.mark.asyncio
    async def test_feedback_commits_and_reports_as_plan_feedback(
        self, monkeypatch, stamped
    ):
        pane = FakePane([EPM_LIVE, EPM_LANDED, EPM_TYPED, EPM_RESOLVED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(
            WINDOW, EPM_TYPED_TEXT, user_turn=STAMP, display="proj"
        )

        assert result is not None and result.ok
        assert "plan feedback" in result.message
        assert pane.arrows == ["Down", "Down", "Down"]  # row 1 → row 4
        assert pane.enter_sent is True
        assert stamped == [(1, 42, WINDOW)]

    @pytest.mark.asyncio
    async def test_epm_overflow_REFUSES_rather_than_guess(self, monkeypatch, stamped):
        """EPM has NO row-active footer proof (its ``ctrl+g`` is unconditional), so
        an EPM pane whose free-text row is not observable must NOT be committed —
        option 1 is "Yes, and bypass permissions". Fail-closed: the draft is
        stranded and honestly reported, never committed on a guess."""
        # A live EPM whose row-4 is simply absent from the post-type capture.
        stripped = "\n".join(
            ln for ln in EPM_TYPED.split("\n") if "4." not in ln and "❯" not in ln
        )
        pane = FakePane([EPM_LIVE, EPM_LANDED, stripped, stripped])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, EPM_TYPED_TEXT, user_turn=STAMP)

        assert result is not None
        assert result.outcome is DeliveryOutcome.DRAFT_WRITTEN
        assert pane.enter_sent is False
        assert stamped == []
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is True


class TestTheAdditiveInvariant:
    """Every pre-write bail returns None ⇒ the caller falls through to PR-1."""

    @pytest.mark.asyncio
    async def test_flag_off_declines(self, monkeypatch):
        free_text.set_enabled(False)
        pane = FakePane([AUQ_LIVE])
        _wire(monkeypatch, pane)
        assert await free_text.try_answer(WINDOW, PAYLOAD, user_turn=STAMP) is None
        assert pane.keys == []

    @pytest.mark.asyncio
    async def test_unlicensed_cc_version_declines(self, monkeypatch):
        pane = FakePane([AUQ_LIVE, AUQ_LANDED], cc_version="2.1.208")
        _wire(monkeypatch, pane)
        assert await free_text.try_answer(WINDOW, PAYLOAD, user_turn=STAMP) is None
        assert pane.keys == [], "an unlicensed version must never send a keystroke"

    @pytest.mark.asyncio
    async def test_lone_digit_payload_declines_without_a_capture(self, monkeypatch):
        """A bare digit is a live HOTKEY on these surfaces — it must never be
        typed. Falling through is correct AND sufficient: PR-1's step 0 applies
        the SAME rule and owns the refusal, so the user gets exactly one message."""
        pane = FakePane([AUQ_LIVE])
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
        pane = FakePane([AUQ_LIVE], cc_version="zsh")
        _wire(monkeypatch, pane)
        assert await free_text.try_answer(WINDOW, PAYLOAD, user_turn=STAMP) is None
        assert pane.keys == []

    @pytest.mark.asyncio
    async def test_landing_unproven_declines_with_NOTHING_typed(self, monkeypatch):
        """The nav didn't land (the post-nav capture still shows the cursor on
        option 1). Nothing was typed ⇒ fall through, clean."""
        pane = FakePane([AUQ_LIVE, AUQ_LIVE, AUQ_LIVE])
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
        pane = FakePane([AUQ_LIVE, AUQ_LANDED, AUQ_TYPED, AUQ_TYPED])
        pane.enter_ok = False
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, AUQ_TYPED_TEXT, user_turn=STAMP)

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
        pane = FakePane([AUQ_LIVE, AUQ_LANDED, AUQ_TYPED, AUQ_TYPED])
        _wire(monkeypatch, pane)

        result = await free_text.try_answer(WINDOW, AUQ_TYPED_TEXT, user_turn=STAMP)

        assert result is not None
        assert result.outcome is DeliveryOutcome.COMMIT_UNKNOWN
        assert result.reason == delivery.REASON_FREE_TEXT_COMMIT_UNCONFIRMED
        assert pane.keys.count(("", True, False)) == 1  # exactly ONE Enter

    @pytest.mark.asyncio
    async def test_stamp_exception_withholds_the_enter(self, monkeypatch):
        pane = FakePane([AUQ_LIVE, AUQ_LANDED, AUQ_TYPED, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        def boom(*_a):
            raise RuntimeError("stamp exploded")

        monkeypatch.setattr(free_text, "_stamp", boom)

        result = await free_text.try_answer(WINDOW, AUQ_TYPED_TEXT, user_turn=STAMP)

        assert result is not None
        assert result.outcome is DeliveryOutcome.DRAFT_WRITTEN
        assert result.reason == delivery.REASON_STAMP_FAILED
        assert pane.enter_sent is False

    @pytest.mark.asyncio
    async def test_a_cancellation_after_the_write_arms_the_brake_and_reraises(
        self, monkeypatch
    ):
        pane = FakePane([AUQ_LIVE, AUQ_LANDED, AUQ_TYPED, AUQ_RESOLVED])
        _wire(monkeypatch, pane)

        real_verify = free_text._verify_typed

        async def cancel_mid_verify(*a, **k):
            raise __import__("asyncio").CancelledError()

        monkeypatch.setattr(free_text, "_verify_typed", cancel_mid_verify)

        import asyncio

        with pytest.raises(asyncio.CancelledError):
            await free_text.try_answer(WINDOW, AUQ_TYPED_TEXT, user_turn=STAMP)

        # The payload may be sitting in the affordance row ⇒ the brake goes up,
        # and the cancellation PROPAGATES (never swallowed into a DeliveryResult).
        assert tmux_mod.tmux_manager.window_has_stranded_draft(WINDOW) is True
        assert real_verify is not None
