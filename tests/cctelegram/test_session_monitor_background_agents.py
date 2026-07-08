"""GH #44 monitor tests — per-agent sidechain activity aggregation + the
parent-path launch / task-notification signal collection.

Pins the §4.1/§4.2 contract: ``check_sidechain_updates`` aggregates
per-(parent, agent_key) ``SidechainTick``s (max parsed-entry timestamp +
``saw_end_of_turn``, INCLUDING lifecycle-only end-turn markers), the parent
parse path records async-launch agentIds and task-notification completions,
and ``pop_sidechain_activity`` drains the combined structure consume-once.
"""

from __future__ import annotations

import json
import os

import pytest

from cctelegram.session_monitor import SessionInfo, SessionMonitor, TrackedSession
from cctelegram.utils import parse_iso_timestamp

PARENT = "parent-sid"


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

    # scan_projects shells out to tmux for active cwds — stub it like the
    # existing check_for_updates tests do.
    async def _scan():
        return [SessionInfo(session_id=parent_sid, file_path=parent_jsonl)]

    monitor.scan_projects = _scan  # type: ignore[method-assign]
    return parent_jsonl, sub_dir


def _append(path, entries):
    with open(path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


# ── sidechain tick aggregation ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_per_agent_ticks_with_max_timestamp(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc = sub_dir / "agent-abc.jsonl"
    sc.write_text("")
    await monitor.check_sidechain_updates({PARENT})  # register at EOF
    assert monitor.pop_sidechain_activity() == {}

    _append(
        sc,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Bash", {"command": "ls"})],
                timestamp="2026-06-12T08:00:00.000Z",
            ),
            make_jsonl_entry(
                "assistant",
                [{"type": "text", "text": "working"}],
                timestamp="2026-06-12T08:05:00.000Z",
            ),
        ],
    )
    await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert PARENT in activity
    ticks = activity[PARENT].ticks
    assert set(ticks) == {"abc"}  # normalized key — no agent- prefix
    assert ticks["abc"].max_event_ts == parse_iso_timestamp("2026-06-12T08:05:00.000Z")
    assert ticks["abc"].saw_end_of_turn is False
    # Consume-once.
    assert monitor.pop_sidechain_activity() == {}


@pytest.mark.asyncio
async def test_sibling_agents_each_get_their_own_tick(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc1 = sub_dir / "agent-abc.jsonl"
    sc2 = sub_dir / "agent-def.jsonl"
    sc1.write_text("")
    sc2.write_text("")
    await monitor.check_sidechain_updates({PARENT})

    entry = make_jsonl_entry(
        "assistant",
        [make_tool_use_block("t1", "Bash", {"command": "ls"})],
        timestamp="2026-06-12T08:00:00.000Z",
    )
    _append(sc1, [entry])
    _append(sc2, [entry])
    await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert set(activity[PARENT].ticks) == {"abc", "def"}


@pytest.mark.asyncio
async def test_all_none_timestamp_batch_reports_none_ts(
    monitor, tmp_path, make_tool_use_block
):
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc = sub_dir / "agent-abc.jsonl"
    sc.write_text("")
    await monitor.check_sidechain_updates({PARENT})

    entry = {
        "type": "assistant",
        "message": {"content": [make_tool_use_block("t1", "Bash", {})]},
        "sessionId": "x",
        # no timestamp key at all
    }
    _append(sc, [entry])
    await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert activity[PARENT].ticks["abc"].max_event_ts is None


@pytest.mark.asyncio
async def test_end_of_turn_detected_including_lifecycle_only(
    monitor, tmp_path, make_jsonl_entry
):
    """codex r2 P2-2 / hermes r2 P2-3: an end-turn entry with NO visible text
    (lifecycle-only) must still flip saw_end_of_turn."""
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc = sub_dir / "agent-abc.jsonl"
    sc.write_text("")
    await monitor.check_sidechain_updates({PARENT})

    entry = make_jsonl_entry(
        "assistant",
        [],  # empty content — parses to a lifecycle-only end-turn marker
        timestamp="2026-06-12T09:00:00.000Z",
    )
    entry["message"]["stop_reason"] = "end_turn"
    _append(sc, [entry])
    await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert activity[PARENT].ticks["abc"].saw_end_of_turn is True


# ── parent-path signals: async launch + task-notification ────────────────


@pytest.mark.asyncio
async def test_parent_async_launch_recorded(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    launch_text = (
        "Async agent launched successfully.\n"
        "agentId: abc123def456 (internal ID - do not mention to user.)\n"
        "The agent is working in the background."
    )
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t9", "Agent", {"prompt": "go"})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t9", launch_text)],
                session_id=PARENT,
            ),
        ],
    )
    await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert activity[PARENT].launched == {"abc123def456"}


@pytest.mark.asyncio
async def test_parent_task_notification_recorded_as_completion(
    monitor, tmp_path, make_jsonl_entry
):
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    notif = (
        "<task-notification>\n<task-id>abc123def456</task-id>\n"
        "<tool-use-id>toolu_x</tool-use-id>\n<status>completed</status>\n"
        "</task-notification>"
    )
    _append(
        parent_jsonl,
        [make_jsonl_entry("user", notif, session_id=PARENT)],
    )
    await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert activity[PARENT].completed == {"abc123def456"}


@pytest.mark.asyncio
async def test_ordinary_tool_results_and_user_text_record_nothing(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t1", "Bash", {"command": "ls"})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t1", "file1\nfile2")],
                session_id=PARENT,
            ),
            make_jsonl_entry("user", "just a normal prompt", session_id=PARENT),
        ],
    )
    await monitor.check_for_updates({PARENT})
    assert monitor.pop_sidechain_activity() == {}


# ── ISSUE-6: Workflow-tool launch bracket (Fix 2a wiring + 2c heartbeat) ──
#
# The Workflow tool's launch tool_result is shaped differently from the
# Agent/Task `agentId:` launch (Task ID mid-line, separate Run ID + Transcript
# dir). These tests pin the monitor's Workflow branch: a `wf-task:<id>` key in
# `.launched`, the matching `<task-notification>` close key, and the per-poll
# mtime-advance heartbeat into `.bracket_heartbeats` (Fix 2c — run-state is
# bounded by a DIR STAT only, never by parsing sidechain entries).

_WF_TASK = "wtask01abc"
_WF_RUN = "wf_run01abcd"


def _wf_launch_text(wf_dir) -> str:
    return (
        f"Workflow launched in background. Task ID: {_WF_TASK}\n"
        "Summary: background work\n"
        f"Transcript dir: {wf_dir}\n"
        f"Run ID: {_WF_RUN}\n"
    )


@pytest.mark.asyncio
async def test_parent_workflow_launch_recorded_as_wf_task_key(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t9", "Workflow", {"script": "..."})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t9", _wf_launch_text(wf_dir))],
                session_id=PARENT,
            ),
        ],
    )
    await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    # The launch key is the EXACT prefixed string (namespace-isolated from the
    # Agent/Task agentId space), so it == the wf-task close key.
    assert f"wf-task:{_WF_TASK}" in activity[PARENT].launched


