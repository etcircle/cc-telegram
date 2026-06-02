"""Scenario (Bug 2): assistant prose buffered behind a live AUQ/ExitPlanMode
reaches the Telegram user BEFORE the picker card, exactly once.

Claude Code co-flushes the whole assistant turn (thinking + prose ``text`` + the
interactive ``tool_use`` + ``tool_result``) to JSONL only at resolution, and the
bot derives content from JSONL via byte-offset reads. So during a live prompt the
prose is not on the bridge — without the fix the picker card renders, the user
chooses blind, and the explanatory prose only bursts in afterward.

PR-B captured the prose live (the ``MessageDisplay`` hook → ``md_capture``). This
PR (C+D) delivers it: ``interactive_ui.handle_interactive_ui`` posts the fresh
captured prose BEFORE the picker card and records a shown-live marker, and the
batch dedup (``session_monitor.filter_live_prose_duplicates``) suppresses the
post-resolution JSONL copy so the prose appears exactly once.

History: these were RED baselines in commit 4c77293 (asserting today's buggy
ordering); this file is the FLIPPED, now-passing contract. The tests touch only
the public ``scenario.bot.sent`` seam (message order / text / reply_markup) plus
the realistic capture/flush inputs (a seeded ``MessageDisplay`` record + a
post-resolution batch driven through the real dedup).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from cctelegram import bot as bot_module
from cctelegram import md_capture, route_runtime, session_monitor
from cctelegram.handlers import interactive_ui, message_queue
from cctelegram.session_monitor import NewMessage
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness

pytestmark = pytest.mark.scenario

_SESSION_ID = "44444444-4444-4444-8444-444444444444"
_THREAD_ID = 42
# Plain word characters only, so MarkdownV2 conversion leaves the needle intact.
_PROSE = "SQLite is a zero config serverless embedded relational database"
_NEEDLE = "zero config serverless"


@pytest.fixture
def cc_tmp(tmp_path, monkeypatch):
    """Isolate ``app_dir()`` to a tmp dir so the side file, the MessageDisplay
    capture, and the shown-live markers all land there (never the real
    ~/.cc-telegram). ``app_dir()`` reads the env on each call, so this holds for
    every production read in the test body."""
    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    return tmp_path


def _single_select_input() -> dict[str, Any]:
    return {
        "questions": [
            {
                "question": "Which DB?",
                "header": "DB",
                "multiSelect": False,
                "options": [
                    {"label": "A) SQLite", "description": "Embedded."},
                    {"label": "B) Postgres", "description": "Server."},
                ],
            }
        ]
    }


def _picker_pane() -> str:
    return (
        "← ☐ DB  ✔ Submit →\n"
        "Which DB?\n"
        "\n"
        "❯ 1. A) SQLite\n"
        "  2. B) Postgres\n"
        "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
    )


def _exitplan_pane() -> str:
    return (
        "  Would you like to proceed?\n"
        "  ─────────────────────────────────\n"
        "  Yes     No\n"
        "  ─────────────────────────────────\n"
        "  ctrl-g to edit in vim\n"
    )


def _write_side_file(tool_input: dict[str, Any]) -> Path:
    pending = app_dir() / "auq_pending"
    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = pending / f"{_SESSION_ID}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": _SESSION_ID,
                "tool_use_id": "tool-bug2",
                "written_at": time.time(),
                "tool_input": tool_input,
            }
        )
    )
    return path


def _seed_prose(prose: str, *, final_at: float | None = None) -> None:
    """Seed a finalized MessageDisplay capture record for the session (what the
    hook appender would have written while the message streamed, before the
    picker blocked)."""
    md_capture.msg_display_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    line = {
        "captured_at": final_at if final_at is not None else time.time(),
        "payload": {
            "message_id": "MDLIVE",
            "index": 0,
            "final": True,
            "delta": prose,
            "transcript_path": f"/p/{_SESSION_ID}.jsonl",
        },
    }
    md_capture.session_ndjson_path(_SESSION_ID).write_text(json.dumps(line) + "\n")


def _bind(scenario: ScenarioHarness, pane: str, *, name: str = "repo") -> str:
    wid = scenario.add_window(window_name=name, cwd="/repo", pane_text=pane)
    scenario.bind_thread(
        _THREAD_ID, wid, display_name=name, cwd="/repo", session_id=_SESSION_ID
    )
    return wid


def _route(scenario: ScenarioHarness, wid: str) -> route_runtime.Route:
    return (scenario.user_id, _THREAD_ID, wid)


async def _render(scenario: ScenarioHarness, wid: str) -> bool:
    return bool(
        await interactive_ui.handle_interactive_ui(
            scenario.bot,
            scenario.user_id,
            wid,
            _THREAD_ID,
            tmux_mgr=scenario.tmux,
            session_mgr=scenario.session_manager,
        )
    )


async def _flush_post_resolution(
    scenario: ScenarioHarness, route, *, tool_name: str
) -> None:
    """Model the buffered turn flushing AFTER the user answered: the monitor
    reads the whole turn (prose ``text`` + the interactive ``tool_use``) as one
    batch and runs it through the live-prose dedup before dispatching."""
    await interactive_ui.clear_interactive_msg(
        scenario.user_id, scenario.bot, _THREAD_ID
    )
    batch = [
        NewMessage(
            session_id=_SESSION_ID,
            text=_PROSE,
            content_type="text",
            role="assistant",
            message_id="MID_FLUSH",
            image_data=None,
            stop_reason="tool_use",
        ),
        NewMessage(
            session_id=_SESSION_ID,
            text=f"**{tool_name}**(...)",
            content_type="tool_use",
            tool_name=tool_name,
            role="assistant",
            message_id="MID_FLUSH",
            image_data=None,
            stop_reason="tool_use",
        ),
    ]
    survivors = session_monitor.filter_live_prose_duplicates(batch)
    for msg in survivors:
        await bot_module.handle_new_message(msg, scenario.bot)
    queue = message_queue.get_content_queue(route)
    if queue is not None:
        await queue.join()
    await asyncio.sleep(0)


def _idx_text(sent, needle: str) -> int:
    return next(
        (i for i, s in enumerate(sent) if needle in (s.kwargs.get("text") or "")), -1
    )


def _count_text(sent, needle: str) -> int:
    return sum(1 for s in sent if needle in (s.kwargs.get("text") or ""))


def _idx_markup(sent) -> int:
    return next(
        (i for i, s in enumerate(sent) if s.kwargs.get("reply_markup") is not None), -1
    )


@pytest.mark.asyncio
async def test_prose_delivered_live_before_picker_at_render(
    scenario: ScenarioHarness, cc_tmp
) -> None:
    """Rendering a live AUQ picker delivers the explanatory prose (captured live
    via MessageDisplay) as its own message BEFORE the picker card."""
    wid = _bind(scenario, _picker_pane())
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)
    _write_side_file(_single_select_input())
    _seed_prose(_PROSE)

    assert await _render(scenario, wid)

    sent = scenario.bot.sent
    picker_idx = _idx_markup(sent)
    prose_idx = _idx_text(sent, _NEEDLE)
    assert picker_idx != -1, "picker card should have rendered"
    assert prose_idx != -1, "prose should be delivered live at render"
    assert prose_idx < picker_idx, (
        f"prose (idx {prose_idx}) must precede the picker card (idx {picker_idx})"
    )


@pytest.mark.asyncio
async def test_auq_prose_before_picker_and_deduped(
    scenario: ScenarioHarness, cc_tmp
) -> None:
    """AUQ: prose is delivered live before the card, and the post-resolution
    JSONL copy is deduped so it appears exactly once."""
    wid = _bind(scenario, _picker_pane())
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)
    _write_side_file(_single_select_input())
    _seed_prose(_PROSE)
    assert await _render(scenario, wid)

    picker_idx = _idx_markup(scenario.bot.sent)
    prose_idx = _idx_text(scenario.bot.sent, _NEEDLE)
    assert prose_idx != -1 and picker_idx != -1
    assert prose_idx < picker_idx, "prose precedes the picker card (live delivery)"

    await _flush_post_resolution(scenario, route, tool_name="AskUserQuestion")

    assert _count_text(scenario.bot.sent, _NEEDLE) == 1, (
        "the post-resolution JSONL copy must be deduped — prose appears once"
    )


@pytest.mark.asyncio
async def test_exitplanmode_prose_before_plan_card_and_deduped(
    scenario: ScenarioHarness, cc_tmp
) -> None:
    """ExitPlanMode variant (NO PreToolUse side file): prose is delivered live
    before the plan-approval card and the post-resolution copy is deduped."""
    wid = _bind(scenario, _exitplan_pane())
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)
    _seed_prose(_PROSE)

    assert await _render(scenario, wid)  # plan-approval card (no side file)

    picker_idx = _idx_markup(scenario.bot.sent)
    prose_idx = _idx_text(scenario.bot.sent, _NEEDLE)
    assert picker_idx != -1, "plan-approval card should have rendered"
    assert prose_idx != -1, "prose should be delivered live at render"
    assert prose_idx < picker_idx, "prose precedes the plan-approval card"

    await _flush_post_resolution(scenario, route, tool_name="ExitPlanMode")

    assert _count_text(scenario.bot.sent, _NEEDLE) == 1, (
        "the post-resolution copy must be deduped — prose appears once"
    )


@pytest.mark.asyncio
async def test_late_prose_not_posted_below_existing_card(
    scenario: ScenarioHarness, cc_tmp
) -> None:
    """Panel PR-C+D P2: if the prose isn't captured yet at first render (card
    sent prose-less), a later re-render must NOT post the prose BELOW the now-
    existing card (which would recreate the bug). The card-existence guard makes
    the second render a no-op for prose."""
    wid = _bind(scenario, _picker_pane())
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)
    _write_side_file(_single_select_input())

    # First render with NO capture yet → card posts, no prose.
    assert await _render(scenario, wid)
    assert _idx_markup(scenario.bot.sent) != -1
    assert _idx_text(scenario.bot.sent, _NEEDLE) == -1

    # Prose finalizes late, THEN the poller re-detects / re-renders.
    _seed_prose(_PROSE)
    await _render(scenario, wid)

    assert _count_text(scenario.bot.sent, _NEEDLE) == 0, (
        "prose must not be posted below the already-rendered picker card"
    )


@pytest.mark.asyncio
async def test_topic_close_tears_down_capture_after_unbind(
    scenario: ScenarioHarness, cc_tmp
) -> None:
    """Panel/codex PR-C+D P2: clear_topic_state must tear down the live-prose
    capture even though callers unbind the thread first (the seam resolves the
    session from the route's window, not the unbound thread_bindings)."""
    from cctelegram.handlers.cleanup import clear_topic_state

    wid = _bind(scenario, _picker_pane())
    route = _route(scenario, wid)
    _seed_prose(_PROSE)
    capture = md_capture.session_ndjson_path(_SESSION_ID)
    assert capture.exists()

    # Create a route queue so routes_for_topic returns the route.
    await bot_module.handle_new_message(
        NewMessage(
            session_id=_SESSION_ID,
            text="hello",
            content_type="text",
            role="assistant",
            image_data=None,
        ),
        scenario.bot,
    )
    q = message_queue.get_content_queue(route)
    if q is not None:
        await q.join()

    # Unbind first (as the real callers do), THEN clear topic state.
    scenario.session_manager.unbind_thread(scenario.user_id, _THREAD_ID)
    await clear_topic_state(scenario.user_id, _THREAD_ID, scenario.bot)

    assert not capture.exists(), (
        "topic-close teardown must unlink the capture even after unbind_thread"
    )


@pytest.mark.asyncio
async def test_stale_card_does_not_suppress_live_prose(
    scenario: ScenarioHarness, cc_tmp
) -> None:
    """codex PR-C+D re-review: the card-existence guard must run AFTER the
    staleness gate. A stale ``_interactive_msgs`` entry from a PREVIOUS session
    (about to be dropped + replaced) must NOT make the current render skip live
    prose — otherwise the fresh card lands and the JSONL prose follows below it."""
    stale_session = "99999999-9999-9999-9999-999999999999"
    wid = _bind(scenario, _exitplan_pane())
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)

    # First render under the STALE session → records an _interactive_msgs card
    # bound to that session.
    scenario.bind_thread(
        _THREAD_ID, wid, display_name="repo", cwd="/repo", session_id=stale_session
    )
    assert await _render(scenario, wid)
    n_before = len(scenario.bot.sent)

    # Session rotates (e.g. /clear) → the persisted card is now stale. The
    # CURRENT session has fresh prose captured.
    scenario.bind_thread(
        _THREAD_ID, wid, display_name="repo", cwd="/repo", session_id=_SESSION_ID
    )
    _seed_prose(_PROSE)

    assert await _render(scenario, wid)

    # The stale card was dropped + replaced, AND the live prose was delivered for
    # the new session (before the replacement card).
    new_msgs = scenario.bot.sent[n_before:]
    prose_idx = _idx_text(new_msgs, _NEEDLE)
    picker_idx = _idx_markup(new_msgs)
    assert prose_idx != -1, "live prose must not be suppressed by the stale card"
    assert picker_idx != -1 and prose_idx < picker_idx
