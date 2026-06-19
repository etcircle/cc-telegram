"""Fix 2a unit tests — Workflow-launch task-id / run-id / transcript-dir parser.

ISSUE-6: the Workflow tool's launch tool_result has a DIFFERENT shape from the
Agent/Task `agentId:` launch. Verified against 34 real launches in the project
JSONL history (PII-scrubbed below): the Task ID is MID-LINE
(``Workflow launched in background. Task ID: <id>``), the id is the last token
on its line, and Task ID (``w13z7jqx6``) ≠ Run ID (``wf_54f46aea-ba6``).

The load-bearing invariant: the captured Task ID must EQUAL the
``<task-notification>`` close key (``extract_task_notification_task_id``) so the
``wf-task:<id>`` launch key == close key (the bracket opens AND closes). The
plan's ``^\\s*Task ID:`` regex (line-start anchored) would match NOTHING on the
real mid-line shape — these tests pin the corrected parser.
"""

from __future__ import annotations

import pytest

from cctelegram.handlers.response_builder import (
    extract_task_notification_task_id,
    extract_workflow_launch_info,
    extract_workflow_launch_task_id,
)

# A REAL launch tool_result shape (PII-scrubbed: synthetic ids + path). The
# parser must tolerate the transcript renderer's possible ⎿/indent prefix, so
# the Task ID is mid-line and the id is bounded by line end.
_REAL_LAUNCH = (
    "Workflow launched in background. Task ID: wtask01abc\n"
    "Summary: Map seams for the busy-signal wave\n"
    "Transcript dir: /home/u/.claude/projects/SID/subagents/workflows/wf_run01abcd-ef0\n"
    "Script file: /home/u/.claude/projects/SID/workflows/scripts/x-wf_run01abcd-ef0.js\n"
    '(Edit this file with Write/Edit and re-invoke Workflow with {scriptPath: "..."}.)\n'
    "Run ID: wf_run01abcd-ef0\n"
    'To resume after editing the script: Workflow({scriptPath: "...", '
    'resumeFromRunId: "wf_run01abcd-ef0"}) — completed agents return cached results.\n'
    "\n"
    "You will be notified when it completes. Use /workflows to watch live progress."
)


def _close(task_id: str) -> str:
    return (
        "<task-notification>\n"
        f"<task-id>{task_id}</task-id>\n"
        "<tool-use-id>toolu_x</tool-use-id>\n<status>completed</status>\n"
        "</task-notification>"
    )


def test_real_launch_shape_task_id_is_extracted_midline():
    """THE premise test: Task ID is mid-line after 'Workflow launched in
    background. ' — a line-start-anchored regex fails this."""
    assert extract_workflow_launch_task_id(_REAL_LAUNCH) == "wtask01abc"


def test_real_launch_info_captures_run_id_and_transcript_dir():
    info = extract_workflow_launch_info(_REAL_LAUNCH)
    assert info is not None
    assert info.task_id == "wtask01abc"
    assert info.run_id == "wf_run01abcd-ef0"
    assert info.transcript_dir is not None
    assert info.transcript_dir.endswith("subagents/workflows/wf_run01abcd-ef0")


def test_indented_launch_line_still_matches():
    """The transcript parser renders tool_result content indented under ⎿."""
    indented = "  ⎿ " + _REAL_LAUNCH
    assert extract_workflow_launch_task_id(indented) == "wtask01abc"


@pytest.mark.parametrize(
    "rendered",
    [
        "Workflow launched in background. Task ID: wtask01abc",
        "Workflow launched in background. Task ID: `wtask01abc`",
        "Workflow launched in background. Task ID: wtask01abc.",
        "Workflow launched in background. Task ID: `wtask01abc`.",
    ],
)
def test_four_punctuation_shapes_capture_only_the_id(rendered: str):
    assert extract_workflow_launch_task_id(rendered) == "wtask01abc"


@pytest.mark.parametrize(
    "rendered",
    [
        "Workflow launched in background. Task ID: wtask01abc",
        "Workflow launched in background. Task ID: `wtask01abc`",
        "Workflow launched in background. Task ID: wtask01abc.",
        "Workflow launched in background. Task ID: `wtask01abc`.",
    ],
)
def test_launch_key_equals_task_notification_close_key(rendered: str):
    """The bracket open/close parity invariant: launch key == close key across
    all four shapes (so wf-task:<launch> == wf-task:<close>)."""
    launch_id = extract_workflow_launch_task_id(rendered)
    close_id = extract_task_notification_task_id(_close("wtask01abc"))
    assert launch_id == close_id == "wtask01abc"


def test_no_task_id_returns_none():
    assert extract_workflow_launch_task_id("Workflow launched in background.") is None
    assert extract_workflow_launch_info("nothing here") is None


def test_run_id_is_not_mistaken_for_task_id():
    """'Run ID:' must never be captured as the Task ID (different identifier)."""
    info = extract_workflow_launch_info(_REAL_LAUNCH)
    assert info is not None
    assert info.task_id != info.run_id
    assert info.task_id == "wtask01abc"


def test_non_workflow_transcript_dir_is_dropped():
    """A transcript dir not under subagents/workflows/wf_ is not kept (only a
    real wf dir feeds the Fix 2c mtime heartbeat)."""
    text = (
        "Workflow launched in background. Task ID: wtask01abc\n"
        "Transcript dir: /home/u/.claude/projects/SID/some/other/path\n"
        "Run ID: wf_run01abcd-ef0\n"
    )
    info = extract_workflow_launch_info(text)
    assert info is not None
    assert info.task_id == "wtask01abc"
    assert info.transcript_dir is None