@pytest.mark.asyncio
async def test_workflow_task_notification_closes_open_bracket_with_wf_task_key(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    """Fix 2d: a <task-notification> whose Task ID matches an OPEN Workflow
    bracket emits the matching wf-task: close key (so the bracket tombstones).
    The realistic flow is launch → … → close (the launch opened the bracket)."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    notif = (
        f"<task-notification>\n<task-id>{_WF_TASK}</task-id>\n"
        "<tool-use-id>toolu_x</tool-use-id>\n<status>completed</status>\n"
        "</task-notification>"
    )
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t9", "Workflow", {"script": "..."})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t9", _wf_launch_text(wf_dir))],
                session_id=PARENT,
            ),
            make_jsonl_entry("user", notif, session_id=PARENT),
        ],
    )
    await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert f"wf-task:{_WF_TASK}" in activity[PARENT].completed


@pytest.mark.asyncio
async def test_isolated_task_notification_without_bracket_emits_no_wf_task_key(
    monitor, tmp_path, make_jsonl_entry
):
    """Gate-on-bracket (Fix 2d): a <task-notification> with NO open bracket
    (the launch was never observed — restart / bot-down between launch and
    close) emits NO wf-task: close key. There is no route_runtime bg key to
    tombstone in that case, so the bare normalized close key suffices — the
    wf-task: close key is emitted ONLY when its launch bracket is open. This
    forbids guessing 'is this a Workflow id?' from the id's character set (a
    fragile external-format assumption); the OPEN BRACKET is the sole signal."""
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    notif = (
        f"<task-notification>\n<task-id>{_WF_TASK}</task-id>\n"
        "<tool-use-id>toolu_x</tool-use-id>\n<status>completed</status>\n"
        "</task-notification>"
    )
    _append(parent_jsonl, [make_jsonl_entry("user", notif, session_id=PARENT)])
    await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    completed = activity[PARENT].completed if PARENT in activity else set()
    assert f"wf-task:{_WF_TASK}" not in completed


# ── PR-2: structured toolUseResult is the PRIMARY Workflow-launch anchor ──────
#
# The entry-level ``toolUseResult`` ({status: async_launched, taskId, runId,
# transcriptDir}) is the robust, drift-proof anchor; the launch prose regex is
# the FALLBACK (with a WARNING for drift detectability).


def _wf_struct_meta(wf_dir, task_id: str = _WF_TASK, run_id: str = _WF_RUN) -> dict:
    return {
        "status": "async_launched",
        "taskId": task_id,
        "runId": run_id,
        "summary": "background work",
        "transcriptDir": str(wf_dir),
        "scriptPath": "/x/scripts/x.js",
    }


@pytest.mark.asyncio
async def test_workflow_launch_structured_meta_is_primary_anchor(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    """The entry-level ``toolUseResult`` yields the wf-task: key + the bracket
    ``wf_dir`` even when the prose carries NO parseable Task ID line."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t9", "Workflow", {"script": "..."})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                # Prose has NO "Task ID:" line — the structured field is the only
                # anchor.
                [make_tool_result_block("t9", "Workflow scheduled.")],
                session_id=PARENT,
                tool_use_result=_wf_struct_meta(wf_dir),
            ),
        ],
    )
    await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert f"wf-task:{_WF_TASK}" in activity[PARENT].launched
    brackets = monitor._open_workflow_brackets.get(PARENT, {})
    assert _WF_TASK in brackets
    assert brackets[_WF_TASK].wf_dir == wf_dir


@pytest.mark.asyncio
async def test_workflow_launch_prose_fallback_when_structured_absent(
    monitor,
    tmp_path,
    make_jsonl_entry,
    make_tool_use_block,
    make_tool_result_block,
    caplog,
):
    """No structured ``toolUseResult`` (older Claude Code / a future drop) →
    fall back to the prose regex AND log a WARNING for drift detectability."""
    import logging

    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t9", "Workflow", {"script": "..."})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t9", _wf_launch_text(wf_dir))],
                session_id=PARENT,
                # tool_use_result omitted → structured absent
            ),
        ],
    )
    with caplog.at_level(logging.WARNING):
        await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert f"wf-task:{_WF_TASK}" in activity[PARENT].launched
    assert any(
        "structured" in r.message.lower() and "prose" in r.message.lower()
        for r in caplog.records
    ), "expected a WARNING that the structured field was absent and prose was used"


