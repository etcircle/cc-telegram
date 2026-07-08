"""CC 2.1.198 queue-shaped ``<task-notification>`` close (busy-parent) tests.

Pins the fix for the missed close in
``temp/2026-07-08-queued-taskn-close-plan.md``: when a background task completes
while the PARENT is busy, CC writes the close as a ``queue-operation`` /
``enqueue`` entry (payload in top-level ``content``) that the parser's hard
``type`` gate dropped — so the GH #44 ``background_agents`` key never tombstoned
and typing stranded to the 2 h TTL. The startup reconciler scans were blind to
the same lane (restart false-relight).

Fixture: ``tests/fixtures/taskn_queue_shapes_v2.1.198.jsonl`` (the REDACTED-REAL
incident lines + SYNTHETIC negatives; see the fixture README for the
enqueue-anchor audit). Terminology: the ``queue-operation`` line carries a
COMPLETION timestamp, the ``type:"user"`` entry a DELIVERY timestamp.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cctelegram import route_runtime, transcript_parser, utils
from cctelegram.handlers import response_builder
from cctelegram.route_runtime import (
    NotificationClearReason,
    TranscriptLifecycleEvent,
)
from cctelegram.session import ClaudeSession, SessionManager
from cctelegram.session_monitor import SessionInfo, SessionMonitor, TranscriptEvent
from cctelegram.transcript_event_adapter import to_lifecycle_event
from cctelegram.transcript_parser import TranscriptParser
from cctelegram.utils import normalize_background_agent_key

PARENT = "parent-sid"

_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "taskn_queue_shapes_v2.1.198.jsonl"
)


# ── fixture loader (REDACTED-REAL selected by intrinsic fields; SYNTHETIC by
#    the `_fixture` sidecar the parser ignores) ─────────────────────────────


def _fixture_lines() -> list[dict]:
    return [json.loads(ln) for ln in _FIXTURE.read_text().splitlines() if ln.strip()]


def _real_queue_close(task_id: str) -> dict:
    for e in _fixture_lines():
        if (
            "_fixture" not in e
            and e.get("type") == "queue-operation"
            and utils.extract_task_notification_task_id(e.get("content", "")) == task_id
        ):
            return e
    raise KeyError(f"real queue-op close for {task_id}")


def _real_user_delivery(task_id: str) -> dict:
    for e in _fixture_lines():
        if "_fixture" in e or e.get("type") != "user":
            continue
        content = e.get("message", {}).get("content")
        if isinstance(content, str) and (
            utils.extract_task_notification_task_id(content) == task_id
        ):
            return e
    raise KeyError(f"real user delivery for {task_id}")


def _real_bash_launch() -> dict:
    for e in _fixture_lines():
        meta = e.get("toolUseResult")
        if isinstance(meta, dict) and meta.get("backgroundTaskId"):
            return e
    raise KeyError("real bash launch")


def _real_attachment() -> dict:
    for e in _fixture_lines():
        if e.get("type") == "attachment":
            return e
    raise KeyError("attachment")


def _synthetic(kind: str) -> dict:
    for e in _fixture_lines():
        if e.get("_fixture") == f"SYNTHETIC:{kind}":
            return e
    raise KeyError(kind)


def _queue_close_entry(task_id: str, *, timestamp: str = "2026-07-08T18:48:25.000Z"):
    """A SYNTHETIC queue-op close for an arbitrary key (Workflow / resume /
    duplicate variants where the REDACTED-REAL trio is a Bash)."""
    return {
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": timestamp,
        "sessionId": PARENT,
        "content": (
            "<task-notification>\n"
            f"<task-id>{task_id}</task-id>\n"
            "<tool-use-id>toolu_x</tool-use-id>\n"
            "<status>completed</status>\n"
            "<summary>done</summary>\n</task-notification>"
        ),
    }


# ── monitor test scaffolding (mirrors test_session_monitor_background_agents) ─


@pytest.fixture
def monitor(tmp_path):
    return SessionMonitor(
        projects_path=tmp_path / "projects",
        state_file=tmp_path / "monitor_state.json",
    )


def _setup_parent(monitor, tmp_path, parent_sid: str = PARENT):
    from cctelegram.session_monitor import TrackedSession

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


# ══════════════════════════════════════════════════════════════════════════
#  Item 1 — parser synthesis + parity negatives + identity pins
# ══════════════════════════════════════════════════════════════════════════


def test_queue_op_notification_synthesizes_lifecycle_user_entry():
    entries, _ = TranscriptParser.parse_entries([_real_queue_close("b5y24hagb")])
    assert len(entries) == 1
    e = entries[0]
    assert e.role == "user"
    assert e.content_type == "text"
    assert e.lifecycle_only is True
    assert e.text.startswith("<task-notification>")
    assert "<task-id>b5y24hagb</task-id>" in e.text


def test_attachment_delivery_synthesizes_no_entry():
    """The attachment shape is the intentionally-dropped redundant lane
    (attachment-only delivery is a DOCUMENTED unsupported shape)."""
    entries, _ = TranscriptParser.parse_entries([_real_attachment()])
    assert entries == []


def test_synthetic_non_notification_queue_op_no_entry():
    entries, _ = TranscriptParser.parse_entries(
        [_synthetic("non_notification_queue_op")]
    )
    assert entries == []


@pytest.mark.parametrize(
    "kind",
    [
        "leading_whitespace",
        "missing_close_tag",
        "malformed_open_tag",
        "trailing_suffix",
    ],
)
def test_synthetic_predicate_parity_negatives_synthesize_nothing(kind):
    """Codex r1+r2 P1: anything the adapter's full-envelope stamp would reject
    must synthesize NOTHING (else the synthesized user event takes
    route_runtime's genuine-user branch and POPS done tombstones)."""
    entries, _ = TranscriptParser.parse_entries([_synthetic(kind)])
    assert entries == []


def test_transcript_parser_predicate_is_utils_object():
    """The synthesis predicate IS the single owner — no silent fork."""
    assert transcript_parser.is_task_notification is utils.is_task_notification


def test_response_builder_reexports_are_alias_only():
    """Both public re-exports are the SAME objects (never wrappers), so the
    adapter's stamp and the parser's synthesis can never diverge."""
    assert response_builder.is_task_notification is utils.is_task_notification
    assert (
        response_builder.extract_task_notification_task_id
        is utils.extract_task_notification_task_id
    )


def test_empty_turn_tracking_unaffected_by_queue_op():
    """A queued completion is not a model wake: the queue-op branch must not
    touch seen_user_prompt / assistant_emitted_after_prompt, so an otherwise
    empty turn STILL emits the 'finished without responding' warning."""
    batch = [
        {"type": "user", "message": {"content": "do the thing"}},
        _real_queue_close("b5y24hagb"),
        {
            "type": "system",
            "subtype": "turn_duration",
            "durationMs": 1234,
            "timestamp": "2026-07-08T18:32:20.000Z",
            "uuid": "u-td",
        },
    ]
    entries, _ = TranscriptParser.parse_entries(batch)
    # The synthetic close is present as a lifecycle-only entry ...
    synth = [e for e in entries if e.lifecycle_only and e.text.startswith("<task-")]
    assert len(synth) == 1
    # ... and the empty-turn warning STILL fires (the queue-op did not flip
    # assistant_emitted_after_prompt).
    assert any("finished the turn without responding" in e.text for e in entries)


# ══════════════════════════════════════════════════════════════════════════
#  Item 8a — cross-module seam invariant (parser output → adapter stamp)
# ══════════════════════════════════════════════════════════════════════════


def test_synthesized_entry_text_adapter_stamps_task_notification():
    """The seam property itself: feed the parser the queue-op line, take the
    SYNTHESIZED entry's text, and assert the ADAPTER stamps it True (so the
    route gets the machine-initiated task-notification lifecycle, not the
    genuine-user branch)."""
    entries, _ = TranscriptParser.parse_entries([_real_queue_close("bihtr1tc7")])
    assert len(entries) == 1
    synth_text = entries[0].text

    event = TranscriptEvent(
        session_id=PARENT,
        role="user",
        block_type="text",
        tool_use_id=None,
        tool_name=None,
        stop_reason=None,
        timestamp="2026-07-08T18:48:25.577Z",
        text=synth_text,
        image_data=None,
    )
    lifecycle = to_lifecycle_event(event)
    assert lifecycle is not None
    assert lifecycle.is_task_notification is True


# ══════════════════════════════════════════════════════════════════════════
#  Item 2 — monitor live path: queue-op close → rec.completed (RED pre-fix)
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_monitor_queue_op_close_records_completion(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block
):
    """A busy-parent background-bash lifecycle: launch (backgroundTaskId) then
    the queue-shaped close → the monitor records BOTH the launched key and the
    completed key. rec.completed is EMPTY today (the RED case)."""
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    bash_launch = _real_bash_launch()
    tuid = bash_launch["message"]["content"][0]["tool_use_id"]
    _append(
        parent_jsonl,
        [
            # Pair the fixture tool_result with its Bash tool_use so tool_name
            # resolves to "Bash" (the real assistant launch line is out of the
            # 5-line fixture window; the tool_result carries the structured meta).
            make_jsonl_entry(
                "assistant",
                [
                    make_tool_use_block(
                        tuid,
                        "Bash",
                        {"command": "sleep 999", "run_in_background": True},
                    )
                ],
                session_id=PARENT,
            ),
            bash_launch,
            _real_queue_close("bihtr1tc7"),
        ],
    )
    await monitor.check_for_updates({PARENT})
    rec = monitor.pop_sidechain_activity()[PARENT]
    assert "bihtr1tc7" in rec.launched
    assert "bihtr1tc7" in rec.completed


# ══════════════════════════════════════════════════════════════════════════
#  Item 3 — Workflow variant: queue-op close for an OPEN bracket
# ══════════════════════════════════════════════════════════════════════════


def _open_bracket(monitor, parent_sid, wf_dir, *, task_id):
    from cctelegram.handlers.response_builder import WorkflowLaunchInfo

    monitor._open_workflow_bracket(
        parent_sid,
        WorkflowLaunchInfo(
            task_id=task_id, run_id=wf_dir.name, transcript_dir=str(wf_dir)
        ),
    )


@pytest.mark.asyncio
async def test_monitor_queue_op_close_closes_open_workflow_bracket(monitor, tmp_path):
    parent_jsonl, sub_dir = _setup_parent(monitor, tmp_path)
    wf_dir = sub_dir / "workflows" / "wf_runqclose"
    wf_dir.mkdir(parents=True, exist_ok=True)
    _open_bracket(monitor, PARENT, wf_dir, task_id="wtaskqclose")
    _append(parent_jsonl, [_queue_close_entry("wtaskqclose")])

    await monitor.check_for_updates({PARENT})
    rec = monitor.pop_sidechain_activity()[PARENT]
    assert "wtaskqclose" in rec.completed
    assert "wf-task:wtaskqclose" in rec.completed
    assert monitor._open_workflow_brackets[PARENT]["wtaskqclose"].closing is True


# ══════════════════════════════════════════════════════════════════════════
#  Item 4 — Fix C NET ordering (queue-op done participates in transcript order)
# ══════════════════════════════════════════════════════════════════════════

_RID = "aresume012345"
_RESUME_META = {
    "success": True,
    "message": f'Agent "{_RID}" had no active task; resumed in the background.',
    "resumedAgentId": _RID,
}


def _resume_entries(make_jsonl_entry, make_tool_use_block, make_tool_result_block, ts):
    return [
        make_jsonl_entry(
            "assistant",
            [make_tool_use_block("tu_resume", "SendMessage", {"to": _RID})],
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


@pytest.mark.asyncio
async def test_queue_op_done_then_resume_nets_live(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    """done→resume (nudge-after-stop): the queue-shaped close sits in the SAME
    parsed stream as the later SendMessage resume, so the transcript-order net
    keeps the key LIVE (a raw side-channel scan would lose this ordering)."""
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    _append(
        parent_jsonl,
        [
            _queue_close_entry(_RID),
            *_resume_entries(
                make_jsonl_entry,
                make_tool_use_block,
                make_tool_result_block,
                "2026-07-08T08:06:46.302Z",
            ),
        ],
    )
    await monitor.check_for_updates({PARENT})
    rec = monitor.pop_sidechain_activity()[PARENT]
    assert _RID in rec.resumed
    assert _RID not in rec.completed


@pytest.mark.asyncio
async def test_resume_then_queue_op_done_nets_done(
    monitor, tmp_path, make_jsonl_entry, make_tool_use_block, make_tool_result_block
):
    """resume→done (fast finish): the later queue-shaped close supersedes the
    earlier resume."""
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    _append(
        parent_jsonl,
        [
            *_resume_entries(
                make_jsonl_entry,
                make_tool_use_block,
                make_tool_result_block,
                "2026-07-08T08:06:46.302Z",
            ),
            _queue_close_entry(_RID),
        ],
    )
    await monitor.check_for_updates({PARENT})
    rec = monitor.pop_sidechain_activity()[PARENT]
    assert _RID in rec.completed
    assert _RID not in rec.resumed


# ══════════════════════════════════════════════════════════════════════════
#  Item 5 — display: synthetic → NO fan-out; worked shape → one visible card
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_queue_op_synthetic_produces_no_newmessage(monitor, tmp_path):
    """The synthetic entry is lifecycle-only → skipped in the display fan-out,
    but still recorded as a completion."""
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    _append(parent_jsonl, [_real_queue_close("bihtr1tc7")])
    msgs = await monitor.check_for_updates({PARENT})
    assert msgs == []
    rec = monitor.pop_sidechain_activity()[PARENT]
    assert "bihtr1tc7" in rec.completed


@pytest.mark.asyncio
async def test_worked_shape_one_visible_card_no_dup_from_enqueue(monitor, tmp_path):
    """The worked (parent-idle) shape still surfaces EXACTLY one visible card —
    from the ``type:"user"`` delivery — with NO duplicate from its preceding
    queue-op enqueue line."""
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    _append(
        parent_jsonl,
        [_real_queue_close("b5y24hagb"), _real_user_delivery("b5y24hagb")],
    )
    msgs = await monitor.check_for_updates({PARENT})
    visible = [m for m in msgs if m.text.startswith("<task-notification>")]
    assert len(visible) == 1
    assert visible[0].role == "user"
    # Idempotent completion (both lines add the same key; set-valued).
    rec = monitor.pop_sidechain_activity()[PARENT]
    assert "b5y24hagb" in rec.completed


# ══════════════════════════════════════════════════════════════════════════
#  Item 6 — startup scans read the queue-op lane (RED pre-fix) + adversarial
# ══════════════════════════════════════════════════════════════════════════


def _wf_launch_entry(task_id, run_id, wf_dir) -> dict:
    text = (
        f"Workflow launched in background. Task ID: {task_id}\n"
        f"Run ID: {run_id}\n"
        f"Transcript dir: {wf_dir}\n"
    )
    return {
        "type": "user",
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": text}]
        },
        "timestamp": "2026-07-08T08:00:00.000Z",
    }


def _agent_launch_entry(agent_id) -> dict:
    return {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "ta1", "content": "launched"}
            ]
        },
        "toolUseResult": {
            "status": "async_launched",
            "isAsync": True,
            "agentId": agent_id,
        },
        "timestamp": "2026-07-08T08:00:00.000Z",
    }


