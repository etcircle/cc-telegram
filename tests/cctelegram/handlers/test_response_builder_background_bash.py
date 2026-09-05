"""typing-unification T1.1 unit tests — the background-Bash launch discriminator.

``response_builder.background_bash_task_id_from_meta`` reads a
``run_in_background`` Bash launch's structured entry-level ``toolUseResult``
(``{... "backgroundTaskId": "<id>"}``) and returns the bare task id — the SAME
id the completion ``<task-notification>`` carries (bare launch/close parity, no
``wf-task:`` prefix).

The load-bearing invariant is DISJOINTNESS: the async-launch meta shapes
(Agent ``agentId``, Workflow ``taskId``+``status``, Bash ``backgroundTaskId``,
SendMessage ``resumedAgentId``, Monitor ``taskId``+``persistent`` and the
TaskStop close ``task_id``+``task_type`` — GH #92) never cross-match, so wiring
a discriminator into the launch chain can't mis-key another lane's launch. The
tool-name gate is primary; these rows are defence-in-depth. Fixtures: the real
launch lines (Claude Code 2.1.197 Bash, 2.1.257 Monitor/TaskStop) under
``tests/cctelegram/fixtures/``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cctelegram.handlers.response_builder import (
    _canonical_task_id,
    async_agent_launch_id_from_meta,
    background_bash_task_id_from_meta,
    monitor_launch_task_id_from_meta,
    resumed_agent_id_from_meta,
    stopped_task_id_from_meta,
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


# ── GH #92: CC 2.1.257 structured Monitor / TaskStop shapes ─────────────────
# Tool-name gating is primary; these rows pin defence-in-depth and canonical
# close parity. Never accept prose as evidence: false dark over false typing.


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" bm7gmjisu ", "bm7gmjisu"),
        ("a-Z_09", "a-Z_09"),
        ("", None),
        (" \t\n", None),
        ("bm7 gmjisu", None),
        ("x/y", None),
        ("é", None),
        (None, None),
        (1, None),
        (True, None),
    ],
)
def test_gh92_canonical_task_id_and_both_helpers(raw, expected):
    """GH #92 / CC 2.1.257: padded launch and stripped close share one key."""
    assert _canonical_task_id(raw) == expected
    assert (
        monitor_launch_task_id_from_meta({"taskId": raw, "persistent": True})
        == expected
    )
    assert (
        stopped_task_id_from_meta({"task_id": raw, "task_type": "local_bash"})
        == expected
    )


@pytest.mark.parametrize("persistent", [True, False])
def test_gh92_monitor_requires_boolean_persistent(persistent):
    assert (
        monitor_launch_task_id_from_meta(
            {"taskId": "bm7gmjisu", "persistent": persistent}
        )
        == "bm7gmjisu"
    )


@pytest.mark.parametrize(
    "meta",
    [
        None,
        "Monitor started (task bm7gmjisu",
        [],
        1,
        {},
        {"taskId": "bm7gmjisu"},
        {"persistent": True},
        *[{"taskId": "bm7gmjisu", "persistent": p} for p in (None, 0, 1, "true")],
    ],
)
def test_gh92_monitor_malformed_shape_fails_closed(meta):
    assert monitor_launch_task_id_from_meta(meta) is None


@pytest.mark.parametrize(
    "field",
    [
        "status",
        "agentId",
        "backgroundTaskId",
        "resumedAgentId",
        "runId",
        "transcriptDir",
    ],
)
def test_gh92_monitor_refuses_competing_field_even_when_none(field):
    assert (
        monitor_launch_task_id_from_meta(
            {"taskId": "bm7gmjisu", "persistent": True, field: None}
        )
        is None
    )


@pytest.mark.parametrize(
    "meta",
    [
        None,
        [],
        "Successfully stopped task: b9um28ext",
        {},
        {"task_id": "b9um28ext"},
        {"task_type": "local_bash"},
    ],
)
def test_gh92_taskstop_requires_both_keys(meta):
    assert stopped_task_id_from_meta(meta) is None


def test_gh92_taskstop_type_presence_is_not_a_value_whitelist():
    assert (
        stopped_task_id_from_meta({"task_id": "b9um28ext", "task_type": None})
        == "b9um28ext"
    )


@pytest.mark.parametrize(
    "meta",
    [
        _AGENT_META,
        _WF_META,
        {"backgroundTaskId": "byziqxhyh"},
        {"resumedAgentId": "abc123"},
        {"status": "teammate_spawned", "agent_id": "x@team"},
    ],
)
def test_gh92_other_launch_shapes_are_neither_monitor_nor_stop(meta):
    assert monitor_launch_task_id_from_meta(meta) is None
    assert stopped_task_id_from_meta(meta) is None


@pytest.mark.parametrize("index, expected", [(1, "bm7gmjisu"), (5, "b9um28ext")])
def test_gh92_real_monitor_and_taskstop_meta_disjointness(index, expected):
    entries = [
        json.loads(line)
        for line in (_FIXTURES / "monitor_launch_v2.1.257.jsonl")
        .read_text()
        .splitlines()
    ]
    meta = entries[index]["toolUseResult"]
    assert async_agent_launch_id_from_meta(meta) is None
    assert workflow_launch_info_from_meta(meta) is None
    assert background_bash_task_id_from_meta(meta) is None
    assert resumed_agent_id_from_meta(meta) is None
    assert monitor_launch_task_id_from_meta(meta) == (expected if index == 1 else None)
    assert stopped_task_id_from_meta(meta) == (expected if index == 5 else None)