@pytest.mark.asyncio
async def test_workflow_launch_structured_wins_over_prose(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    """Structured + prose BOTH present and DISAGREEING → structured wins (the
    authoritative entry-level field beats the drift-prone prose)."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    prose_other = (
        "Workflow launched in background. Task ID: proseonlyid\n"
        f"Transcript dir: {wf_dir}\n"
        f"Run ID: {_WF_RUN}\n"
    )
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t9", "Workflow", {"script": "..."})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t9", prose_other)],
                session_id=PARENT,
                tool_use_result=_wf_struct_meta(wf_dir, task_id=_WF_TASK),
            ),
        ],
    )
    await monitor.check_for_updates({PARENT})
    launched = monitor.pop_sidechain_activity()[PARENT].launched
    assert f"wf-task:{_WF_TASK}" in launched
    assert "wf-task:proseonlyid" not in launched


@pytest.mark.asyncio
async def test_workflow_structured_present_but_rejected_does_not_prose_fallback(
    monitor,
    tmp_path,
    make_jsonl_entry,
    make_tool_use_block,
    make_tool_result_block,
    caplog,
):
    """hermes P2: the prose fallback is ABSENT-only, NOT reject-driven. A PRESENT
    structured dict that is not an async_launched Workflow (e.g. a non-launch
    Workflow result) is AUTHORITATIVE — the bot must NOT fall back to a stale /
    quoted ``Task ID:`` line in the prose and open a bogus bracket, and must NOT
    log the 'structured absent' warning."""
    import logging

    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    # A PRESENT but non-launch structured dict + a prose line that LOOKS like a
    # launch (stale / quoted / diagnostic).
    stale_prose = (
        "Workflow result.\n"
        "Workflow launched in background. Task ID: proseonlyid\n"
        f"Transcript dir: {wf_dir}\n"
        f"Run ID: {_WF_RUN}\n"
    )
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t9", "Workflow", {"script": "..."})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t9", stale_prose)],
                session_id=PARENT,
                tool_use_result={"status": "completed", "result": "done"},
            ),
        ],
    )
    with caplog.at_level(logging.WARNING):
        await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    launched = activity[PARENT].launched if PARENT in activity else set()
    assert "wf-task:proseonlyid" not in launched
    assert monitor._open_workflow_brackets.get(PARENT, {}) == {}
    assert not any("structured" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_workflow_launch_structured_present_logs_no_warning(
    monitor,
    tmp_path,
    make_jsonl_entry,
    make_tool_use_block,
    make_tool_result_block,
    caplog,
):
    """The drift WARNING fires ONLY on the prose-fallback path — a normal launch
    with the structured field present must stay silent (no false drift alarm)."""
    import logging

    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t9", "Workflow", {"script": "..."})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t9", _wf_launch_text(wf_dir))],
                session_id=PARENT,
                tool_use_result=_wf_struct_meta(wf_dir),
            ),
        ],
    )
    with caplog.at_level(logging.WARNING):
        await monitor.check_for_updates({PARENT})
    assert f"wf-task:{_WF_TASK}" in monitor.pop_sidechain_activity()[PARENT].launched
    assert not any("structured" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_workflow_bracket_heartbeats_only_on_mtime_advance(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    """Fix 2c: an OPEN bracket's wf_dir is stat'd each poll; a `wf-task:` activity
    refresh is emitted ONLY when the freshest `*.jsonl` mtime ADVANCED (real new
    sidechain writes). No advance → no heartbeat → the key ages out via TTL."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    agent_file = wf_dir / "agent-aaa111.jsonl"
    agent_file.write_text("{}\n")
    t0 = agent_file.stat().st_mtime
    os.utime(agent_file, (t0, t0))

    # Open the bracket via the launch parse, then drain the launch signal.
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("t9", "Workflow", {"script": "..."})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("t9", _wf_launch_text(wf_dir))],
                session_id=PARENT,
            ),
        ],
    )
    await monitor.check_for_updates({PARENT})
    await monitor.check_sidechain_updates({PARENT})
    monitor.pop_sidechain_activity()  # drain launch + any baseline registration

    # Real new sidechain write → mtime advances → heartbeat for the wf-task key.
    os.utime(agent_file, (t0 + 30, t0 + 30))
    await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    hb = activity[PARENT].bracket_heartbeats
    assert f"wf-task:{_WF_TASK}" in hb
    assert hb[f"wf-task:{_WF_TASK}"] >= t0 + 30

    # No further write → no heartbeat (the gate is advance-only).
    await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    if PARENT in activity:
        assert f"wf-task:{_WF_TASK}" not in activity[PARENT].bracket_heartbeats


# ── Fix 5 PR-A characterization: pin the CURRENT top-level Agent/Task ──────
#   sidechain behavior of check_sidechain_updates BEFORE the helper
#   extraction (§4-A). This is the gate-landing safety net: the extraction
#   of _track_and_emit_sidechain_file(feed_run_state=True) for the top-level
#   loop must keep ALL of (a) tick population, (b) first-seen-at-EOF
#   registration, and (c) the _pending_tools tool_use/tool_result carry
#   across ticks byte-identical. Uses synthetic ids/content (no PII).


@pytest.mark.asyncio
async def test_characterize_toplevel_sidechain_ticks_eof_and_pending_carry(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    """Characterize the existing top-level (subagents/agent-*.jsonl) path.

    Pins three behaviors that the §2.1(a) extraction must preserve:
      (a) ``pop_sidechain_activity().ticks`` carries the normalized stem key
          with the correct ``max_event_ts`` and ``saw_end_of_turn``;
      (b) a newly-discovered file registers at EOF (first observation emits
          NOTHING and the tracker exists at the file's current size);
      (c) ``_pending_tools`` carries an unpaired ``tool_use`` across ticks and
          pairs it with the ``tool_result`` that lands the next tick.
    """
    _, sub_dir = _setup_parent(monitor, tmp_path)
    sc = sub_dir / "agent-char01.jsonl"

    # ── (b) first-seen registers at EOF ──────────────────────────────────
    # Pre-existing "historical" content the bot must NOT replay.
    _append(
        sc,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("old", "Bash", {"command": "echo hi"})],
                timestamp="2026-06-12T07:00:00.000Z",
            )
        ],
    )
    eof_size = sc.stat().st_size

    first_msgs = await monitor.check_sidechain_updates({PARENT})
    assert first_msgs == []  # started at EOF — no historical replay
    assert monitor.pop_sidechain_activity() == {}  # no activity on registration

    tracking_key = f"sub:{PARENT}:agent-char01"
    tracked = monitor.state.get_session(tracking_key)
    assert tracked is not None  # tracker exists after first observation
    assert tracked.parent_session_id == PARENT
    assert tracked.last_byte_offset == eof_size  # registered at EOF

    # ── (c) tick 1: an UNPAIRED tool_use → carried in _pending_tools ──────
    _append(
        sc,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("tc1", "Read", {"file_path": "syn.py"})],
                timestamp="2026-06-12T08:00:00.000Z",
            )
        ],
    )
    msgs_t1 = await monitor.check_sidechain_updates({PARENT})
    # The tool_use is forwarded (subagent-tagged) but its tool_result has not
    # arrived, so the parser carries it as a pending tool for next tick.
    assert [m.subagent_key for m in msgs_t1] == [tracking_key]
    assert msgs_t1[0].content_type == "tool_use"
    assert msgs_t1[0].tool_use_id == "tc1"
    assert "tc1" in monitor._pending_tools.get(tracking_key, {})

    # ── (a) tick 1 activity: normalized stem key + ts + no end-of-turn ────
    activity_t1 = monitor.pop_sidechain_activity()
    assert PARENT in activity_t1
    ticks = activity_t1[PARENT].ticks
    assert set(ticks) == {"char01"}  # normalized — "agent-" stripped
    assert ticks["char01"].max_event_ts == parse_iso_timestamp(
        "2026-06-12T08:00:00.000Z"
    )
    assert ticks["char01"].saw_end_of_turn is False

    # ── (c) tick 2: the tool_result pairs with the carried tool_use ──────
    # Plus an end-of-turn text block so (a) saw_end_of_turn flips True.
    _append(
        sc,
        [
            make_jsonl_entry(
                "user",
                [make_tool_result_block("tc1", "synthetic result")],
                timestamp="2026-06-12T08:05:00.000Z",
            ),
            make_jsonl_entry(
                "assistant",
                [{"type": "text", "text": "done"}],
                timestamp="2026-06-12T08:06:00.000Z",
            ),
        ],
    )
    # Mark the assistant turn's end so saw_end_of_turn flips.
    # (make_jsonl_entry has no stop_reason; set it on the last raw line.)
    raw = sc.read_text().splitlines()
    last = json.loads(raw[-1])
    last["message"]["stop_reason"] = "end_turn"
    raw[-1] = json.dumps(last)
    sc.write_text("\n".join(raw) + "\n")

    msgs_t2 = await monitor.check_sidechain_updates({PARENT})

    # The carried tool_use is now paired with its tool_result and cleared.
    assert "tc1" not in monitor._pending_tools.get(tracking_key, {})
    # The tool_result and the text both forward, subagent-tagged.
    kinds_t2 = [m.content_type for m in msgs_t2]
    assert "tool_result" in kinds_t2
    assert "text" in kinds_t2
    assert all(m.subagent_key == tracking_key for m in msgs_t2)

    # ── (a) tick 2 activity: end-of-turn now seen ────────────────────────
    activity_t2 = monitor.pop_sidechain_activity()
    assert PARENT in activity_t2
    ticks2 = activity_t2[PARENT].ticks
    assert "char01" in ticks2
    assert ticks2["char01"].max_event_ts == parse_iso_timestamp(
        "2026-06-12T08:06:00.000Z"
    )
    assert ticks2["char01"].saw_end_of_turn is True