@pytest.mark.asyncio
async def test_workflow_scan_reads_queue_op_close(monitor, tmp_path):
    """The startup Workflow scan recovers a queue-shaped close (busy-parent) so
    a completed Workflow is NOT false-relit after a restart. Today the queue-op
    lane is invisible to the scan → closes empty (the RED case)."""
    proj = tmp_path / "projects" / "-tmp-fake"
    wf_dir = proj / PARENT / "subagents" / "workflows" / "wf_runqclose"
    wf_dir.mkdir(parents=True, exist_ok=True)
    jsonl = proj / f"{PARENT}.jsonl"
    _append(
        jsonl,
        [
            _wf_launch_entry("wtaskqclose", "wf_runqclose", wf_dir),
            _queue_close_entry("wtaskqclose"),
        ],
    )
    launches, closes, reliable = await monitor._scan_workflow_launches_and_closes(jsonl)
    assert reliable is True
    assert "wtaskqclose" in closes
    assert launches.get("wf_runqclose") == "wtaskqclose"


@pytest.mark.asyncio
async def test_agent_scan_reads_queue_op_close(monitor, tmp_path):
    """The startup Agent scan recovers a queue-shaped close so a completed
    background Agent is NOT false-relit after a restart (RED pre-fix)."""
    proj = tmp_path / "projects" / "-tmp-fake"
    (proj / PARENT / "subagents").mkdir(parents=True, exist_ok=True)
    jsonl = proj / f"{PARENT}.jsonl"
    _append(
        jsonl,
        [_agent_launch_entry("aqclose123"), _queue_close_entry("aqclose123")],
    )
    async_keys, closes, reliable = await monitor._scan_agent_async_launches_and_closes(
        jsonl
    )
    assert reliable is True
    assert normalize_background_agent_key("aqclose123") in async_keys
    assert normalize_background_agent_key("aqclose123") in closes


