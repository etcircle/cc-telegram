"""Scenario: live AUQ / ExitPlanMode shows "🔔 Waiting on you" not "🟡 Busy" (Bug 1).

During a live ``AskUserQuestion`` / ``ExitPlanMode`` prompt the interactive
``tool_use`` is buffered in JSONL until the prompt resolves, so ``route_runtime``
never ingests it. The 1 Hz ``status_polling`` poller PROMOTES the active
``RUNNING`` route to ``WAITING_ON_USER`` from a **pane-confirmed** live surface
(``mark_interactive_pending``), so the activity-digest header flips
"🟡 Busy" → "🔔 Waiting on you" and the false "typing…" stops. It is retracted
by the transcript reclaim (primary), the poller liveness reconciliation
(mode-ended), the in-mode tombstone, or route teardown — so no strand and no
false-promote on a double-resume sibling.

These are public-Telegram-seam scenario tests. The digest header is captured via
``message_queue._render_activity_digest(state, route=route)`` (the accepted
precedent from ``test_route_runtime_snapshot.py``); ``run_state`` /
``interactive_pending`` / ``typing_eligible`` are read from the real
``route_runtime.snapshot(route)`` the production surfaces read.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from cctelegram import bot as bot_module
from cctelegram import route_runtime, transcript_event_adapter
from cctelegram.route_runtime import RunState
from cctelegram.handlers import (
    auq_source,
    interactive_ui,
    message_queue,
    status_polling,
)
from cctelegram.session_monitor import TranscriptEvent
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness, make_update_text, make_update_topic_closed

pytestmark = pytest.mark.scenario

_SESSION_ID = "33333333-3333-4333-8333-333333333333"
_THREAD_ID = 42


def _single_select_input() -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": "Which rollout lane should we take?",
                "header": "Rollout lane",
                "multiSelect": False,
                "options": [
                    {"label": "A) Ship now", "description": "Ship the hotfix today."},
                    {"label": "B) Bake first", "description": "Soak on canary first."},
                ],
            }
        ]
    }


def _picker_pane(question: str = "Which rollout lane should we take?") -> str:
    """A live, pane-confirmed single-select AUQ picker."""
    return (
        "← ☐ Rollout lane  ✔ Submit →\n"
        f"{question}\n"
        "\n"
        "❯ 1. A) Ship now\n"
        "  2. B) Bake first\n"
        "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
    )


def _exitplan_pane() -> str:
    """A live ExitPlanMode plan-approval pane (NO PreToolUse side file)."""
    return (
        "  Would you like to proceed?\n"
        "  ─────────────────────────────────\n"
        "  Yes     No\n"
        "  ─────────────────────────────────\n"
        "  ctrl-g to edit in vim\n"
    )


def _gone_pane() -> str:
    """A non-interactive, idle pane (no picker, no anchors)."""
    return "> \n\nClaude is ready.\n"


# Claude task-list overlay: obscures the picker so BOTH pane predicates read
# absent, but the PreToolUse side file is still live (site c — bit-neutral).
_OVERLAY_PANE = (
    "  ◻ Wave 3 R6: pick_verdict() verdict-parity › blocked by #2\n"
    "  ◻ Wave 3 R7: extract auq_context_dedup.py  › blocked by #3\n"
    "  ◻ Wave 3 R8: EditableCard Route Outbox      › blocked by #4\n"
)


def _write_side_file(
    tool_input: dict[str, Any], *, session_id: str = _SESSION_ID
) -> Path:
    pending = app_dir() / "auq_pending"
    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = pending / f"{session_id}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": session_id,
                "tool_use_id": "tool-use-waiting-indicator",
                "written_at": time.time(),
                "tool_input": tool_input,
            }
        )
    )
    return path


def _bind(
    scenario: ScenarioHarness,
    pane: str,
    *,
    thread_id: int = _THREAD_ID,
    session_id: str = _SESSION_ID,
    name: str = "repo",
) -> str:
    wid = scenario.add_window(window_name=name, cwd="/repo", pane_text=pane)
    scenario.bind_thread(
        thread_id, wid, display_name=name, cwd="/repo", session_id=session_id
    )
    return wid


async def _render(
    scenario: ScenarioHarness, wid: str, *, thread_id: int = _THREAD_ID
) -> bool:
    """Publish the interactive picker card (sets interactive mode). Renders from
    the pane / side file — it does NOT ingest the buffered ``tool_use``."""
    return bool(
        await interactive_ui.handle_interactive_ui(
            scenario.bot,
            scenario.user_id,
            wid,
            thread_id,
            tmux_mgr=scenario.tmux,
            session_mgr=scenario.session_manager,
        )
    )


async def _poll(
    scenario: ScenarioHarness, wid: str, n: int = 1, *, thread_id: int = _THREAD_ID
) -> None:
    for _ in range(n):
        await status_polling.update_status_message(
            scenario.bot, user_id=scenario.user_id, window_id=wid, thread_id=thread_id
        )


def _route(
    scenario: ScenarioHarness, wid: str, *, thread_id: int = _THREAD_ID
) -> route_runtime.Route:
    return (scenario.user_id, thread_id, wid)


def _digest(scenario: ScenarioHarness, wid: str, *, thread_id: int = _THREAD_ID) -> str:
    """Render the activity-digest card text for this route (header driven by
    ``route_runtime.snapshot(route).run_state``)."""
    state = message_queue.ActivityDigestState(message_id=0, window_id=wid)
    return message_queue._render_activity_digest(
        state, route=(scenario.user_id, thread_id, wid)
    )


def _txn(**kw: Any) -> TranscriptEvent:
    defaults: dict[str, Any] = dict(
        session_id=_SESSION_ID,
        role="assistant",
        block_type="text",
        tool_use_id=None,
        tool_name=None,
        stop_reason=None,
        timestamp=None,
        text="",
        image_data=None,
    )
    defaults.update(kw)
    return TranscriptEvent(**defaults)


# ── core: the fix (flipped from the PR-A RED baseline) ────────────────────


@pytest.mark.asyncio
async def test_live_auq_shows_waiting_not_busy(scenario: ScenarioHarness) -> None:
    """The fix: a live AUQ whose interactive ``tool_use`` is buffered flips the
    digest to "🔔 Waiting on you", run_state to WAITING_ON_USER, and typing off
    — via the pane-confirmed poller promotion."""
    wid = _bind(scenario, _picker_pane())
    route = _route(scenario, wid)

    await route_runtime.mark_inbound_sent(route)  # RUNNING (the stuck state)
    assert route_runtime.snapshot(route).run_state is RunState.RUNNING
    _write_side_file(_single_select_input())
    assert await _render(scenario, wid)

    await _poll(scenario, wid, 2)

    snap = route_runtime.snapshot(route)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.interactive_pending is True
    assert snap.typing_eligible is False
    rendered = _digest(scenario, wid)
    assert rendered.startswith("🔔 Waiting on you")
    assert "🟡 Busy" not in rendered


@pytest.mark.asyncio
async def test_first_render_poller_path_promotes_site_d(
    scenario: ScenarioHarness,
) -> None:
    """SET site (d): when the poller itself FIRST-renders the picker (no prior
    interactive mode), a successful publish promotes RUNNING → WAITING_ON_USER
    on the same tick. (The other tests pre-render then poll → site (a); this
    proves the first-render dispatch path.)"""
    wid = _bind(scenario, _picker_pane())
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)  # RUNNING
    _write_side_file(_single_select_input())
    # NO _render() — the route is not yet in interactive mode; the poller's
    # new-UI dispatch (site d) renders the card AND promotes.
    assert not interactive_ui.has_interactive_surface(scenario.user_id, _THREAD_ID)

    await _poll(scenario, wid)

    assert interactive_ui.has_interactive_surface(scenario.user_id, _THREAD_ID)
    snap = route_runtime.snapshot(route)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.interactive_pending is True
    assert _digest(scenario, wid).startswith("🔔 Waiting on you")


@pytest.mark.asyncio
async def test_multiquestion_stays_waiting_across_pickers(
    scenario: ScenarioHarness,
) -> None:
    """Q1 → Q2 → Q3: each keeps a picker on the pane, so SET (a) re-asserts
    every tick and the badge stays "🔔 Waiting on you" across non-final picks."""
    wid = _bind(scenario, _picker_pane("Q1: pick a lane?"))
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)
    _write_side_file(_single_select_input())
    await _render(scenario, wid)

    for q in ("Q1: pick a lane?", "Q2: confirm scope?", "Q3: ship window?"):
        scenario.tmux.set_pane(wid, _picker_pane(q))
        await _poll(scenario, wid)
        assert route_runtime.snapshot(route).run_state is RunState.WAITING_ON_USER
        assert _digest(scenario, wid).startswith("🔔 Waiting on you")


@pytest.mark.asyncio
async def test_answer_flush_clears_via_transcript_reclaim(
    scenario: ScenarioHarness,
) -> None:
    """The PRIMARY clear: when the buffered turn flushes, the transcript
    tool_use+tool_result land and re-derive run_state, zeroing the pane bit."""
    wid = _bind(scenario, _picker_pane())
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)
    _write_side_file(_single_select_input())
    await _render(scenario, wid)
    await _poll(scenario, wid)
    assert route_runtime.snapshot(route).interactive_pending is True

    # The buffered turn flushes: interactive tool_use (transcript-set WAITING,
    # bit cleared) then its tool_result (open set empties → RUNNING).
    await transcript_event_adapter.dispatch_transcript_event(
        _txn(block_type="tool_use", tool_use_id="auq-1", tool_name="AskUserQuestion"),
        [route],
    )
    assert route_runtime.snapshot(route).interactive_pending is False
    await transcript_event_adapter.dispatch_transcript_event(
        _txn(role="user", block_type="tool_result", tool_use_id="auq-1"),
        [route],
    )
    snap = route_runtime.snapshot(route)
    assert snap.run_state is RunState.RUNNING
    assert snap.interactive_pending is False
    assert "🔔 Waiting on you" not in _digest(scenario, wid)


@pytest.mark.asyncio
async def test_mode_ended_no_flush_clears_via_reconciliation(
    scenario: ScenarioHarness,
) -> None:
    """The UNIFIED clear: the interactive lifecycle ends (mode popped) with NO
    transcript flush and the pane UI gone → the next poller tick's mode-ended
    reconciliation (`interactive_window != window_id`) clears the bit. The
    bot-less seam (`clear_interactive_msg`) must NOT clear the bit itself."""
    wid = _bind(scenario, _picker_pane())
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)
    _write_side_file(_single_select_input())
    await _render(scenario, wid)
    await _poll(scenario, wid)
    assert route_runtime.snapshot(route).interactive_pending is True

    # Mode ends with no transcript flush (the seam fires); side file gone too.
    auq_source.forget_for_window(wid)
    await interactive_ui.clear_interactive_msg(
        scenario.user_id, scenario.bot, _THREAD_ID
    )
    # Seam is UNCHANGED — the bit still lingers until the poller reconciles.
    assert route_runtime.snapshot(route).interactive_pending is True

    scenario.tmux.set_pane(wid, _gone_pane())
    await _poll(scenario, wid)
    snap = route_runtime.snapshot(route)
    assert snap.interactive_pending is False
    assert snap.run_state is RunState.RUNNING


@pytest.mark.asyncio
async def test_exitplanmode_no_flush_clears_via_reconciliation(
    scenario: ScenarioHarness,
) -> None:
    """ExitPlanMode variant (no side file): pane-confirmed promote, then
    mode-ended reconciliation clears it."""
    wid = _bind(scenario, _exitplan_pane())
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)
    assert await _render(scenario, wid)  # no side file
    await _poll(scenario, wid)
    assert route_runtime.snapshot(route).run_state is RunState.WAITING_ON_USER

    await interactive_ui.clear_interactive_msg(
        scenario.user_id, scenario.bot, _THREAD_ID
    )
    scenario.tmux.set_pane(wid, _gone_pane())
    await _poll(scenario, wid)
    assert route_runtime.snapshot(route).interactive_pending is False


@pytest.mark.asyncio
async def test_esc_in_tmux_tombstone_clears(scenario: ScenarioHarness) -> None:
    """ESC / answer-in-tmux while mode is STILL set, side file gone, pane absent
    ≥ threshold → the in-mode tombstone clears the bit (and the card)."""
    wid = _bind(scenario, _exitplan_pane())  # no side file to manage
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)
    await _render(scenario, wid)
    await _poll(scenario, wid)
    assert route_runtime.snapshot(route).interactive_pending is True

    # Pane goes absent; mode stays set; no side file → streak → tombstone.
    scenario.tmux.set_pane(wid, _gone_pane())
    await _poll(scenario, wid, status_polling.ABSENT_STREAK_THRESHOLD + 1)
    snap = route_runtime.snapshot(route)
    assert snap.interactive_pending is False
    assert not interactive_ui.has_interactive_surface(scenario.user_id, _THREAD_ID)


@pytest.mark.asyncio
async def test_sidefile_only_preserves_prior_waiting(scenario: ScenarioHarness) -> None:
    """Site (c) is bit-neutral: an obscured pane with the side file still live
    PRESERVES an already-promoted WAITING (no flap) — and never tears the card
    down. (The bit shares the AUQ card's liveness boundary.)"""
    wid = _bind(scenario, _picker_pane())
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)
    _write_side_file(_single_select_input())
    await _render(scenario, wid)
    await _poll(scenario, wid)
    assert route_runtime.snapshot(route).interactive_pending is True

    # Task-list overlay obscures the picker; the side file is still live.
    scenario.tmux.set_pane(wid, _OVERLAY_PANE)
    await _poll(scenario, wid, status_polling.ABSENT_STREAK_THRESHOLD + 2)
    snap = route_runtime.snapshot(route)
    assert snap.interactive_pending is True  # preserved (no flap)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert interactive_ui.has_interactive_surface(scenario.user_id, _THREAD_ID)