# ── Fix 5 PR-B: Workflow ↳ display cards via the bracket wf_dir (DISPLAY ──────
#   ONLY; the wf-task: bracket stays the SOLE Workflow run-state input). The
#   nested layout is subagents/workflows/wf_<runid>/agent-*.jsonl, discovered
#   via the OPEN brackets' wf_dir with feed_run_state=False. Synthetic ids/
#   content (no PII).


def _open_bracket(monitor, parent_sid, wf_dir, *, task_id=_WF_TASK):
    """Open a Workflow bracket directly (skip the launch parse).

    Mirrors what ``_open_workflow_bracket`` does from the launch tool_result —
    a tiny synthetic ``WorkflowLaunchInfo`` stand-in carrying the validated
    ``wf_dir`` as ``transcript_dir``.
    """
    from cctelegram.handlers.response_builder import WorkflowLaunchInfo

    monitor._open_workflow_bracket(
        parent_sid,
        WorkflowLaunchInfo(
            task_id=task_id, run_id=wf_dir.name, transcript_dir=str(wf_dir)
        ),
    )


@pytest.mark.asyncio
async def test_workflow_nested_card_emits_via_bracket_wf_dir(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """B-a: a Workflow sub-agent (one level deeper at
    subagents/workflows/wf_<runid>/agent-*.jsonl) is discovered via the open
    bracket's wf_dir, registers at EOF, and forwards its blocks as
    subagent-tagged NewMessages keyed sub:<parent>:<runid>:<stem>."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    agent_file = wf_dir / "agent-aaa111.jsonl"
    agent_file.write_text("")
    _open_bracket(monitor, PARENT, wf_dir)

    tracking_key = f"sub:{PARENT}:{_WF_RUN}:agent-aaa111"

    # First poll registers the nested file at EOF (no replay).
    first = await monitor.check_sidechain_updates({PARENT})
    assert [m for m in first if m.subagent_key == tracking_key] == []
    assert monitor.state.get_session(tracking_key) is not None

    # A new sidechain block forwards as a subagent-tagged NewMessage.
    _append(
        agent_file,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("wt1", "Bash", {"command": "pnpm test"})],
                timestamp="2026-06-12T10:00:00.000Z",
            )
        ],
    )
    msgs = await monitor.check_sidechain_updates({PARENT})
    nested = [m for m in msgs if m.subagent_key == tracking_key]
    assert len(nested) == 1
    assert nested[0].content_type == "tool_use"
    assert nested[0].session_id == PARENT  # routed back to the parent topic


@pytest.mark.asyncio
async def test_workflow_nested_all_block_types_forward(
    monitor,
    tmp_path,
    make_jsonl_entry,
    make_tool_use_block,
    make_tool_result_block,
    make_thinking_block,
):
    """B-a: text/thinking/tool_use/tool_result all forward from the nested
    dir (the per-block emission is shape-agnostic)."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    agent_file = wf_dir / "agent-aaa111.jsonl"
    agent_file.write_text("")
    _open_bracket(monitor, PARENT, wf_dir)
    await monitor.check_sidechain_updates({PARENT})  # register at EOF

    tracking_key = f"sub:{PARENT}:{_WF_RUN}:agent-aaa111"
    _append(
        agent_file,
        [
            make_jsonl_entry("assistant", [make_thinking_block("plan")]),
            make_jsonl_entry("assistant", [{"type": "text", "text": "narrating"}]),
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("wt1", "Read", {"file_path": "syn.py"})],
            ),
            make_jsonl_entry(
                "user", [make_tool_result_block("wt1", "synthetic result")]
            ),
        ],
    )
    msgs = await monitor.check_sidechain_updates({PARENT})
    kinds = [m.content_type for m in msgs if m.subagent_key == tracking_key]
    assert "thinking" in kinds
    assert "text" in kinds
    assert "tool_use" in kinds
    assert "tool_result" in kinds


@pytest.mark.asyncio
async def test_workflow_nested_sidechain_does_NOT_populate_ticks(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """⭐ B-b (run-state isolation, THE decisive guard): a Workflow nested
    sidechain ENTRY must NEVER feed run-state — ``ticks`` stays empty for the
    agent stem (feed_run_state=False). The wf-task: bracket + its mtime
    heartbeat are the SOLE Workflow run-state input."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    agent_file = wf_dir / "agent-aaa111.jsonl"
    agent_file.write_text("")
    _open_bracket(monitor, PARENT, wf_dir)
    await monitor.check_sidechain_updates({PARENT})  # register at EOF
    monitor.pop_sidechain_activity()

    _append(
        agent_file,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("wt1", "Bash", {"command": "ls"})],
                timestamp="2026-06-12T10:00:00.000Z",
            ),
            make_jsonl_entry("assistant", [{"type": "text", "text": "working"}]),
        ],
    )
    await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    # No tick keyed on the agent stem (normalized or raw) from the nested file.
    ticks = activity[PARENT].ticks if PARENT in activity else {}
    assert "aaa111" not in ticks
    assert "agent-aaa111" not in ticks
    # Nothing in this parent's record may key on the agent stem at all.
    if PARENT in activity:
        assert activity[PARENT].ticks == {}


@pytest.mark.asyncio
async def test_workflow_display_emission_and_zero_ticks_in_same_call(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """⭐ B-b: the SAME call returns a display NewMessage AND populates no
    workflow tick — pins both halves so they can't drift apart."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    agent_file = wf_dir / "agent-aaa111.jsonl"
    agent_file.write_text("")
    _open_bracket(monitor, PARENT, wf_dir)
    await monitor.check_sidechain_updates({PARENT})  # register at EOF
    monitor.pop_sidechain_activity()

    tracking_key = f"sub:{PARENT}:{_WF_RUN}:agent-aaa111"
    _append(
        agent_file,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("wt1", "Bash", {"command": "ls"})],
                timestamp="2026-06-12T10:00:00.000Z",
            )
        ],
    )
    msgs = await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()

    assert any(m.subagent_key == tracking_key for m in msgs)  # display fired
    ticks = activity[PARENT].ticks if PARENT in activity else {}
    assert "aaa111" not in ticks and "agent-aaa111" not in ticks  # run-state did not