@pytest.mark.asyncio
async def test_workflow_scan_adversarial_queue_op_no_launch(monitor, tmp_path):
    """ADVERSARIAL (tx-lane-only invariant): a queue-op notification whose body
    embeds 'Task ID: wf_bogus' must close but NEVER mint a launch — launch
    recovery reads ONLY the tool_result (tr) lane."""
    proj = tmp_path / "projects" / "-tmp-fake"
    (proj / PARENT / "subagents").mkdir(parents=True, exist_ok=True)
    jsonl = proj / f"{PARENT}.jsonl"
    _append(jsonl, [_synthetic("adversarial_embedded_launch_strings")])
    launches, closes, reliable = await monitor._scan_workflow_launches_and_closes(jsonl)
    assert reliable is True
    assert "abogusclose1" in closes
    assert launches == {}


@pytest.mark.asyncio
async def test_agent_scan_adversarial_queue_op_no_launch(monitor, tmp_path):
    """ADVERSARIAL: 'agentId: abogus123' embedded in a queue-op notification
    body closes but never mints an async agent key."""
    proj = tmp_path / "projects" / "-tmp-fake"
    (proj / PARENT / "subagents").mkdir(parents=True, exist_ok=True)
    jsonl = proj / f"{PARENT}.jsonl"
    _append(jsonl, [_synthetic("adversarial_embedded_launch_strings")])
    async_keys, closes, reliable = await monitor._scan_agent_async_launches_and_closes(
        jsonl
    )
    assert reliable is True
    assert normalize_background_agent_key("abogusclose1") in closes
    assert async_keys == set()


