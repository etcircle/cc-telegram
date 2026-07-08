"""Fix C (2026-07-08) monitor tests — resume signal collection + net-intent.

The parent parse path records a ``SendMessage`` resume (structured
``resumedAgentId``) into ``ParentSidechainActivity.resumed`` (a MAP
``key -> resume_ts``, the resume tool_result's EVENT timestamp), and resolves a
same-batch resume/``<task-notification>`` pair for the SAME key by TRANSCRIPT
ORDER (the parent-lane net: ``.resumed`` XOR ``.completed``). ``SidechainTick``
carries the max PARSEABLE end_turn ts + an unparseable-seen flag (the DONE
causality inputs, kept strictly separate from ``max_event_ts`` activity).
"""

from __future__ import annotations

import json

import pytest

from cctelegram.session_monitor import SessionInfo, SessionMonitor, TrackedSession
from cctelegram.utils import parse_iso_timestamp

PARENT = "parent-sid"
RID = "a1951c4043e2c9561"
_RESUME_META = {
    "success": True,
    "message": f'Agent "{RID}" had no active task; resumed from transcript in '
    "the background with your message.",
    "resumedAgentId": RID,
}


@pytest.fixture
def monitor(tmp_path):
    return SessionMonitor(
        projects_path=tmp_path / "projects",
        state_file=tmp_path / "monitor_state.json",
    )


def _setup_parent(monitor, tmp_path, parent_sid: str = PARENT):
    proj_dir = tmp_path / "projects" / "-tmp-fake"
    proj_dir.mkdir(parents=True, exist_ok=True)
    parent_jsonl = proj_dir / f"{parent_sid}.jsonl"
    if not parent_jsonl.exists():
        parent_jsonl.write_text("")
    sub_dir = proj_dir / parent_sid / "subagents"
    sub_dir.mkdir(parents=True, exist_ok=True)
    monitor.state.update_session(
        TrackedSession(
            session_id=parent_sid,
            file_path=str(parent_jsonl),
            last_byte_offset=parent_jsonl.stat().st_size,
        )
    )

    async def _scan():
        return [SessionInfo(session_id=parent_sid, file_path=parent_jsonl)]

    monitor.scan_projects = _scan  # type: ignore[method-assign]
    return parent_jsonl, sub_dir


def _append(path, entries):
    with open(path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _resume_entries(
    make_jsonl_entry, make_tool_use_block, make_tool_result_block, *, ts
):
    """A SendMessage tool_use + its resume tool_result (structured meta)."""
    return [
        make_jsonl_entry(
            "assistant",
            [make_tool_use_block("tu_resume", "SendMessage", {"to": RID})],
            session_id=PARENT,
        ),
        make_jsonl_entry(
            "user",
            [make_tool_result_block("tu_resume", "resumed in the background")],
            session_id=PARENT,
            tool_use_result=_RESUME_META,
            timestamp=ts,
        ),
    ]


def _task_notification_entry(make_jsonl_entry, task_id: str = RID):
    text = (
        "<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        "<tool-use-id>toolu_x</tool-use-id>\n<status>completed</status>\n"
        "<summary>done</summary>\n</task-notification>"
    )
    return make_jsonl_entry("user", text, session_id=PARENT)


# ── resume recorded into the .resumed map with its event ts ──────────────


@pytest.mark.asyncio
async def test_resume_recorded_with_event_ts(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    ts = "2026-07-08T08:06:46.302Z"
    _append(
        parent_jsonl,
        _resume_entries(
            make_jsonl_entry, make_tool_use_block, make_tool_result_block, ts=ts
        ),
    )
    await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert RID in activity[PARENT].resumed
    # must-have 4: resume ts is the SendMessage tool_result EVENT timestamp.
    assert activity[PARENT].resumed[RID] == parse_iso_timestamp(ts)
    assert RID not in activity[PARENT].completed


@pytest.mark.asyncio
async def test_plain_sendmessage_without_resumed_id_records_nothing(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    """A SendMessage to a FRESH agent (no resumedAgentId) is not a resume."""
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("tu1", "SendMessage", {"to": "x"})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("tu1", "delivered")],
                session_id=PARENT,
                tool_use_result={"success": True, "message": "delivered"},
            ),
        ],
    )
    await monitor.check_for_updates({PARENT})
    assert monitor.pop_sidechain_activity() == {}