@pytest.mark.asyncio
async def test_workflow_sidechain_entries_never_mark_background_agent_activity(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """⭐ B-b: the fan-out (pop_sidechain_activity) carries ZERO agent-stem keys
    from a nested workflow write — only a wf-task: heartbeat may be present."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    agent_file = wf_dir / "agent-aaa111.jsonl"
    agent_file.write_text("")
    _open_bracket(monitor, PARENT, wf_dir)
    await monitor.check_sidechain_updates({PARENT})
    monitor.pop_sidechain_activity()

    _append(
        agent_file,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("wt1", "Bash", {"command": "ls"})],
                timestamp="2026-06-12T10:00:00.000Z",
            )
        ],
    )
    await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    rec = activity.get(PARENT)
    if rec is not None:
        # No agent-stem keys anywhere in the run-state channels.
        assert "aaa111" not in rec.ticks
        assert "aaa111" not in rec.launched
        assert "aaa111" not in rec.completed


@pytest.mark.asyncio
async def test_workflow_bracket_heartbeats_unaffected_by_display_widening(
    monitor, tmp_path, make_tool_use_block
):
    """⭐ B-b: the nested agent file is now display-tracked AND the
    bracket_heartbeats mtime-advance path still fires; the display tracker's
    _file_mtimes key is distinct from the bracket's last_seen_mtime, so they
    don't interfere."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    agent_file = wf_dir / "agent-aaa111.jsonl"
    agent_file.write_text("{}\n")
    t0 = agent_file.stat().st_mtime
    os.utime(agent_file, (t0, t0))
    _open_bracket(monitor, PARENT, wf_dir)
    await monitor.check_sidechain_updates({PARENT})  # display register + heartbeat
    monitor.pop_sidechain_activity()

    # Real new sidechain write → mtime advances → heartbeat for the wf-task key.
    os.utime(agent_file, (t0 + 30, t0 + 30))
    await monitor.check_sidechain_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    hb = activity[PARENT].bracket_heartbeats
    assert f"wf-task:{_WF_TASK}" in hb
    # The display tracker's own mtime key is distinct from the bracket's gate.
    tracking_key = f"sub:{PARENT}:{_WF_RUN}:agent-aaa111"
    assert tracking_key in monitor._file_mtimes
    # And display widening drove NO agent-stem tick.
    assert "aaa111" not in activity[PARENT].ticks


@pytest.mark.asyncio
async def test_glob_is_anchored_not_blind_rglob(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """B-e: a decoy dir whose name is NOT a bracket's wf_dir (wrong prefix /
    deeper tree) is NEVER discovered. Discovery is anchored to the open
    bracket's exact wf_dir.glob('agent-*.jsonl'), never a blind rglob."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    # Decoy 1: a workflows dir that is NOT the bracket's wf_dir.
    decoy = sub_dir / "workflows" / "notawf_xxx"
    decoy.mkdir(parents=True, exist_ok=True)
    (decoy / "agent-zzz999.jsonl").write_text("")
    # Decoy 2: a deeper nested agent file inside the bracket's wf_dir.
    deeper = wf_dir / "nested"
    deeper.mkdir(parents=True, exist_ok=True)
    (deeper / "agent-deep01.jsonl").write_text("")
    _open_bracket(monitor, PARENT, wf_dir)
    await monitor.check_sidechain_updates({PARENT})

    # Neither decoy got a tracker.
    assert monitor.state.get_session(f"sub:{PARENT}:notawf_xxx:agent-zzz999") is None
    assert monitor.state.get_session(f"sub:{PARENT}:{_WF_RUN}:agent-deep01") is None
    # Append to the decoys and re-poll — still never emitted.
    (decoy / "agent-zzz999.jsonl").write_text(
        json.dumps(make_jsonl_entry("assistant", [make_tool_use_block("d", "Bash")]))
        + "\n"
    )
    (deeper / "agent-deep01.jsonl").write_text(
        json.dumps(make_jsonl_entry("assistant", [make_tool_use_block("e", "Bash")]))
        + "\n"
    )
    msgs = await monitor.check_sidechain_updates({PARENT})
    assert not any(
        (m.subagent_key or "").endswith("agent-zzz999")
        or (m.subagent_key or "").endswith("agent-deep01")
        for m in msgs
    )


@pytest.mark.asyncio
async def test_restart_no_bracket_no_nested_discovery(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """B-g: a fresh monitor (no open bracket) over an existing nested dir
    discovers NOTHING until a launch re-opens the bracket (in-memory bracket
    state degrades in lockstep with run-state)."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    agent_file = wf_dir / "agent-aaa111.jsonl"
    _append(
        agent_file,
        [make_jsonl_entry("assistant", [make_tool_use_block("wt1", "Bash")])],
    )
    # No bracket opened — restart degradation.
    msgs = await monitor.check_sidechain_updates({PARENT})
    assert not any((m.subagent_key or "").startswith(f"sub:{PARENT}:") for m in msgs)
    assert monitor.state.get_session(f"sub:{PARENT}:{_WF_RUN}:agent-aaa111") is None


@pytest.mark.asyncio
async def test_check_sidechain_updates_empty_parents_no_raise(monitor):
    """⭐ B-h (the v2 P1 regression guard): an active-session set that resolves
    to NO top-level parents returns [] and raises nothing (no post-loop
    parent_session_id NameError)."""
    msgs = await monitor.check_sidechain_updates({"unknown-sid"})
    assert msgs == []


@pytest.mark.asyncio
async def test_two_parents_each_with_open_bracket_both_emit(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """⭐ B-h: TWO tracked parents, each holding an open bracket with a nested
    file → BOTH parents' nested cards emit (guards the only-last-parent
    under-delivery of a post-loop placement)."""
    parent_a_jsonl, sub_a = _setup_parent(monitor, tmp_path, parent_sid="parent-a")
    parent_b_jsonl, sub_b = _setup_parent(monitor, tmp_path, parent_sid="parent-b")

    async def _scan():
        return [
            SessionInfo(session_id="parent-a", file_path=parent_a_jsonl),
            SessionInfo(session_id="parent-b", file_path=parent_b_jsonl),
        ]

    monitor.scan_projects = _scan  # type: ignore[method-assign]

    wf_a = sub_a / "workflows" / "wf_runA"
    wf_b = sub_b / "workflows" / "wf_runB"
    wf_a.mkdir(parents=True, exist_ok=True)
    wf_b.mkdir(parents=True, exist_ok=True)
    (wf_a / "agent-aaa111.jsonl").write_text("")
    (wf_b / "agent-bbb222.jsonl").write_text("")
    _open_bracket(monitor, "parent-a", wf_a, task_id="taskA")
    _open_bracket(monitor, "parent-b", wf_b, task_id="taskB")
    await monitor.check_sidechain_updates({"parent-a", "parent-b"})  # register EOF

    _append(
        wf_a / "agent-aaa111.jsonl",
        [make_jsonl_entry("assistant", [make_tool_use_block("a1", "Bash")])],
    )
    _append(
        wf_b / "agent-bbb222.jsonl",
        [make_jsonl_entry("assistant", [make_tool_use_block("b1", "Bash")])],
    )
    msgs = await monitor.check_sidechain_updates({"parent-a", "parent-b"})
    keys = {m.subagent_key for m in msgs if m.subagent_key}
    assert "sub:parent-a:wf_runA:agent-aaa111" in keys
    assert "sub:parent-b:wf_runB:agent-bbb222" in keys


@pytest.mark.asyncio
async def test_two_concurrent_workflow_runs_same_stem_stream_independently(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """⭐ B-j (run-id key disjointness): under one parent, TWO brackets with
    distinct wf_<runid> dirs each holding agent-aaa111.jsonl (SAME stem,
    DIFFERENT content) → two independent run-id-qualified trackers, two
    distinct cards, NO byte-offset bleed. Fails a plain sub:<parent>:<stem>
    impl (one shared offset → garbled streams)."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_a = sub_dir / "workflows" / "wf_runAAA"
    wf_b = sub_dir / "workflows" / "wf_runBBB"
    wf_a.mkdir(parents=True, exist_ok=True)
    wf_b.mkdir(parents=True, exist_ok=True)
    file_a = wf_a / "agent-aaa111.jsonl"
    file_b = wf_b / "agent-aaa111.jsonl"
    file_a.write_text("")
    file_b.write_text("")
    _open_bracket(monitor, PARENT, wf_a, task_id="taskAAA")
    _open_bracket(monitor, PARENT, wf_b, task_id="taskBBB")
    await monitor.check_sidechain_updates({PARENT})  # register both at EOF

    key_a = f"sub:{PARENT}:wf_runAAA:agent-aaa111"
    key_b = f"sub:{PARENT}:wf_runBBB:agent-aaa111"
    assert monitor.state.get_session(key_a) is not None
    assert monitor.state.get_session(key_b) is not None

    _append(
        file_a,
        [make_jsonl_entry("assistant", [make_tool_use_block("ta", "Read", {})])],
    )
    _append(
        file_b,
        [make_jsonl_entry("assistant", [make_tool_use_block("tb", "Grep", {})])],
    )
    msgs = await monitor.check_sidechain_updates({PARENT})
    by_key: dict[str, list] = {}
    for m in msgs:
        if m.subagent_key:
            by_key.setdefault(m.subagent_key, []).append(m)
    # Each run streamed exactly its OWN tool — no offset bleed / cross-attribution.
    assert [m.tool_name for m in by_key.get(key_a, [])] == ["Read"]
    assert [m.tool_name for m in by_key.get(key_b, [])] == ["Grep"]


@pytest.mark.asyncio
async def test_workflow_nested_trackers_removed_on_parent_cleanup(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """B-f: a parent with an open bracket AND nested display trackers →
    _remove_sidechains_for_parent pops the bracket AND drops the run-id-
    qualified nested trackers + their _file_mtimes (the sub:<parent>: prefix
    sweep covers the run-id-qualified keys)."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "agent-aaa111.jsonl").write_text("")
    _open_bracket(monitor, PARENT, wf_dir)
    await monitor.check_sidechain_updates({PARENT})

    tracking_key = f"sub:{PARENT}:{_WF_RUN}:agent-aaa111"
    assert monitor.state.get_session(tracking_key) is not None
    assert tracking_key in monitor._file_mtimes
    assert PARENT in monitor._open_workflow_brackets

    monitor._remove_sidechains_for_parent(PARENT)

    assert monitor.state.get_session(tracking_key) is None
    assert tracking_key not in monitor._file_mtimes
    assert PARENT not in monitor._open_workflow_brackets


@pytest.mark.asyncio
async def test_workflow_bracket_close_stops_display_reads(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    """B-f: after the <task-notification> close pops the bracket (via the
    closing-tick final tail), appending more lines to the nested dir emits NO
    new NewMessage — discovery is bracket-gated."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    agent_file = wf_dir / "agent-aaa111.jsonl"
    agent_file.write_text("")
    _open_bracket(monitor, PARENT, wf_dir)
    await monitor.check_sidechain_updates({PARENT})

    # Close the bracket via the task-notification path, then run the closing
    # tick's sidechain pass (tails final tail + pops the bracket).
    notif = (
        f"<task-notification>\n<task-id>{_WF_TASK}</task-id>\n"
        "<tool-use-id>toolu_x</tool-use-id>\n<status>completed</status>\n"
        "</task-notification>"
    )
    _append(parent_jsonl, [make_jsonl_entry("user", notif, session_id=PARENT)])
    await monitor.check_for_updates({PARENT})
    await monitor.check_sidechain_updates({PARENT})  # final tail + pop
    assert PARENT not in monitor._open_workflow_brackets

    # Now appending more lines never re-discovers (bracket gone).
    _append(
        agent_file,
        [make_jsonl_entry("assistant", [make_tool_use_block("late", "Bash")])],
    )
    msgs = await monitor.check_sidechain_updates({PARENT})
    tracking_key = f"sub:{PARENT}:{_WF_RUN}:agent-aaa111"
    assert not any(m.subagent_key == tracking_key for m in msgs)


@pytest.mark.asyncio
async def test_workflow_nested_first_seen_at_eof_skips_history(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """B-f: a nested file with pre-existing content registers at EOF and emits
    nothing on first observation."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    agent_file = wf_dir / "agent-aaa111.jsonl"
    _append(
        agent_file,
        [make_jsonl_entry("assistant", [make_tool_use_block("hist", "Bash")])],
    )
    eof = agent_file.stat().st_size
    _open_bracket(monitor, PARENT, wf_dir)
    msgs = await monitor.check_sidechain_updates({PARENT})
    tracking_key = f"sub:{PARENT}:{_WF_RUN}:agent-aaa111"
    assert not any(m.subagent_key == tracking_key for m in msgs)
    tracked = monitor.state.get_session(tracking_key)
    assert tracked is not None
    assert tracked.last_byte_offset == eof


@pytest.mark.asyncio
async def test_closing_tick_emits_final_tail_then_collapse_then_pops(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    """⭐ B-i (Hermes-delta P1-2, monitor half): the closing tick's
    check_sidechain_updates STILL emits the final display block(s) for the run
    AND appends a NewMessage(subagent_collapse_prefix=...) AFTER them, THEN pops
    the bracket. Popping at the <task-notification> would skip the final tail."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    agent_file = wf_dir / "agent-aaa111.jsonl"
    agent_file.write_text("")
    _open_bracket(monitor, PARENT, wf_dir)
    await monitor.check_sidechain_updates({PARENT})  # register at EOF

    tracking_key = f"sub:{PARENT}:{_WF_RUN}:agent-aaa111"
    collapse_prefix = f"sub:{PARENT}:{_WF_RUN}:"

    # The closing tick: a FINAL nested block lands the same tick the parent's
    # <task-notification> closes the bracket.
    _append(
        agent_file,
        [make_jsonl_entry("assistant", [make_tool_use_block("wfinal", "Bash")])],
    )
    notif = (
        f"<task-notification>\n<task-id>{_WF_TASK}</task-id>\n"
        "<tool-use-id>toolu_x</tool-use-id>\n<status>completed</status>\n"
        "</task-notification>"
    )
    _append(parent_jsonl, [make_jsonl_entry("user", notif, session_id=PARENT)])

    # check_for_updates marks the bracket closing (does NOT pop).
    await monitor.check_for_updates({PARENT})
    assert PARENT in monitor._open_workflow_brackets
    assert monitor._open_workflow_brackets[PARENT][_WF_TASK].closing is True

    # The closing tick's sidechain pass: final tail emitted, then the collapse
    # marker AFTER it, then the bracket popped.
    msgs = await monitor.check_sidechain_updates({PARENT})
    display = [m for m in msgs if m.subagent_key == tracking_key]
    collapse = [m for m in msgs if m.subagent_collapse_prefix == collapse_prefix]
    assert display, "final tail block must still be emitted on the closing tick"
    assert collapse, "a collapse marker must be appended on the closing tick"
    # Ordering: every display block precedes the collapse marker.
    last_display_idx = max(i for i, m in enumerate(msgs) if m in display)
    collapse_idx = min(i for i, m in enumerate(msgs) if m in collapse)
    assert last_display_idx < collapse_idx
    # Bracket popped only after the final tail.
    assert PARENT not in monitor._open_workflow_brackets


@pytest.mark.asyncio
async def test_closing_bracket_does_not_heartbeat(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    """⭐ B-i: a closing bracket is skipped by _emit_workflow_bracket_heartbeats
    (no run-state churn after the done already fired via rec.completed)."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / _WF_RUN
    wf_dir.mkdir(parents=True, exist_ok=True)
    agent_file = wf_dir / "agent-aaa111.jsonl"
    agent_file.write_text("{}\n")
    _open_bracket(monitor, PARENT, wf_dir)
    # Mark the bracket closing directly.
    monitor._open_workflow_brackets[PARENT][_WF_TASK].closing = True

    # Even with a fresh mtime, a closing bracket emits no heartbeat.
    t0 = agent_file.stat().st_mtime
    os.utime(agent_file, (t0 + 60, t0 + 60))
    monitor._emit_workflow_bracket_heartbeats(PARENT)
    activity = monitor.pop_sidechain_activity()
    if PARENT in activity:
        assert f"wf-task:{_WF_TASK}" not in activity[PARENT].bracket_heartbeats


@pytest.mark.asyncio
async def test_wf_dir_none_closing_bracket_emits_no_collapse_just_pops(
    monitor, tmp_path, make_jsonl_entry
):
    """B-i edge: a wf_dir-less closing bracket (malformed launch) emits no
    collapse marker (it never had display cards) and is simply popped."""
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    from cctelegram.handlers.response_builder import WorkflowLaunchInfo

    monitor._open_workflow_bracket(
        PARENT, WorkflowLaunchInfo(task_id=_WF_TASK, run_id=None, transcript_dir=None)
    )
    monitor._open_workflow_brackets[PARENT][_WF_TASK].closing = True
    msgs = await monitor.check_sidechain_updates({PARENT})
    assert not any(m.subagent_collapse_prefix for m in msgs)
    assert PARENT not in monitor._open_workflow_brackets


@pytest.mark.asyncio
async def test_wf_dir_none_closing_bracket_pops_even_without_toplevel_subagents_dir(
    monitor, tmp_path
):
    """hermes P2 sibling of the test above: a wf_dir-less closing bracket on a
    parent with NO top-level ``subagents`` dir at all must still be POPPED — the
    missing-dir guard must default ``sidechain_files`` to ``[]`` WITHOUT
    ``continue``, so the workflow enumeration + closing-pop below still run.
    (The sibling test above creates ``sub_dir`` via ``_setup_parent`` and so
    misses this edge — a bare ``continue`` would strand the bracket.)"""
    from cctelegram.handlers.response_builder import WorkflowLaunchInfo

    # Mirror _setup_parent BUT deliberately do NOT create the subagents dir.
    proj_dir = tmp_path / "projects" / "-tmp-fake"
    proj_dir.mkdir(parents=True, exist_ok=True)
    parent_jsonl = proj_dir / f"{PARENT}.jsonl"
    parent_jsonl.write_text("")
    # NOTE: proj_dir / PARENT / "subagents" intentionally absent.
    assert not (proj_dir / PARENT / "subagents").exists()
    monitor.state.update_session(
        TrackedSession(
            session_id=PARENT,
            file_path=str(parent_jsonl),
            last_byte_offset=parent_jsonl.stat().st_size,
        )
    )

    async def _scan():
        return [SessionInfo(session_id=PARENT, file_path=parent_jsonl)]

    monitor.scan_projects = _scan  # type: ignore[method-assign]

    monitor._open_workflow_bracket(
        PARENT, WorkflowLaunchInfo(task_id=_WF_TASK, run_id=None, transcript_dir=None)
    )
    monitor._open_workflow_brackets[PARENT][_WF_TASK].closing = True

    msgs = await monitor.check_sidechain_updates({PARENT})

    # wf_dir is None → no display cards → no collapse marker emitted.
    assert not any(m.subagent_collapse_prefix for m in msgs)
    # The decisive assertion: the bracket is POPPED, not stranded.
    assert PARENT not in monitor._open_workflow_brackets


# ── typing-unification T1.2 / T1.6: background-Bash launch (2026-07-08) ───────
#
# A ``run_in_background`` Bash launch's tool_result carries a structured
# entry-level ``toolUseResult.backgroundTaskId``. The monitor adds the BARE id
# (== the <task-notification> close key) as a launched key, STRUCTURED-ONLY —
# never lifting from prose. The T1.6 drift detector warns (once per tool_use_id)
# only when the Bash prose announces a background launch but the structured id
# is absent/malformed.

from pathlib import Path  # noqa: E402

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _bg_bash_launch_fixture_lines() -> list[str]:
    """The REAL Bash tool_use + background tool_result launch pair (Claude Code
    2.1.197 capture)."""
    return (_FIXTURES / "bg_bash_launch_v2.1.197.jsonl").read_text().splitlines()


@pytest.mark.asyncio
async def test_parent_background_bash_launch_recorded_from_fixture(monitor, tmp_path):
    """The REAL launch pair → the bare ``byziqxhyh`` key in ``.launched``."""
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    with open(parent_jsonl, "a") as f:
        for line in _bg_bash_launch_fixture_lines():
            f.write(line + "\n")
    await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert activity[PARENT].launched == {"byziqxhyh"}


@pytest.mark.asyncio
async def test_background_bash_launch_recorded_from_structured_meta(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    """Bash-branch-gated: a Bash tool_result carrying ``backgroundTaskId`` →
    the bare launched key."""
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("tb", "Bash", {"command": "sleep 999"})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [
                    make_tool_result_block(
                        "tb", "Command running in background with ID: byziqxhyh."
                    )
                ],
                session_id=PARENT,
                tool_use_result={
                    "stdout": "",
                    "stderr": "",
                    "interrupted": False,
                    "isImage": False,
                    "noOutputExpected": False,
                    "backgroundTaskId": "byziqxhyh",
                },
            ),
        ],
    )
    await monitor.check_for_updates({PARENT})
    activity = monitor.pop_sidechain_activity()
    assert activity[PARENT].launched == {"byziqxhyh"}


@pytest.mark.asyncio
async def test_foreground_bash_result_records_nothing(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    """A plain (foreground) Bash tool_result — no ``backgroundTaskId``, ordinary
    prose — records NOTHING and never warns (guards the existing contract)."""
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("tb", "Bash", {"command": "ls"})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [make_tool_result_block("tb", "file1\nfile2")],
                session_id=PARENT,
            ),
        ],
    )
    await monitor.check_for_updates({PARENT})
    assert monitor.pop_sidechain_activity() == {}


@pytest.mark.asyncio
async def test_bg_bash_prose_without_structured_meta_warns_and_no_lift(
    monitor,
    tmp_path,
    make_jsonl_entry,
    make_tool_use_block,
    make_tool_result_block,
    caplog,
):
    """T1.6: the Bash prose announces a background launch but the structured
    ``backgroundTaskId`` is absent → a single WARNING and NO lift (never lift
    from prose)."""
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("tb", "Bash", {"command": "sleep 999"})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [
                    make_tool_result_block(
                        "tb", "Command running in background with ID: byziqxhyh."
                    )
                ],
                session_id=PARENT,
                # NO tool_use_result — structured meta absent (the drift case).
            ),
        ],
    )
    with caplog.at_level("WARNING"):
        await monitor.check_for_updates({PARENT})
    assert monitor.pop_sidechain_activity() == {}  # NO lift from prose
    drift = [
        r for r in caplog.records if "backgroundTaskId is absent/malformed" in r.message
    ]
    assert len(drift) == 1


