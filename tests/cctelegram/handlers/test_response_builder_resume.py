"""Fix C (2026-07-08) unit tests — the resumed-agent launch discriminator.

``response_builder.resumed_agent_id_from_meta`` reads a ``SendMessage``
tool_result's structured entry-level ``toolUseResult``
(``{success, message, resumedAgentId}``) and returns the bare agent id — the
FOURTH background-launch source (after Agent ``agentId``, Workflow ``taskId``,
Bash ``backgroundTaskId``). The load-bearing invariant is FOUR-WAY DISJOINTNESS:
a resume meta never cross-matches the other three, and vice-versa, so wiring the
resume discriminator into the launch chain can't mis-key another launch shape.

Fixture: the real ``SendMessage`` resume tool_result (Claude Code 2.1.204),
extracted verbatim from a live parent transcript.
"""

from __future__ import annotations

import json
from pathlib import Path

from cctelegram.handlers.response_builder import (
    async_agent_launch_id_from_meta,
    background_bash_task_id_from_meta,
    resumed_agent_id_from_meta,
    workflow_launch_info_from_meta,
)

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _real_resume_meta() -> dict:
    """The real structured ``toolUseResult`` from the resume fixture."""
    for line in (_FIXTURES / "agent_resume_v2.1.204.jsonl").read_text().splitlines():
        meta = json.loads(line).get("toolUseResult")
        if isinstance(meta, dict) and meta.get("resumedAgentId"):
            return meta
    raise AssertionError("agent_resume fixture missing resumedAgentId")


# Documented real shapes for the four-way disjointness matrix.
_AGENT_META = {"status": "async_launched", "isAsync": True, "agentId": "abc123def456"}
_WF_META = {
    "status": "async_launched",
    "taskId": "w13z7jqx6",
    "runId": "wf_54f46aea",
    "transcriptDir": "/x/subagents/workflows/wf_54f46aea",
}
_BASH_META = {"stdout": "", "stderr": "", "backgroundTaskId": "byziqxhyh"}


def test_real_fixture_meta_yields_resumed_agent_id():
    meta = _real_resume_meta()
    assert resumed_agent_id_from_meta(meta) == "a1951c4043e2c9561"


def test_minimal_resumedagentid_meta_yields_id():
    assert (
        resumed_agent_id_from_meta({"resumedAgentId": "a1951c4043e2c9561"})
        == "a1951c4043e2c9561"
    )


def test_none_and_non_dict_meta_return_none():
    assert resumed_agent_id_from_meta(None) is None
    assert resumed_agent_id_from_meta("resumed from transcript") is None
    assert resumed_agent_id_from_meta(123) is None
    assert resumed_agent_id_from_meta(["resumedAgentId"]) is None


def test_strange_value_resumedagentid_rejected():
    # non-str / empty / whitespace-only → reject (Hermes P3 strange-value pin).
    assert resumed_agent_id_from_meta({"resumedAgentId": ""}) is None
    assert resumed_agent_id_from_meta({"resumedAgentId": "   "}) is None
    assert resumed_agent_id_from_meta({"resumedAgentId": None}) is None
    assert resumed_agent_id_from_meta({"resumedAgentId": 5}) is None
    assert resumed_agent_id_from_meta({"resumedAgentId": ["x"]}) is None


def test_never_keys_on_success_alone():
    # A dict that merely carries success=True but no resumedAgentId is NOT a
    # resume (a plain SendMessage to a fresh agent).
    assert resumed_agent_id_from_meta({"success": True, "message": "delivered"}) is None


# ── four-way disjointness ────────────────────────────────────────────────────


def test_agent_shaped_meta_is_not_a_resume():
    assert resumed_agent_id_from_meta(_AGENT_META) is None


def test_workflow_shaped_meta_is_not_a_resume():
    assert resumed_agent_id_from_meta(_WF_META) is None


def test_bash_shaped_meta_is_not_a_resume():
    assert resumed_agent_id_from_meta(_BASH_META) is None


def test_resume_meta_is_not_an_agent_launch():
    assert async_agent_launch_id_from_meta(_real_resume_meta()) is None
    assert (
        async_agent_launch_id_from_meta({"resumedAgentId": "a1951c4043e2c9561"}) is None
    )


def test_resume_meta_is_not_a_workflow_launch():
    assert workflow_launch_info_from_meta(_real_resume_meta()) is None
    assert (
        workflow_launch_info_from_meta({"resumedAgentId": "a1951c4043e2c9561"}) is None
    )


def test_resume_meta_is_not_a_background_bash():
    assert background_bash_task_id_from_meta(_real_resume_meta()) is None
    assert (
        background_bash_task_id_from_meta({"resumedAgentId": "a1951c4043e2c9561"})
        is None
    )