@pytest.mark.asyncio
async def test_obstructed_from_first_poll_underclaims_busy(
    scenario: ScenarioHarness,
) -> None:
    """Obstructed from the FIRST poll (side file live but never pane-confirmed):
    the bit is never set → bounded under-claim, stays "🟡 Busy"."""
    wid = _bind(scenario, _picker_pane())
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)
    _write_side_file(_single_select_input())
    await _render(scenario, wid)  # card rendered over the picker (exists)
    # Obscured BEFORE any poll pane-confirms the picker → SET (a/b/d) never
    # fire; every poll sees the overlay → site (c) bit-neutral preserve.
    scenario.tmux.set_pane(wid, _OVERLAY_PANE)
    await _poll(scenario, wid, 3)
    snap = route_runtime.snapshot(route)
    assert snap.interactive_pending is False  # never pane-confirmed
    assert snap.run_state is RunState.RUNNING
    assert _digest(scenario, wid).startswith("🟡 Busy")
    # ...but the card is preserved (site c), so the question isn't lost.
    assert interactive_ui.has_interactive_surface(scenario.user_id, _THREAD_ID)


@pytest.mark.asyncio
async def test_exitplanmode_shows_waiting_no_side_file(
    scenario: ScenarioHarness,
) -> None:
    """ExitPlanMode promotes to WAITING / "🔔 Waiting on you" with NO side file
    (pane-confirmed SET works on the plan-approval pane)."""
    wid = _bind(scenario, _exitplan_pane())
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)
    assert await _render(scenario, wid)
    await _poll(scenario, wid, 2)
    snap = route_runtime.snapshot(route)
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.typing_eligible is False
    assert _digest(scenario, wid).startswith("🔔 Waiting on you")
    assert not (app_dir() / "auq_pending" / f"{_SESSION_ID}.json").exists()