@pytest.mark.asyncio
async def test_bg_bash_drift_warning_is_rate_limited_per_tool_use_id(
    monitor,
    tmp_path,
    make_jsonl_entry,
    make_tool_use_block,
    make_tool_result_block,
    caplog,
):
    """T1.6 rate-limit: a re-observed drift launch with the SAME tool_use_id warns
    ONCE — and the test PROVES the Bash branch was genuinely reached on the
    re-observation (hermes fold 2026-07-08: the earlier shape re-appended only the
    lone tool_result, whose tool_use pairing was already consumed on poll 1, so
    ``tool_name`` was no longer ``"Bash"`` and the branch was never REACHED — the
    assertion passed vacuously, guard or no guard; empirically reproduced).

    Poll 2 re-appends the FULL tool_use+tool_result pair (same tool_use_id
    ``tb``), so the same-batch pairing re-yields ``tool_name == "Bash"`` and the
    branch runs — only ``_bg_bash_drift_warned`` suppresses the second warning.
    Poll 3 is the reachability proof AND the guard-deletion simulation: clearing
    the guard set and appending the identical pair fires the warning AGAIN, so
    (a) poll 2's identical construction provably reached the branch, and (b) if
    someone deletes the rate-limit guard, poll 2 itself re-warns and the
    ``== 1`` assertion fails."""
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)

    def _launch_pair() -> list[dict]:
        # Same tool_use_id every time — the rate-limit key under test.
        return [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("tb", "Bash", {"command": "sleep 999"})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [
                    make_tool_result_block(
                        "tb", "Command running in background with ID: byziqxhyh."
                    )
                ],
                session_id=PARENT,
            ),
        ]

    def _drift_count() -> int:
        return len(
            [
                r
                for r in caplog.records
                if "backgroundTaskId is absent/malformed" in r.message
            ]
        )

    with caplog.at_level("WARNING"):
        _append(parent_jsonl, _launch_pair())
        await monitor.check_for_updates({PARENT})
        assert _drift_count() == 1
        assert "tb" in monitor._bg_bash_drift_warned  # the guard state under test

        # Poll 2: the FULL pair again (same tool_use_id) — pairing re-yields
        # tool_name=="Bash", the branch RUNS, the guard suppresses the re-warn.
        _append(parent_jsonl, _launch_pair())
        await monitor.check_for_updates({PARENT})
        assert _drift_count() == 1  # rate-limited (fails if the guard is deleted)

        # Poll 3 (reachability proof): with the guard state cleared, the SAME
        # construction warns again — so poll 2 provably reached the branch and
        # the guard was the only suppressor.
        monitor._bg_bash_drift_warned.clear()
        _append(parent_jsonl, _launch_pair())
        await monitor.check_for_updates({PARENT})
        assert _drift_count() == 2


