"""Scenario floor: GH #50 PR-2 — a Telegram message ANSWERS a live card.

Black-box, at the public Telegram seam: a real ``Update`` → the real
``text_handler`` → the real aggregator → the real free-text executor → a fake
tmux whose panes are the REAL CC 2.1.207 rig captures.

PR-1 refuses every payload at a live blocking surface. PR-2 makes the two
surfaces Claude Code gives a free-text affordance actually answerable:

    AskUserQuestion (single-select)   row N+1  "Type something."
    ExitPlanMode                      row 4    "Tell Claude what to change"

The FakeTmux pane is advanced by a script bound to the keystrokes the executor
sends — that is the TERMINAL behaving like a terminal (the fake substrate), not
a monkeypatch of handler internals: every decision under test is made by real
handler code reading real pane bytes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cctelegram import bot as bot_module
from cctelegram.handlers import free_text, interactive_ui
from cctelegram.handlers.inbound_aggregator import (
    aggregator_flush_route,
    aggregator_offer_photo,
    aggregator_offer_text,
    aggregator_offer_voice,
)
from cctelegram.tmux_manager import tmux_manager as _real_tmux
from tests.conftest import (
    IDLE_PANE_V2_1_207,
    ScenarioHarness,
    make_update_command,
    make_update_text,
    pane_fixture,
)

pytestmark = pytest.mark.scenario

V = "v2.1.207"

# The rig captures, in the order the transaction walks them.
AUQ_LIVE = pane_fixture(f"auq_single_picker_{V}.txt")  # cursor row 1
AUQ_LANDED = pane_fixture(f"auq_freetext_row_selected_pretype_{V}.ansi.txt")
AUQ_TYPED = pane_fixture(f"auq_freetext_row_typed_{V}.ansi.txt")
AUQ_TYPED_BIG = pane_fixture(f"auq_freetext_row_typed_large_{V}.ansi.txt")
AUQ_RESOLVED = pane_fixture(f"auq_after_answer_t5_{V}.txt")

EPM_LIVE = pane_fixture(f"gate_epm_{V}.txt")
EPM_LANDED = pane_fixture(f"epm_freetext_row_selected_pretype_{V}.ansi.txt")
EPM_TYPED = pane_fixture(f"epm_freetext_row_typed_{V}.ansi.txt")
EPM_RESOLVED = pane_fixture(f"epm_after_approve_t5_{V}.txt")

OUT_OF_SCOPE = {
    "auq_multi_select": pane_fixture(f"auq_multi_picker_{V}.txt"),
    "folder_trust": pane_fixture(f"folder_trust_arrival_plain_{V}.txt"),
    "switch_model": pane_fixture(f"switch_model_live_{V}.txt"),
}

# The exact answers the typed fixtures render (so the authorship + tail proofs
# see the truth on the pane).
AUQ_ANSWER = "teal, actually"
EPM_ANSWER = "please name it farewell.txt instead"

# The owner's real shape: a 947-char, 9-line voice note (rig-captured; Enter
# committed all 947 chars, JSONL-verified).
BIG_ANSWER = (
    '> Re: "the picker card you posted a moment ago"\n>\n'
    "> Claude asked: What's your favorite color?\n\n"
    "OK so about the colour question, I have been thinking about this for a "
    "while and I want to give you the full reasoning rather than just picking "
    "one of the three options you offered, because none of them is quite right "
    "on its own.\n\n"
    "I would actually prefer a deep teal, somewhere between blue and green, "
    "because it reads well on both light and dark backgrounds and it does not "
    "fight with the orange accent we already use in the header. Blue on its own "
    "is too corporate and cold, green on its own reads too much like a success "
    "state, and red is completely out of the question for anything that is not "
    "an error.\n\n"
    "So please go with teal as the primary, keep the existing orange as the "
    "accent, and make sure the contrast ratio stays above four point five to "
    "one for body text. If teal is impossible for some reason, fall back to "
    "blue, but tell me why first.\n"
)


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch):
    free_text.reset_for_tests()
    monkeypatch.setattr(free_text, "NAV_SETTLE_S", 0)
    monkeypatch.setattr(free_text, "TEXT_SETTLE_S", 0)
    monkeypatch.setattr(free_text, "COMMIT_SETTLE_S", 0)
    yield
    free_text.reset_for_tests()


def _script_pane(
    h: ScenarioHarness,
    monkeypatch: pytest.MonkeyPatch,
    *,
    landed: str,
    typed: str,
    done: str,
) -> None:
    """Make the fake terminal REACT to keystrokes, like a real one.

    Arrow keys → the cursor lands on the affordance row (dim placeholder).
    A literal write → that row now holds our text (plain).
    Enter → the surface resolves.

    Patched onto the tmux SINGLETON — what ``fake_tmux`` binds, and what every
    module that cached ``from .tmux_manager import tmux_manager`` sees.
    """
    real_send = h.tmux.send_keys

    async def send(window_id, keys, enter=True, literal=True):
        ok = await real_send(window_id, keys, enter=enter, literal=literal)
        if not literal and not enter and keys in ("Down", "Up"):
            h.tmux.set_pane(window_id, landed, ansi=landed)
        elif literal and not enter and keys:
            h.tmux.set_pane(window_id, typed, ansi=typed)
        elif enter and not keys:
            h.tmux.set_pane(window_id, done, ansi=done)
        return ok

    monkeypatch.setattr(_real_tmux, "send_keys", send)


async def _bind(h: ScenarioHarness, pane: str, *, card: bool = True) -> str:
    wid = h.add_window(
        window_name="repo", cwd="/repo", pane_text=pane, pane_text_ansi=pane
    )
    h.bind_thread(42, wid, display_name="repo", cwd="/repo", session_id="sess-1")
    if card:
        # The REAL render seam the 1 Hz poller drives — this is what publishes
        # the interactive surface the free-text lane is gated on.
        await interactive_ui.handle_interactive_ui(
            h.bot, h.user_id, wid, 42, tmux_mgr=h.tmux, session_mgr=h.session_manager
        )
    return wid


async def _send_text(h: ScenarioHarness, wid: str, text: str) -> None:
    await bot_module.text_handler(
        make_update_text(text, thread_id=42, user_id=h.user_id, chat_id=h.chat_id),
        h.context,
    )
    await aggregator_flush_route((h.user_id, 42, wid))


def _notices(h: ScenarioHarness) -> list[str]:
    """Every in-topic message the bot posted (the refusal disclosure lane)."""
    return [str(t) for t in h.bot.texts()]


def _arrows(h: ScenarioHarness) -> list[str]:
    return [k for _w, k, e, lit in h.tmux.sent_keys if not lit and not e and k]


class TestAuqSingleSelect:
    @pytest.mark.asyncio
    async def test_a_message_at_a_live_card_becomes_the_answer(
        self, scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
    ):
        wid = await _bind(scenario, AUQ_LIVE)
        _script_pane(
            scenario, monkeypatch, landed=AUQ_LANDED, typed=AUQ_TYPED, done=AUQ_RESOLVED
        )

        await _send_text(scenario, wid, AUQ_ANSWER)

        # 3 real options ⇒ the free-text row is 4 ⇒ 3 Downs (never a wrap).
        assert _arrows(scenario) == ["Down", "Down", "Down"]
        assert scenario.tmux.delivered(AUQ_ANSWER)
        assert not any("Not delivered" in n for n in _notices(scenario))


class TestTheOwnersActualUseCase:
    @pytest.mark.asyncio
    async def test_a_large_voice_note_answer_commits(
        self, scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
    ):
        """947 chars / 9 lines, sent as a VOICE note (voice is free-text-eligible
        — it is the user speaking).

        Rig-measured: an affordance row does NOT paste-collapse. The INPUT BOX
        does (``❯\\xa0[Pasted text #1 +12 lines]`` + ``paste again to expand``,
        past ~800 chars — the shipped PR-1 regression), which is exactly why this
        had to be measured rather than reasoned. The row keeps its number, its
        cursor and a PLAIN (non-SGR-2) label, so the typed-state proof holds and
        Enter submits all 947 chars.
        """
        wid = await _bind(scenario, AUQ_LIVE)
        _script_pane(
            scenario,
            monkeypatch,
            landed=AUQ_LANDED,
            typed=AUQ_TYPED_BIG,
            done=AUQ_RESOLVED,
        )

        await aggregator_offer_voice(
            (scenario.user_id, 42, wid), BIG_ANSWER, bot=scenario.bot
        )
        await aggregator_flush_route((scenario.user_id, 42, wid))

        assert len(BIG_ANSWER) > 800
        assert scenario.tmux.delivered(BIG_ANSWER), scenario.tmux.written_texts
        # ONE literal write — what the bot's send_keys emits, and what the rig
        # proved is consumed as literal text on an affordance row.
        assert scenario.tmux.written_texts == [BIG_ANSWER]
        assert not any("Not delivered" in n for n in _notices(scenario))


class TestExitPlanMode:
    @pytest.mark.asyncio
    async def test_a_message_rejects_the_plan_with_feedback(
        self, scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
    ):
        """Row 4 = "Tell Claude what to change" ⇒ the plan is REJECTED, the
        feedback is delivered, and PLAN MODE IS PRESERVED (rig-verified: the
        plan's file was NOT written and the mode line still read "plan mode on").

        It must never reach option 1 — "Yes, and bypass permissions".
        """
        wid = await _bind(scenario, EPM_LIVE)
        _script_pane(
            scenario, monkeypatch, landed=EPM_LANDED, typed=EPM_TYPED, done=EPM_RESOLVED
        )

        await _send_text(scenario, wid, EPM_ANSWER)

        assert _arrows(scenario) == ["Down", "Down", "Down"]
        assert scenario.tmux.delivered(EPM_ANSWER)


class TestOutOfScopeSurfacesKeepThePr1Refusal:
    """Plan §2.2 — each OUT for a stated reason; the gate must still refuse."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", sorted(OUT_OF_SCOPE))
    async def test_refused_and_nothing_typed(
        self, scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch, name: str
    ):
        wid = await _bind(
            scenario, OUT_OF_SCOPE[name], card=(name == "auq_multi_select")
        )
        _script_pane(
            scenario, monkeypatch, landed=AUQ_LANDED, typed=AUQ_TYPED, done=AUQ_RESOLVED
        )

        await _send_text(scenario, wid, "please use teal")

        assert scenario.tmux.written_texts == [], name
        assert scenario.tmux.committed is False, name
        assert _arrows(scenario) == [], f"{name}: no keystroke may reach this pane"
        assert any("Not delivered" in n for n in _notices(scenario)), name


class TestIneligibleProvenanceKeepsThePr1Refusal:
    """Plan §2.3 — eligible = typed prose OR voice, AND none of
    caption / attachment / reply-context / command."""

    @pytest.mark.asyncio
    async def test_a_slash_command_never_rides_the_lane(
        self, scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
    ):
        """``forward_command_handler`` flushes the bundle and then sends the
        command through the normal gate, so a command can never be typed into a
        card's free-text row."""
        await _bind(scenario, AUQ_LIVE)
        _script_pane(
            scenario, monkeypatch, landed=AUQ_LANDED, typed=AUQ_TYPED, done=AUQ_RESOLVED
        )

        await bot_module.forward_command_handler(
            make_update_command(
                "/compact",
                thread_id=42,
                user_id=scenario.user_id,
                chat_id=scenario.chat_id,
            ),
            scenario.context,
        )

        assert scenario.tmux.written_texts == []
        assert scenario.tmux.committed is False
        assert _arrows(scenario) == []

    @pytest.mark.asyncio
    async def test_an_attachment_bundle_never_rides_the_lane(
        self, scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
    ):
        wid = await _bind(scenario, AUQ_LIVE)
        _script_pane(
            scenario, monkeypatch, landed=AUQ_LANDED, typed=AUQ_TYPED, done=AUQ_RESOLVED
        )

        await aggregator_offer_photo(
            (scenario.user_id, 42, wid),
            Path("/tmp/img.png"),
            "is this teal?",
            None,
            bot=scenario.bot,
        )
        await aggregator_flush_route((scenario.user_id, 42, wid))

        # A caption + an attachment is a message ABOUT files, not an answer.
        assert scenario.tmux.written_texts == []
        assert scenario.tmux.committed is False
        assert _arrows(scenario) == []

    @pytest.mark.asyncio
    async def test_a_reply_context_message_never_rides_the_lane(
        self, scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
    ):
        """A reply-context quote renders a block ADDRESSED to Claude — a message,
        not the card's answer (a deliberate plan §2.3 decision). The fact is
        OBSERVED by ``_apply_reply_context`` and carried on the bundle."""
        wid = await _bind(scenario, AUQ_LIVE)
        _script_pane(
            scenario, monkeypatch, landed=AUQ_LANDED, typed=AUQ_TYPED, done=AUQ_RESOLVED
        )

        await aggregator_offer_text(
            (scenario.user_id, 42, wid),
            "> quoted\n\nplease use teal",
            bot=scenario.bot,
            has_reply_context=True,
        )
        await aggregator_flush_route((scenario.user_id, 42, wid))

        assert scenario.tmux.written_texts == []
        assert scenario.tmux.committed is False
        assert _arrows(scenario) == []

    @pytest.mark.asyncio
    async def test_a_lone_digit_is_never_typed(
        self, scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
    ):
        """A bare digit is a live HOTKEY on these surfaces (rig C7/C8) — it would
        COMMIT an option with no Enter. PR-1's step 0 owns the refusal."""
        wid = await _bind(scenario, AUQ_LIVE)
        _script_pane(
            scenario, monkeypatch, landed=AUQ_LANDED, typed=AUQ_TYPED, done=AUQ_RESOLVED
        )

        await _send_text(scenario, wid, "3")

        assert scenario.tmux.sent_keys == []
        assert any("number" in n for n in _notices(scenario))


class TestLicensing:
    @pytest.mark.asyncio
    async def test_an_unlicensed_cc_version_degrades_to_the_pr1_refusal(
        self, scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
    ):
        """Every CC upgrade empties the effective allowlist until the surface is
        re-characterized — the buttons + PR-1's refusal, never a keystroke driven
        by a stale TUI empiric."""
        wid = await _bind(scenario, AUQ_LIVE)
        scenario.tmux.set_pane_command(wid, "2.1.208")
        _script_pane(
            scenario, monkeypatch, landed=AUQ_LANDED, typed=AUQ_TYPED, done=AUQ_RESOLVED
        )

        await _send_text(scenario, wid, AUQ_ANSWER)

        assert scenario.tmux.written_texts == []
        assert scenario.tmux.committed is False
        assert _arrows(scenario) == []
        assert any("Not delivered" in n for n in _notices(scenario))

    @pytest.mark.asyncio
    async def test_flag_off_degrades_to_the_pr1_refusal(
        self, scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
    ):
        free_text.set_enabled(False)
        wid = await _bind(scenario, AUQ_LIVE)
        _script_pane(
            scenario, monkeypatch, landed=AUQ_LANDED, typed=AUQ_TYPED, done=AUQ_RESOLVED
        )

        await _send_text(scenario, wid, AUQ_ANSWER)

        assert scenario.tmux.written_texts == []
        assert scenario.tmux.committed is False
        assert _arrows(scenario) == []


class TestNonRegression:
    @pytest.mark.asyncio
    async def test_an_idle_pane_still_delivers_normally(
        self, scenario: ScenarioHarness
    ):
        """No card ⇒ the lane is never consulted ⇒ no extra capture, no arrow
        key, and the message goes through PR-1's gate exactly as before."""
        wid = await _bind(scenario, IDLE_PANE_V2_1_207, card=False)

        await _send_text(scenario, wid, "hello claude")

        assert scenario.tmux.delivered("hello claude")
        assert _arrows(scenario) == []