# ══════════════════════════════════════════════════════════════════════════
#  Item 7 — history: lifecycle_only entries never leak into get_recent_messages
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_lifecycle_only_never_in_get_recent_messages(tmp_path):
    """The synthetic queue-op close has REAL text (the envelope), so without
    the lifecycle_only filter it would leak into /history. The pre-existing
    empty-text end-of-turn markers are excluded too (COUNT pinned)."""
    jsonl = tmp_path / "session.jsonl"
    _append(
        jsonl,
        [
            {"type": "user", "message": {"content": "hello world"}},
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hi there"}]},
            },
            _real_queue_close("b5y24hagb"),  # synthetic lifecycle_only user entry
            {
                "type": "assistant",
                "message": {"content": [], "stop_reason": "end_turn"},
            },  # pre-existing empty-text lifecycle marker
        ],
    )
    sm = SessionManager()

    async def _resolve(window_id):
        return ClaudeSession(
            session_id="s", summary="", message_count=0, file_path=str(jsonl)
        )

    sm.resolve_session_for_window = _resolve  # type: ignore[method-assign]
    messages, count = await sm.get_recent_messages("@0")

    assert count == 2
    assert [m["text"] for m in messages] == ["hello world", "hi there"]
    # (No task-notification envelope + no empty-text entry leaked.)
    assert not any("<task-notification>" in m["text"] for m in messages)
    assert all(m["text"] for m in messages)


