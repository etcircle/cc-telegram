"""typing-unification T1.1 unit tests — the background-Bash launch discriminator.

``response_builder.background_bash_task_id_from_meta`` reads a
``run_in_background`` Bash launch's structured entry-level ``toolUseResult``
(``{... "backgroundTaskId": "<id>"}``) and returns the bare task id — the SAME
id the completion ``<task-notification>`` carries (bare launch/close parity, no
``wf-task:`` prefix).

The load-bearing invariant is DISJOINTNESS: the three async-launch meta shapes
(Agent ``agentId``, Workflow ``taskId``, Bash ``backgroundTaskId``) never
cross-match, so wiring the Bash discriminator into the launch chain can't
mis-key an Agent/Workflow launch and vice-versa. Fixture: the real launch line
(Claude Code 2.1.197) under ``tests/cctelegram/fixtures/``.
"""

from __future__ import annotations

import json
from pathlib import Path

from cctelegram.handlers.response_builder import (
    async_agent_launch_id_from_meta,
    background_bash_task_id_from_meta,
    workflow_launch_info_from_meta,
)

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _real_bash_launch_meta() -> dict:
    """The real structured ``toolUseResult`` from the bg-bash launch fixture."""
    for line in (_FIXTURES / "bg_bash_launch_v2.1.197.jsonl").read_text().splitlines():
        meta = json.loads(line).get("toolUseResult")
        if isinstance(meta, dict) and meta.get("backgroundTaskId"):
            return meta
    raise AssertionError("bg_bash_launch fixture missing backgroundTaskId")


# Shapes for the disjointness matrix (documented real shapes).
_AGENT_META = {"status": "async_launched", "isAsync": True, "agentId": "abc123def456"}
_WF_META = {
    "status": "async_launched",
    "taskId": "w13z7jqx6",
    "runId": "wf_54f46aea",
    "transcriptDir": "/x/subagents/workflows/wf_54f46aea",
}


def test_real_fixture_meta_yields_bare_task_id():
    meta = _real_bash_launch_meta()
    assert background_bash_task_id_from_meta(meta) == "byziqxhyh"


def test_minimal_backgroundtaskid_meta_yields_id():
    assert (
        background_bash_task_id_from_meta({"backgroundTaskId": "byziqxhyh"})
        == "byziqxhyh"
    )


def test_none_and_non_dict_meta_return_none():
    assert background_bash_task_id_from_meta(None) is None
    assert background_bash_task_id_from_meta("Command running in background") is None
    assert background_bash_task_id_from_meta(123) is None
    assert background_bash_task_id_from_meta(["backgroundTaskId"]) is None


def test_absent_or_empty_or_non_str_backgroundtaskid_returns_none():
    # A plain (foreground) Bash tool_result has NO backgroundTaskId.
    assert background_bash_task_id_from_meta({"stdout": "x", "stderr": ""}) is None
    assert background_bash_task_id_from_meta({"backgroundTaskId": ""}) is None
    assert background_bash_task_id_from_meta({"backgroundTaskId": None}) is None
    assert background_bash_task_id_from_meta({"backgroundTaskId": 5}) is None


def test_never_keys_on_status_alone():
    # A dict that merely carries an async-launch status but no backgroundTaskId
    # (an Agent/Workflow-shaped meta) is NOT a background bash.
    assert background_bash_task_id_from_meta({"status": "async_launched"}) is None


# ── disjointness BOTH ways ───────────────────────────────────────────────────


def test_agent_shaped_meta_is_not_a_background_bash():
    assert background_bash_task_id_from_meta(_AGENT_META) is None


def test_workflow_shaped_meta_is_not_a_background_bash():
    assert background_bash_task_id_from_meta(_WF_META) is None


def test_background_bash_meta_is_not_an_agent_launch():
    assert async_agent_launch_id_from_meta(_real_bash_launch_meta()) is None
    assert async_agent_launch_id_from_meta({"backgroundTaskId": "byziqxhyh"}) is None


def test_background_bash_meta_is_not_a_workflow_launch():
    assert workflow_launch_info_from_meta(_real_bash_launch_meta()) is None
    assert workflow_launch_info_from_meta({"backgroundTaskId": "byziqxhyh"}) is None