# ── parent-lane net resolution (transcript order) ────────────────────────


@pytest.mark.asyncio
async def test_same_batch_done_then_resume_nets_to_resumed(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    """done→resume (nudge-after-stop) ⇒ NET resumed (LIVE): the later resume
    supersedes the earlier <task-notification> for the same key."""
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    _append(
        parent_jsonl,
        [
            _task_notification_entry(make_jsonl_entry),
            *_resume_entries(
                make_jsonl_entry,
                make_tool_use_block,
                make_tool_result_block,
                ts="2026-07-08T08:06:46.302Z",
            ),
        ],
    )
    await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert RID in activity[PARENT].resumed
    assert RID not in activity[PARENT].completed


@pytest.mark.asyncio
async def test_same_batch_resume_then_done_nets_to_completed(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    """resume→done (fast finish after the nudge) ⇒ NET completed (TOMBSTONED):
    the later <task-notification> supersedes the earlier resume."""
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    _append(
        parent_jsonl,
        [
            *_resume_entries(
                make_jsonl_entry,
                make_tool_use_block,
                make_tool_result_block,
                ts="2026-07-08T08:06:46.302Z",
            ),
            _task_notification_entry(make_jsonl_entry),
        ],
    )
    await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert RID in activity[PARENT].completed
    assert RID not in activity[PARENT].resumed


# ── SidechainTick DONE-causality inputs ──────────────────────────────────


@pytest.mark.asyncio
async def test_sidechain_tick_tracks_max_end_turn_ts_separately(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc = sub_dir / f"agent-{RID}.jsonl"
    sc.write_text("")
    await monitor.check_sidechain_updates({PARENT})  # register at EOF
    _append(
        sc,
        [
            # A later NON-end-turn activity ts (must NOT feed max_end_turn_ts).
            make_jsonl_entry(
                "assistant",
                [{"type": "text", "text": "working"}],
                timestamp="2026-06-12T08:09:00.000Z",
            ),
            make_jsonl_entry(
                "assistant",
                [{"type": "text", "text": "phase 1 done"}],
                timestamp="2026-06-12T08:05:00.000Z",
            ),
        ],
    )
    # Mark the second entry end-of-turn by patching its stop_reason via a
    # dedicated end-turn entry.
    _append(
        sc,
        [
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "final"}],
                    "stop_reason": "end_turn",
                },
                "timestamp": "2026-06-12T08:06:00.000Z",
                "sessionId": PARENT,
            }
        ],
    )
    await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    tick = activity[PARENT].ticks[RID]
    assert tick.saw_end_of_turn is True
    # max_event_ts is the max over ALL entries (activity), incl the 08:09 text.
    assert tick.max_event_ts == parse_iso_timestamp("2026-06-12T08:09:00.000Z")
    # max_end_turn_ts is the max over END-TURN entries only (done causality).
    assert tick.max_end_turn_ts == parse_iso_timestamp("2026-06-12T08:06:00.000Z")
    assert tick.end_turn_ts_unparseable is False


@pytest.mark.asyncio
async def test_sidechain_tick_flags_unparseable_end_turn_ts(
    monitor, tmp_path, make_jsonl_entry
):
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc = sub_dir / f"agent-{RID}.jsonl"
    sc.write_text("")
    await monitor.check_sidechain_updates({PARENT})
    _append(
        sc,
        [
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "final"}],
                    "stop_reason": "end_turn",
                },
                # No timestamp → unparseable end-turn ts.
                "sessionId": PARENT,
            }
        ],
    )
    await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    tick = activity[PARENT].ticks[RID]
    assert tick.saw_end_of_turn is True
    assert tick.end_turn_ts_unparseable is True
    assert tick.max_end_turn_ts is None
