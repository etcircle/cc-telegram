"""Scenario floor: GH #50 PR-2 — a Telegram message ANSWERS a live card.

Black-box, at the public Telegram seam: a real ``Update`` → the real
``text_handler`` → the real aggregator → the real free-text executor → a fake
tmux whose panes are the REAL CC 2.1.207 rig captures.

PR-1 refuses every payload at a live blocking surface. PR-2 makes the ONE surface
it ships for actually answerable:

    AskUserQuestion (single-select)   row N+1  "Type something."

ExitPlanMode had its own lane through peer-review round 3; the owner DROPPED it
on 2026-07-12 (they run ``--dangerously-skip-permissions`` anyway, so hardening a
plan-approval surface did not justify a whole hook + state file + trust
boundary). A plan card therefore takes PR-1's refusal — pinned below as an
explicit, intended degradation, not an accident.

The FakeTmux pane is advanced by a script bound to the keystrokes the executor
sends — that is the TERMINAL behaving like a terminal (the fake substrate), not
a monkeypatch of handler internals: every decision under test is made by real
handler code reading real pane bytes.
"""

from __future__ import annotations

import time
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
from tests.free_text_frames import (
    AUQ_RESOLVED,
    AUQ_X_ANSWER,
    AUQ_X_LANDED,
    AUQ_X_LIVE,
    AUQ_X_TYPED,
    AUQ_X_TYPED_BIG,
    AUQ_Y_TYPED,
    BIG_ANSWER,
    plain,
)

pytestmark = pytest.mark.scenario

V = "v2.1.207"

# The rig captures, chained by CARD GENERATION (see ``tests/free_text_frames``):
# a happy path must walk ONE card end-to-end, and the wrong-card test hands the
# verifier a DIFFERENT real card mid-transaction.
AUQ_LIVE = AUQ_X_LIVE  # card X, cursor row 1
AUQ_LANDED = AUQ_X_LANDED  # card X, cursor row 4, DIM
AUQ_TYPED = AUQ_X_TYPED  # card X, cursor row 4, PLAIN
AUQ_TYPED_BIG = AUQ_X_TYPED_BIG  # card X, the 947-char answer
AUQ_OTHER_CARD_TYPED = AUQ_Y_TYPED  # card Y — a DIFFERENT question, same geometry

OUT_OF_SCOPE = {
    "auq_multi_select": pane_fixture(f"auq_multi_picker_{V}.txt"),
    # ExitPlanMode is out of scope BY DECISION (2026-07-12), not by accident: its
    # option 1 is "Yes, and bypass permissions", and a plan card must now take
    # PR-1's refusal like any other blocking surface.
    "exit_plan_mode": pane_fixture(f"gate_epm_{V}.txt"),
    "folder_trust": pane_fixture(f"folder_trust_arrival_plain_{V}.txt"),
    "switch_model": pane_fixture(f"switch_model_live_{V}.txt"),
}

# The exact answer the typed fixtures render (so the authorship + tail proofs
# see the truth on the pane).
AUQ_ANSWER = AUQ_X_ANSWER


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch):
    free_text.reset_for_tests()
    monkeypatch.setattr(free_text, "NAV_SETTLE_S", 0)
    monkeypatch.setattr(free_text, "TEXT_SETTLE_S", 0)
    monkeypatch.setattr(free_text, "COMMIT_SETTLE_S", 0)
    yield
    free_text.reset_for_tests()


# ── The OCCURRENCE anchors, as REAL files on the REAL substrate ───────────
#
# The free-text lane refuses to touch a card it cannot identify by an
# occurrence-unique anchor: the ``PreToolUse(AskUserQuestion)`` side file, minted
# by the process that is about to block, BEFORE it renders, carrying the prompt's
# per-invocation ``tool_use_id``. The scenario floor drives the REAL reader over
# REAL bytes, stubbing nothing — including the hook-written ``session_map.json``
# (the harness writes it at bind time, exactly as ``SessionStart`` does), because
# the anchor embeds the window's FRESHLY-resolved session generation (round-4 P1).

SESSION_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"  # the reader validates UUIDs

AUQ_TOOL_INPUT = {
    "questions": [
        {
            "question": "What's your favorite color?",
            "header": "Color",
            "options": [{"label": "Blue"}, {"label": "Green"}, {"label": "Red"}],
            "multiSelect": False,
        }
    ]
}