@pytest.mark.asyncio
async def test_typing_isolation_concurrent_route(scenario: ScenarioHarness) -> None:
    """The AUQ route suppresses typing (WAITING), while a concurrent RUNNING
    route in another topic is still typing-eligible."""
    wid_auq = _bind(scenario, _picker_pane(), thread_id=42, name="auq")
    route_auq = _route(scenario, wid_auq, thread_id=42)
    await route_runtime.mark_inbound_sent(route_auq)
    _write_side_file(_single_select_input())
    await _render(scenario, wid_auq, thread_id=42)
    await _poll(scenario, wid_auq, thread_id=42)

    wid_run = _bind(
        scenario, _gone_pane(), thread_id=99, session_id="run-sess", name="run"
    )
    route_run = _route(scenario, wid_run, thread_id=99)
    await route_runtime.mark_inbound_sent(route_run)  # RUNNING

    assert route_runtime.snapshot(route_auq).typing_eligible is False
    assert route_runtime.snapshot(route_run).typing_eligible is True


@pytest.mark.asyncio
async def test_clear_mid_auq_settles_idle(scenario: ScenarioHarness) -> None:
    """`/clear` mid-AUQ → mark_session_reset drops the bit and settles IDLE."""
    wid = _bind(scenario, _picker_pane())
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)
    _write_side_file(_single_select_input())
    await _render(scenario, wid)
    await _poll(scenario, wid)
    assert route_runtime.snapshot(route).interactive_pending is True

    await route_runtime.mark_session_reset(route)
    snap = route_runtime.snapshot(route)
    assert snap.run_state is RunState.IDLE_CLEARED
    assert snap.interactive_pending is False
    assert _digest(scenario, wid).startswith("✅ Done")