@pytest.mark.asyncio
async def test_bg_bash_prose_in_assistant_text_never_warns_or_lifts(
    monitor, tmp_path, make_jsonl_entry, make_text_block, caplog
):
    """T1.6 negative: the SAME prose QUOTED in assistant text (not a Bash
    tool_result) must never warn or lift — the branch is tool_result+Bash
    gated."""
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [
                    make_text_block(
                        "The tool said: Command running in background with ID: "
                        "byziqxhyh."
                    )
                ],
                session_id=PARENT,
            )
        ],
    )
    with caplog.at_level("WARNING"):
        await monitor.check_for_updates({PARENT})
    assert monitor.pop_sidechain_activity() == {}
    assert not any(
        "backgroundTaskId is absent/malformed" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_bg_bash_prose_in_non_bash_tool_result_never_warns(
    monitor,
    tmp_path,
    make_jsonl_entry,
    make_tool_use_block,
    make_tool_result_block,
    caplog,
):
    """T1.6 negative: the prose in a NON-Bash tool_result (e.g. Read) never
    warns — the drift detector is Bash-scoped."""
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    _append(
        parent_jsonl,
        [
            make_jsonl_entry(
                "assistant",
                [make_tool_use_block("tr", "Read", {"file_path": "/x"})],
                session_id=PARENT,
            ),
            make_jsonl_entry(
                "user",
                [
                    make_tool_result_block(
                        "tr", "Command running in background with ID: byziqxhyh."
                    )
                ],
                session_id=PARENT,
            ),
        ],
    )
    with caplog.at_level("WARNING"):
        await monitor.check_for_updates({PARENT})
    assert monitor.pop_sidechain_activity() == {}
    assert not any(
        "backgroundTaskId is absent/malformed" in r.message for r in caplog.records
    )