def _write_auq_side_file() -> None:
    """What the ``PreToolUse`` hook writes before an AskUserQuestion renders."""
    from cctelegram.utils import app_dir, atomic_write_json

    pending = app_dir() / "auq_pending"
    pending.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        pending / f"{SESSION_ID}.json",
        {
            "schema_version": 1,
            "session_id": SESSION_ID,
            "tool_use_id": "toolu_CARD_X",
            "tool_input": AUQ_TOOL_INPUT,
            "written_at": time.time(),
            "input_fingerprint": "",  # RECOMPUTED by the reader; never trusted
            "transcript_path": "",
            "cwd": "/repo",
        },
    )


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
        # The frames are ANSI captures (the SGR-2 dim bit carries the PRE-TYPE
        # landing proof — the guard; its POST-write flip is only weak
        # corroboration, since it passes on a real option row), so the fake
        # terminal must expose both views exactly as a real one does.
        if not literal and not enter and keys in ("Down", "Up"):
            h.tmux.set_pane(window_id, plain(landed), ansi=landed)
        elif literal and not enter and keys:
            h.tmux.set_pane(window_id, plain(typed), ansi=typed)
        elif enter and not keys:
            h.tmux.set_pane(window_id, plain(done), ansi=done)
        return ok

    monkeypatch.setattr(_real_tmux, "send_keys", send)


async def _bind(
    h: ScenarioHarness, pane: str, *, card: bool = True, side_file: bool = True
) -> str:
    wid = h.add_window(
        window_name="repo", cwd="/repo", pane_text=plain(pane), pane_text_ansi=pane
    )
    h.bind_thread(42, wid, display_name="repo", cwd="/repo", session_id=SESSION_ID)
    if side_file:
        # The occurrence anchor. ``side_file=False`` is the hook-less install:
        # the lane then has no way to identify the card and must DECLINE (PR-1
        # owns the refusal, nothing is typed).
        _write_auq_side_file()
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


class TestReplyQuotedPayloadsAreEligible:
    """OWNER DECISION (2026-07-12) — supersedes plan §2.3, which made a
    reply-context payload INELIGIBLE.

    The owner's dominant gesture at a card is a VOICE NOTE sent as a REPLY to it
    (both live test messages were), so the as-planned rule refused their most
    natural way of answering — the exact friction this lane exists to remove.
    Claude receives the FULL rendered payload: the quoted context AND the user's
    words, exactly as the bot renders it for any other send.
    """

    @pytest.mark.asyncio
    async def test_a_reply_quoted_voice_note_answers_the_card(
        self, scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
    ):
        """The owner's real shape, end to end: a 947-char voice note carrying the
        reply-context quote of the card it is answering. Its typed render IS the
        rig capture, so the tail + authorship proofs see the truth."""
        wid = await _bind(scenario, AUQ_LIVE)
        _script_pane(
            scenario,
            monkeypatch,
            landed=AUQ_LANDED,
            typed=AUQ_TYPED_BIG,
            done=AUQ_RESOLVED,
        )

        await aggregator_offer_voice(
            (scenario.user_id, 42, wid),
            BIG_ANSWER,
            bot=scenario.bot,
            has_reply_context=True,
        )
        await aggregator_flush_route((scenario.user_id, 42, wid))

        assert scenario.tmux.delivered(BIG_ANSWER), scenario.tmux.written_texts
        # THE QUOTE IS INCLUDED — Claude gets the context, not just the words.
        assert BIG_ANSWER.startswith('> Re: "')
        assert scenario.tmux.written_texts == [BIG_ANSWER]
        assert not any("Not delivered" in n for n in _notices(scenario))

    @pytest.mark.asyncio
    async def test_a_reply_quoted_typed_message_answers_the_card(
        self, scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
    ):
        wid = await _bind(scenario, AUQ_LIVE)
        _script_pane(
            scenario, monkeypatch, landed=AUQ_LANDED, typed=AUQ_TYPED, done=AUQ_RESOLVED
        )

        await aggregator_offer_text(
            (scenario.user_id, 42, wid),
            AUQ_ANSWER,
            bot=scenario.bot,
            has_reply_context=True,
        )
        await aggregator_flush_route((scenario.user_id, 42, wid))

        assert scenario.tmux.delivered(AUQ_ANSWER)
        assert _arrows(scenario) == ["Down", "Down", "Down"]


