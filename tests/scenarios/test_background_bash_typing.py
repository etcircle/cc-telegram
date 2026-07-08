"""Scenario: a ``run_in_background`` Bash launch lifts typing; its completion
clears it (typing-unification T1, 2026-07-08).

Black-box at the public seams — no monkeypatch of run-state internals:

  - the bot's ``apply_sidechain_activity`` fan-out (the sink the monitor's Bash
    launch branch feeds — a bare ``backgroundTaskId`` launched key), and
  - ``transcript_event_adapter.dispatch_transcript_event`` for the parent
    lifecycle (end-of-turn, the machine ``<task-notification>``),

with reads only from ``route_runtime.snapshot(route)``.

The two contracted surfaces (typing + digest/dashboard Busy) follow the
projected snapshot: a stored-idle parent with a live background key projects
RUNNING (typing on) for the whole run; the completion ``<task-notification>``
must NOT force stored RUNNING (T1.3), so once the paired done tombstone lands
the route drops cleanly to idle (typing off) instead of stranding.
"""

from __future__ import annotations

from typing import Any

import pytest

from cctelegram import bot as bot_module
from cctelegram import route_runtime, transcript_event_adapter
from cctelegram.session_monitor import ParentSidechainActivity, TranscriptEvent
from tests.conftest import ScenarioHarness

pytestmark = pytest.mark.scenario

_SID = "sess-bash"
# The bare background-Bash task id (== the launch backgroundTaskId == the
# <task-notification> task-id; no wf-task: prefix).
_BASH_KEY = "byziqxhyh"


def _event(**kw: Any) -> TranscriptEvent:
    defaults: dict[str, Any] = dict(
        session_id=_SID,
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


@pytest.mark.asyncio
async def test_background_bash_lifts_typing_until_completion(
    scenario: ScenarioHarness,
) -> None:
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=42, window_id=wid, display_name="repo", cwd="/repo", session_id=_SID
    )
    route = (scenario.user_id, 42, wid)

    # User prompts; Claude launches the background bash mid-turn. The bot fan-out
    # registers the bare launched key with background provenance.
    await transcript_event_adapter.dispatch_transcript_event(
        _event(role="user", block_type="text", text="run the review"), [route]
    )
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(launched={_BASH_KEY})}
    )

    # Parent end-of-turn: the stored state goes idle, but the live background key
    # PROJECTS RUNNING → typing stays on while the bash runs.
    await transcript_event_adapter.dispatch_transcript_event(
        _event(
            block_type="text",
            stop_reason="end_turn",
            text="launched",
            timestamp="2026-07-08T10:00:01.000Z",
        ),
        [route],
    )
    snap = route_runtime.snapshot(route)
    assert snap.run_state is route_runtime.RunState.RUNNING
    assert snap.typing_eligible is True
    assert snap.background_agents == (_BASH_KEY,)

    # The machine <task-notification> arrives (bash completed) — a real envelope
    # (the adapter derives is_task_notification from the text). It must PRESERVE
    # the stored idle (T1.3) — typing stays on only because the key is still live
    # at this instant (the done fan-out lands next).
    await transcript_event_adapter.dispatch_transcript_event(
        _event(
            role="user",
            block_type="text",
            text=(
                "<task-notification>\n"
                f"<task-id>{_BASH_KEY}</task-id>\n"
                "<status>completed</status>\n"
                "</task-notification>"
            ),
            timestamp="2026-07-08T10:00:02.000Z",
        ),
        [route],
    )
    assert route_runtime.snapshot(route).typing_eligible is True

    # Completion fan-out tombstones the key → clean idle, typing off (NOT
    # stranded — the T1.3 fix's whole point).
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(completed={_BASH_KEY})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.run_state in (
        route_runtime.RunState.IDLE_RECENT,
        route_runtime.RunState.IDLE_CLEARED,
    )
    assert snap.background_agents == ()
