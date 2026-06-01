"""Scenario: live AUQ shows "🟡 Busy" instead of "🔔 Waiting on you" (Bug 1).

**Verification gate (PR-A).** During a live ``AskUserQuestion`` the interactive
``tool_use`` is buffered in JSONL until the prompt resolves, so ``route_runtime``
never ingests it: the route stays ``RUNNING`` and the activity-digest header
renders "🟡 Busy" (the wired-but-unreachable "🔔 Waiting on you" at
``message_queue.py`` never fires) while the false "typing…" chat-action keeps
firing (``typing_eligible`` excludes ``WAITING_ON_USER``).

This file reproduces the bug at the public Telegram seam by *withholding* the
interactive ``tool_use`` event — modelling Claude Code's JSONL buffer — while a
pane-confirmed picker is live and a side file is present. The digest header is
captured via the existing ``message_queue._render_activity_digest(state,
route=route)`` direct-render path (same pattern as
``test_route_runtime_snapshot.py``).

PR-A asserts the **buggy baseline** ("🟡 Busy" / ``RUNNING`` / typing-eligible)
and passes on ``main`` — proving the harness captures the bug. PR-C wires the
fix (``route_runtime.mark_interactive_pending`` promoted from the 1 Hz poller on
a pane-confirmed surface) and flips these assertions to the fixed
"🔔 Waiting on you" / ``WAITING_ON_USER`` / typing-off behaviour, and adds the
full state-machine matrix (clear-ordering, multi-question, reconciliation,
tombstone, teardown). Scope: Bug 1 only.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from cctelegram import route_runtime
from cctelegram.route_runtime import RunState
from cctelegram.handlers import interactive_ui, message_queue, status_polling
from cctelegram.utils import app_dir
from tests.conftest import ScenarioHarness

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


def _picker_pane() -> str:
    """A live, pane-confirmed single-select AUQ picker (``extract_interactive_content``
    matches this shape)."""
    return """← ☐ Rollout lane  ✔ Submit →
Which rollout lane should we take?

❯ 1. A) Ship now
  2. B) Bake first
Enter to select · ↑/↓ to navigate · Esc to cancel
"""


def _write_side_file(tool_input: dict[str, Any]) -> Path:
    """Write the PreToolUse ``auq_pending/<session>.json`` side file (the
    question-is-live authority), exactly as ``hook.py`` does before the picker
    renders."""
    pending = app_dir() / "auq_pending"
    pending.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = pending / f"{_SESSION_ID}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": _SESSION_ID,
                "tool_use_id": "tool-use-waiting-indicator",
                "written_at": time.time(),
                "tool_input": tool_input,
            }
        )
    )
    return path


def _bind(scenario: ScenarioHarness, pane: str) -> str:
    wid = scenario.add_window(window_name="repo", cwd="/repo", pane_text=pane)
    scenario.bind_thread(
        _THREAD_ID,
        wid,
        display_name="repo",
        cwd="/repo",
        session_id=_SESSION_ID,
    )
    return wid


async def _render(scenario: ScenarioHarness, wid: str) -> None:
    """Publish the interactive picker card (sets interactive mode). Renders from
    the pane / side file — it does NOT ingest the buffered ``tool_use``, so
    ``route_runtime`` run-state is untouched (the bug's premise)."""
    assert await interactive_ui.handle_interactive_ui(
        scenario.bot,
        scenario.user_id,
        wid,
        _THREAD_ID,
        tmux_mgr=scenario.tmux,
        session_mgr=scenario.session_manager,
    )


async def _poll(scenario: ScenarioHarness, wid: str, n: int) -> None:
    for _ in range(n):
        await status_polling.update_status_message(
            scenario.bot,
            user_id=scenario.user_id,
            window_id=wid,
            thread_id=_THREAD_ID,
        )


def _render_digest(
    scenario: ScenarioHarness, route: route_runtime.Route, wid: str
) -> str:
    """Render the activity-digest card text for ``route`` (header is driven by
    ``route_runtime.snapshot(route).run_state``)."""
    state = message_queue.ActivityDigestState(message_id=0, window_id=wid)
    return message_queue._render_activity_digest(state, route=route)


@pytest.mark.asyncio
async def test_live_auq_red_baseline_shows_busy_not_waiting(
    scenario: ScenarioHarness,
) -> None:
    """RED baseline (PR-A): a live AUQ with the interactive ``tool_use``
    WITHHELD leaves ``route_runtime`` at ``RUNNING``, so the digest renders
    "🟡 Busy" and typing stays eligible. PR-C flips all three assertions to
    ``WAITING_ON_USER`` / "🔔 Waiting on you" / typing-off.
    """
    pane = _picker_pane()
    wid = _bind(scenario, pane)
    route: route_runtime.Route = (scenario.user_id, _THREAD_ID, wid)

    # Telegram-originated prompt delivered to tmux → RUNNING (the real
    # ``bot.py`` path via ``mark_inbound_sent``). This is the state the route
    # is stuck in for the whole live-AUQ window.
    await route_runtime.mark_inbound_sent(route)
    assert route_runtime.snapshot(route).run_state is RunState.RUNNING

    # PreToolUse side file lands before the picker renders; the interactive
    # ``tool_use`` is BUFFERED in JSONL — modelled by never dispatching it
    # through the transcript adapter.
    _write_side_file(_single_select_input())
    await _render(scenario, wid)
    assert interactive_ui.has_interactive_surface(scenario.user_id, _THREAD_ID)

    # The 1 Hz poller ticks over the live picker pane. (PR-C: this is where
    # ``mark_interactive_pending`` will promote RUNNING → WAITING_ON_USER.)
    await _poll(scenario, wid, 2)

    snap = route_runtime.snapshot(route)
    # ── RED assertions: the bug. PR-C flips all three. ─────────────────────
    assert snap.run_state is RunState.RUNNING
    assert snap.typing_eligible is True
    rendered = _render_digest(scenario, route, wid)
    assert rendered.startswith("🟡 Busy")
    assert "🔔 Waiting on you" not in rendered