# ══════════════════════════════════════════════════════════════════════════
#  Item 8b/8c — route_runtime behavior driven by the is_task_notification stamp
# ══════════════════════════════════════════════════════════════════════════

_ROUTE: route_runtime.Route = (7, 71, "@7")
_SET_AT = 2000.0


@pytest.fixture(autouse=True)
def _reset_route_runtime():
    route_runtime.reset_for_tests()
    yield
    route_runtime.reset_for_tests()


def _evt(role, block="text", *, timestamp=None, is_task_notification=False):
    return TranscriptLifecycleEvent(
        role=role,  # type: ignore[arg-type]
        block_type=block,  # type: ignore[arg-type]
        tool_use_id=None,
        tool_name=None,
        stop_reason=None,
        timestamp=timestamp,
        is_task_notification=is_task_notification,
    )


def _rt_state():
    return route_runtime._state[_ROUTE]


async def _mk_waiting_with_notification(set_at=_SET_AT):
    # RUNNING (user turn) → open a non-interactive tool → notification commits.
    await route_runtime.ingest_transcript_event(_ROUTE, _evt("user"))
    await route_runtime.ingest_transcript_event(
        _ROUTE,
        TranscriptLifecycleEvent(
            role="assistant",
            block_type="tool_use",
            tool_use_id="wf-1",
            tool_name="Workflow",
            stop_reason=None,
            timestamp=None,
            is_task_notification=False,
        ),
    )
    result = await route_runtime.mark_notification_pending(
        _ROUTE, set_at=set_at, generation="g1"
    )
    assert route_runtime.snapshot(_ROUTE).notification_pending is True
    return result