class TestTheCursorIsAlreadyOnTheFreeTextRow:
    """The card's own ↑/↓ buttons put it there — which is what the card invites.

    Pre-fix, the parser drops the affordance row and clears every real option's
    cursor when the ❯ is parked on it, so the executor found no cursor and
    DECLINED into a PR-1 refusal. The most natural gesture was the broken one.
    """

    @pytest.mark.asyncio
    async def test_zero_nav_keystrokes_and_the_answer_still_lands(
        self, scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
    ):
        wid = await _bind(scenario, AUQ_LANDED)  # ❯ already on "Type something."
        _script_pane(
            scenario, monkeypatch, landed=AUQ_LANDED, typed=AUQ_TYPED, done=AUQ_RESOLVED
        )

        await _send_text(scenario, wid, AUQ_ANSWER)

        assert _arrows(scenario) == [], "the cursor is already there"
        assert scenario.tmux.delivered(AUQ_ANSWER)
        assert not any("Not delivered" in n for n in _notices(scenario))


class TestTheWrongCard:
    @pytest.mark.asyncio
    async def test_a_card_swapped_mid_transaction_never_receives_the_answer(
        self, scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
    ):
        """Card X is on the pane when the message is planned; by the pre-Enter
        capture a DIFFERENT question (card Y — same 3-option geometry, real rig
        bytes) is live and holds our text in ITS free-text row. Every other proof
        passes. Identity refuses, the Enter is withheld, and the user is told."""
        wid = await _bind(scenario, AUQ_LIVE)
        _script_pane(
            scenario,
            monkeypatch,
            landed=AUQ_LANDED,
            typed=AUQ_OTHER_CARD_TYPED,  # the card RESOLVED and a new one rendered
            done=AUQ_RESOLVED,
        )

        await _send_text(scenario, wid, AUQ_ANSWER)

        assert scenario.tmux.committed is False, "the wrong card must not be answered"
        assert any("NOT" in n or "not" in n for n in _notices(scenario))

    @pytest.mark.asyncio
    async def test_an_auq_with_no_PreToolUse_side_file_falls_back_to_the_pr1_refusal(
        self, scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
    ):
        """peer-review round-2 P1 — the AUQ anchor was OPTIONAL, so a missing side
        file silently degraded identity to the PANE, which cannot tell two
        same-shaped cards apart. Without the hook the lane must DECLINE, not
        guess: PR-1 refuses, exactly once, and nothing is typed."""
        wid = await _bind(scenario, AUQ_LIVE, side_file=False)
        _script_pane(
            scenario, monkeypatch, landed=AUQ_LANDED, typed=AUQ_TYPED, done=AUQ_RESOLVED
        )

        await _send_text(scenario, wid, AUQ_ANSWER)

        assert scenario.tmux.written_texts == [], "nothing may be typed into the card"
        assert scenario.tmux.committed is False
        assert _arrows(scenario) == []
        assert any("Not delivered" in n for n in _notices(scenario))


class TestTheStrandedDraftBrake:
    @pytest.mark.asyncio
    async def test_a_braked_window_gets_pr1s_single_refusal(
        self, scenario: ScenarioHarness, monkeypatch: pytest.MonkeyPatch
    ):
        """PR-1 raised the brake because a payload may still be sitting unsent in
        this pane. The free-text lane must not be a way around it — it DECLINES,
        so PR-1 owns the one refusal and the one notice."""
        wid = await _bind(scenario, AUQ_LIVE)
        _script_pane(
            scenario, monkeypatch, landed=AUQ_LANDED, typed=AUQ_TYPED, done=AUQ_RESOLVED
        )
        _real_tmux.mark_window_stranded_draft(wid)

        await _send_text(scenario, wid, AUQ_ANSWER)

        assert scenario.tmux.sent_keys == [], "not one keystroke may reach the pane"
        assert scenario.tmux.committed is False
        notices = [n for n in _notices(scenario) if "Not delivered" in n]
        assert len(notices) == 1, f"exactly ONE refusal, got {notices}"
        assert "input box" in notices[0] or "UNSENT" in notices[0], notices[0]
        # The lane never clears the brake — its release rules are PR-1's.
        assert _real_tmux.window_has_stranded_draft(wid) is True


class TestIneligibleProvenanceKeepsThePr1Refusal:
    """Eligible = typed prose OR voice, AND none of caption / attachment / command.

    (Reply-context WAS on this list; the owner moved it — see
    ``TestReplyQuotedPayloadsAreEligible``.)"""

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
