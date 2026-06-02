"""Scenario (Bug 2 RED): assistant prose buffered behind a live AUQ/ExitPlanMode
must reach the Telegram user BEFORE the picker card — today it arrives after.

Claude Code co-flushes the whole assistant turn (thinking + prose ``text`` + the
interactive ``tool_use`` + ``tool_result``) to JSONL only at resolution, and the
bot derives content from JSONL via byte-offset reads. So during a live prompt the
prose is not on the bridge: the picker card renders, the user chooses blind, and
the explanatory prose only bursts in afterward. (Live-verified 2026-06-02 —
``temp/auq-fixtures/2026-06-02-messagedisplay-live-capture/``.)

RED-baseline style (repo convention, commit 1dcccff / Bug 1 PR-A): each test
asserts the CURRENT buggy ordering and PASSES today, keeping the scenario floor
green. The fix PR (PR-C: live ``MessageDisplay`` delivery + dedup) flips each
assertion — the flip target is named in every docstring. These touch only the
public ``scenario.bot.sent`` seam (message order / text / reply_markup); they
import no not-yet-existent symbols.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from cctelegram import bot as bot_module
from cctelegram import route_runtime
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


async def _deliver_post_resolution_prose(scenario: ScenarioHarness, route) -> None:
    """Model the buffered turn flushing AFTER the user answered: the picker
    resolves (interactive mode cleared) and the prose lands via the monitor."""
    await interactive_ui.clear_interactive_msg(
        scenario.user_id, scenario.bot, _THREAD_ID
    )
    await bot_module.handle_new_message(
        NewMessage(
            session_id=_SESSION_ID,
            text=_PROSE,
            content_type="text",
            role="assistant",
            stop_reason="end_turn",
        ),
        scenario.bot,
    )
    queue = message_queue.get_content_queue(route)
    if queue is not None:
        await queue.join()
    await asyncio.sleep(0)


def _idx_text(sent, needle: str) -> int:
    return next(
        (i for i, s in enumerate(sent) if needle in (s.kwargs.get("text") or "")), -1
    )


def _idx_markup(sent) -> int:
    return next(
        (i for i, s in enumerate(sent) if s.kwargs.get("reply_markup") is not None), -1
    )


@pytest.mark.asyncio
async def test_red_baseline_prose_not_delivered_live_at_render(
    scenario: ScenarioHarness,
) -> None:
    """RED baseline: rendering a live AUQ picker delivers NO explanatory prose —
    the buffered prose isn't on the bridge yet. PR-C flips this: the live
    ``MessageDisplay`` record is posted as its own message BEFORE the picker
    card, so ``_idx_text(sent, _NEEDLE) != -1`` and precedes the card."""
    wid = _bind(scenario, _picker_pane())
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)
    _write_side_file(_single_select_input())

    assert await _render(scenario, wid)

    sent = scenario.bot.sent
    assert _idx_markup(sent) != -1, "picker card should have rendered"
    assert _idx_text(sent, _NEEDLE) == -1, (
        "RED baseline: prose is NOT delivered live at render today. "
        "PR-C flips this — prose is posted (before the card) from the live "
        "MessageDisplay record."
    )


@pytest.mark.asyncio
async def test_red_baseline_auq_prose_arrives_after_picker_card(
    scenario: ScenarioHarness,
) -> None:
    """RED baseline: with the picker live and the prose arriving only
    post-resolution, the prose lands AFTER the picker card. PR-C flips this to
    ``prose_idx < picker_idx`` and the post-resolution copy is deduped (the
    prose appears exactly once)."""
    wid = _bind(scenario, _picker_pane())
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)
    _write_side_file(_single_select_input())
    assert await _render(scenario, wid)

    await _deliver_post_resolution_prose(scenario, route)

    sent = scenario.bot.sent
    picker_idx = _idx_markup(sent)
    prose_idx = _idx_text(sent, _NEEDLE)
    assert picker_idx != -1, "picker card should have rendered"
    assert prose_idx != -1, "prose should have been delivered (post-resolution)"
    assert picker_idx < prose_idx, (
        f"RED baseline: prose (idx {prose_idx}) arrives AFTER the picker card "
        f"(idx {picker_idx}); the user chose blind. PR-C flips this to "
        "prose_idx < picker_idx (live delivery) with the post-resolution copy "
        "deduped."
    )


@pytest.mark.asyncio
async def test_red_baseline_exitplanmode_prose_arrives_after_plan_card(
    scenario: ScenarioHarness,
) -> None:
    """RED baseline: the ExitPlanMode variant (NO PreToolUse side file) — the
    plan-approval card renders before the explanatory prose. PR-C flips this so
    the prose precedes the card via the same live MessageDisplay path."""
    wid = _bind(scenario, _exitplan_pane())
    route = _route(scenario, wid)
    await route_runtime.mark_inbound_sent(route)

    assert await _render(scenario, wid)  # plan-approval card (no side file)

    await _deliver_post_resolution_prose(scenario, route)

    sent = scenario.bot.sent
    picker_idx = _idx_markup(sent)
    prose_idx = _idx_text(sent, _NEEDLE)
    assert picker_idx != -1, "plan-approval card should have rendered"
    assert prose_idx != -1, "prose should have been delivered (post-resolution)"
    assert picker_idx < prose_idx, (
        f"RED baseline: ExitPlanMode prose (idx {prose_idx}) arrives AFTER the "
        f"plan-approval card (idx {picker_idx}). PR-C flips this to "
        "prose_idx < picker_idx."
    )