async def test_task_notification_newer_clears_notification_ts_qualified():
    """A task-notification (the synthetic event's stamp) with an event ts
    STRICTLY NEWER than set_at clears the bit (reason TASK_NOTIFICATION)."""
    await _mk_waiting_with_notification()
    await route_runtime.ingest_transcript_event(
        _ROUTE, _evt("user", timestamp=_SET_AT + 10, is_task_notification=True)
    )
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is False
    assert snap.notification_clear_reason is NotificationClearReason.TASK_NOTIFICATION


async def test_task_notification_older_preserves_notification():
    """An OLDER (or equal) event ts must NOT clear the bit — a buffered
    pre-notification flush must not re-hide the wait."""
    await _mk_waiting_with_notification()
    await route_runtime.ingest_transcript_event(
        _ROUTE, _evt("user", timestamp=_SET_AT - 10, is_task_notification=True)
    )
    assert route_runtime.snapshot(_ROUTE).notification_pending is True


async def test_task_notification_preserves_tombstones_pane_bit_stash():
    """The synthetic (machine-initiated) event preserves done tombstones, the
    pane bit, and the suspended-tools stash (an agent finishing proves nothing
    about a live picker / pending approval)."""
    await route_runtime.ingest_transcript_event(_ROUTE, _evt("user"))
    st = _rt_state()
    st.background_agents_done.add("agentkey1")
    st.pane_interactive_pending = True
    st.suspended_tools["t9"] = False
    await route_runtime.ingest_transcript_event(
        _ROUTE, _evt("user", timestamp=100.0, is_task_notification=True)
    )
    st = _rt_state()
    assert "agentkey1" in st.background_agents_done
    assert st.pane_interactive_pending is True
    assert "t9" in st.suspended_tools


async def test_genuine_user_text_takes_genuine_branch_contrast():
    """Contrast pin: a GENUINE (non-envelope) user text clears notification
    UNCONDITIONALLY and POPS done tombstones / stash / pane bit — the exact
    behavior the malformed-envelope parity guard exists to avoid triggering."""
    await _mk_waiting_with_notification()
    st = _rt_state()
    st.background_agents_done.add("agentkey1")
    st.pane_interactive_pending = True
    st.suspended_tools["t9"] = False
    await route_runtime.ingest_transcript_event(
        _ROUTE, _evt("user", timestamp=_SET_AT - 999, is_task_notification=False)
    )
    snap = route_runtime.snapshot(_ROUTE)
    assert snap.notification_pending is False  # unconditional (ts-agnostic)
    st = _rt_state()
    assert st.background_agents_done == set()
    assert st.pane_interactive_pending is False
    assert st.suspended_tools == {}


# ══════════════════════════════════════════════════════════════════════════
#  Item 9 — cross-tick + duplicate idempotency
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cross_tick_enqueue_then_user_delivery_idempotent(monitor, tmp_path):
    """The same id delivered across TWO poll batches (enqueue in one, user
    delivery in the next) collects the completion each tick without crashing or
    double-collecting — the downstream done-mark is set-valued (idempotent)."""
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)

    _append(parent_jsonl, [_real_queue_close("b5y24hagb")])
    await monitor.check_for_updates({PARENT})
    rec1 = monitor.pop_sidechain_activity()[PARENT]
    assert rec1.completed == {"b5y24hagb"}

    _append(parent_jsonl, [_real_user_delivery("b5y24hagb")])
    await monitor.check_for_updates({PARENT})
    rec2 = monitor.pop_sidechain_activity()[PARENT]
    assert rec2.completed == {"b5y24hagb"}


@pytest.mark.asyncio
async def test_duplicate_enqueue_only_same_id_idempotent(monitor, tmp_path):
    """Real duplicate ENQUEUE-only pairs exist in CC transcripts (b4ey2yxbc
    968/970). Two enqueue lines for one id in a single batch collapse to one
    completed key (set-valued)."""
    parent_jsonl, _ = _setup_parent(monitor, tmp_path)
    _append(
        parent_jsonl,
        [_real_queue_close("bihtr1tc7"), _real_queue_close("bihtr1tc7")],
    )
    await monitor.check_for_updates({PARENT})
    rec = monitor.pop_sidechain_activity()[PARENT]
    assert rec.completed == {"bihtr1tc7"}