@pytest.mark.asyncio
async def test_topic_close_clears_waiting(scenario: ScenarioHarness) -> None:
    """Closing the topic mid-AUQ tears the route down via clear_topic_state →
    route_runtime.clear_routes_for_topic: the snapshot is back to the default,
    no stranded "Waiting on you". The route has NO message_queue queue worker
    (the hermes round-2 P2 case) — route_runtime is torn down regardless of
    queue presence, NOT derived from _route_queues."""
    wid = _bind(scenario, _picker_pane())
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)
    _write_side_file(_single_select_input())
    await _render(scenario, wid)
    await _poll(scenario, wid)
    assert route_runtime.snapshot(route).interactive_pending is True
    # No content was enqueued → this route has no _route_queues entry.
    assert route not in message_queue._route_queues

    update = make_update_topic_closed(thread_id=_THREAD_ID, user_id=scenario.user_id)
    await bot_module.topic_closed_handler(update, scenario.context)

    snap = route_runtime.snapshot(route)
    assert snap.run_state is RunState.IDLE_CLEARED  # _default_snapshot
    assert snap.interactive_pending is False


@pytest.mark.asyncio
async def test_inbound_stale_window_unbind_clears_route(
    scenario: ScenarioHarness,
) -> None:
    """codex/hermes round-5 P2: an inbound message that hits a stale window
    (find_window_by_id None) unbinds the thread AND tears down route_runtime
    state — the pane bit (and all route_runtime state) is gone, no leak."""
    wid = _bind(scenario, _picker_pane())
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)
    await route_runtime.mark_interactive_pending(route)
    assert route_runtime.snapshot(route).interactive_pending is True

    # The tmux window vanishes externally; the binding is now stale.
    scenario.tmux.windows.pop(wid, None)
    update = make_update_text("hello?", thread_id=_THREAD_ID, user_id=scenario.user_id)
    await bot_module.text_handler(update, scenario.context)

    snap = route_runtime.snapshot(route)
    assert snap.run_state is RunState.IDLE_CLEARED  # clear_route → default
    assert snap.interactive_pending is False