# ── PR-2: structured ``toolUseResult`` parser (workflow_launch_info_from_meta) ─
#
# The Workflow launch's ENTRY-level ``toolUseResult`` dict is the robust,
# drift-proof anchor (verified entry-level on 40/40 real launches; the prose
# regex above is the fallback). Critically the Agent/Task ``run_in_background``
# launch ALSO carries ``status="async_launched"`` but a DIFFERENT shape
# (``agentId``, no ``taskId``) — so the parser must key on the Workflow fields,
# never on ``status`` alone.

# A REAL Workflow ``toolUseResult`` shape (PII-scrubbed). ``Path(transcriptDir)
# .name == runId`` and the dir is under ``subagents/workflows/wf_`` (verified on
# all 40 real launches).
_STRUCT_META = {
    "status": "async_launched",
    "taskId": "wtask01abc",
    "runId": "wf_run01abcd-ef0",
    "summary": "Map seams for the busy-signal wave",
    "transcriptDir": (
        "/home/u/.claude/projects/SID/subagents/workflows/wf_run01abcd-ef0"
    ),
    "scriptPath": "/home/u/.claude/projects/SID/workflows/scripts/x.js",
}


def test_structured_meta_parses_task_run_and_dir():
    from cctelegram.handlers.response_builder import workflow_launch_info_from_meta

    info = workflow_launch_info_from_meta(_STRUCT_META)
    assert info is not None
    assert info.task_id == "wtask01abc"
    assert info.run_id == "wf_run01abcd-ef0"
    assert info.transcript_dir is not None
    assert info.transcript_dir.endswith("subagents/workflows/wf_run01abcd-ef0")


def test_structured_meta_newer_shape_with_tasktype_workflowname():
    """The newer (12/40) Workflow shape adds ``taskType`` / ``workflowName`` —
    extra keys must be tolerated (we read only task/run/dir)."""
    from cctelegram.handlers.response_builder import workflow_launch_info_from_meta

    meta = dict(_STRUCT_META, taskType="verify", workflowName="my-wf")
    info = workflow_launch_info_from_meta(meta)
    assert info is not None
    assert info.task_id == "wtask01abc"


def test_structured_meta_dir_name_equals_run_id_invariant():
    """``Path(transcriptDir).name == runId`` (the Half-B matching premise; the
    wf_dir basename IS the run id)."""
    from pathlib import Path

    from cctelegram.handlers.response_builder import workflow_launch_info_from_meta

    info = workflow_launch_info_from_meta(_STRUCT_META)
    assert info is not None and info.transcript_dir is not None
    assert Path(info.transcript_dir).name == info.run_id


def test_structured_meta_agent_shape_returns_none():
    """The Agent/Task ``run_in_background`` async launch ALSO has
    ``status="async_launched"`` but carries ``agentId`` (no ``taskId``) — it must
    NOT be parsed as a Workflow launch (keying on ``status`` alone would be a
    bug)."""
    from cctelegram.handlers.response_builder import workflow_launch_info_from_meta

    agent_meta = {
        "status": "async_launched",
        "agentId": "abc123def456",
        "isAsync": True,
        "canReadOutputFile": True,
        "description": "do a thing",
        "outputFile": "/tmp/out",
        "prompt": "...",
    }
    assert workflow_launch_info_from_meta(agent_meta) is None


def test_structured_meta_non_async_status_returns_none():
    from cctelegram.handlers.response_builder import workflow_launch_info_from_meta

    assert (
        workflow_launch_info_from_meta(dict(_STRUCT_META, status="completed")) is None
    )


def test_structured_meta_none_and_non_dict_return_none():
    from cctelegram.handlers.response_builder import workflow_launch_info_from_meta

    assert workflow_launch_info_from_meta(None) is None
    assert workflow_launch_info_from_meta("not a dict") is None
    assert workflow_launch_info_from_meta([]) is None


def test_structured_meta_missing_or_blank_task_id_returns_none():
    from cctelegram.handlers.response_builder import workflow_launch_info_from_meta

    no_tid = dict(_STRUCT_META)
    del no_tid["taskId"]
    assert workflow_launch_info_from_meta(no_tid) is None
    assert workflow_launch_info_from_meta(dict(_STRUCT_META, taskId="")) is None
    assert workflow_launch_info_from_meta(dict(_STRUCT_META, taskId=123)) is None


def test_structured_meta_non_workflow_dir_dropped_to_none():
    """The SAME drop-to-None guard as the prose parser (response_builder.py:138):
    a transcriptDir not under subagents/workflows/wf_ is not kept, but the
    task_id still parses (bracket ages out on the launch-wall TTL)."""
    from cctelegram.handlers.response_builder import workflow_launch_info_from_meta

    m = dict(_STRUCT_META, transcriptDir="/home/u/.claude/projects/SID/some/other")
    info = workflow_launch_info_from_meta(m)
    assert info is not None
    assert info.task_id == "wtask01abc"
    assert info.transcript_dir is None


def test_structured_meta_missing_dir_and_run_are_none():
    """A structured meta with only the status+taskId still yields a bare info
    (run_id/transcript_dir None) — the bracket opens and ages out on TTL."""
    from cctelegram.handlers.response_builder import workflow_launch_info_from_meta

    info = workflow_launch_info_from_meta(
        {"status": "async_launched", "taskId": "wtask01abc"}
    )
    assert info is not None
    assert info.task_id == "wtask01abc"
    assert info.run_id is None
    assert info.transcript_dir is None
