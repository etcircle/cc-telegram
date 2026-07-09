"""GH #46 PR-1 — teammate ``idle_notification`` predicate + parser tests.

Pins the two leaf primitives that let the bridge tombstone an agent-teams
teammate's background key when the teammate parks (reports idle) instead of
stranding typing/Busy for up to 2 h:

  - ``utils.is_teammate_message`` — the byte-0-anchored envelope predicate
    (line 1 exactly ``Another Claude session sent a message:``, an opening
    ``<teammate-message`` tag, and a ``</teammate-message>`` close within the
    64 KiB scan bound). Fail-closed to genuine-user on any drift.
  - ``response_builder.parse_teammate_idle_notification`` — the strict-or-None
    inner-payload parser (name + park_ts + park_ts_unparseable).

Uses the real sanitized captures in ``fixtures/teammate_idle_notification_
v2.1.204.jsonl`` (Claude Code 2.1.204 agent-teams).
"""

from __future__ import annotations

import json
from pathlib import Path

from cctelegram.handlers.response_builder import (
    is_teammate_message,
    parse_teammate_idle_notification,
)
from cctelegram.utils import TEAMMATE_ENVELOPE_SCAN_BYTES, parse_iso_timestamp

_FIXTURES = Path(__file__).parent / "fixtures"


def _content_str(obj: dict) -> str:
    c = obj["message"]["content"]
    if isinstance(c, str):
        return c
    for b in c:
        if isinstance(b, dict) and b.get("type") == "text":
            return b.get("text", "")
    return ""


def _fixture_lines() -> list[dict]:
    return [
        json.loads(ln)
        for ln in (_FIXTURES / "teammate_idle_notification_v2.1.204.jsonl")
        .read_text()
        .splitlines()
    ]


def _bare_text() -> str:
    # line 1 in the fixture: a bare idle_notification (no summary field).
    return _content_str(_fixture_lines()[1])


def _summary_text() -> str:
    # line 2: an idle_notification whose JSON carries a ``summary`` field.
    return _content_str(_fixture_lines()[2])


def _spawn_text() -> str:
    # line 0: the teammate_spawned Agent tool_result (a tool_result block, not a
    # teammate-message envelope).
    tr = _fixture_lines()[0]["message"]["content"]
    for b in tr:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            c = b.get("content")
            return c if isinstance(c, str) else json.dumps(c)
    return ""


# ── is_teammate_message ─────────────────────────────────────────────────


def test_bare_fixture_is_teammate_message():
    assert is_teammate_message(_bare_text()) is True


def test_summary_fixture_is_teammate_message():
    assert is_teammate_message(_summary_text()) is True


def test_task_notification_is_not_teammate_message():
    task = (
        "<task-notification>\n<task-id>abc123</task-id>\n"
        "<status>completed</status>\n</task-notification>"
    )
    assert is_teammate_message(task) is False


def test_plain_text_is_not_teammate_message():
    assert is_teammate_message("just a normal user prompt") is False
    assert is_teammate_message("") is False


def test_truncated_envelope_no_closing_tag_rejects():
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="x" color="red">\n'
        '{"type":"idle_notification","from":"x","timestamp":"2026-07-09T00:00:00Z"}'
        # NOTE: no </teammate-message> close.
    )
    assert is_teammate_message(text) is False


def test_prefix_mid_text_not_byte_zero_rejects():
    text = (
        "some preamble line\n"
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="x">\n{}\n</teammate-message>'
    )
    assert is_teammate_message(text) is False


def test_leading_bom_rejects():
    assert is_teammate_message("﻿" + _bare_text()) is False


def test_leading_space_rejects():
    assert is_teammate_message(" " + _bare_text()) is False


def test_crlf_line_endings_accepted():
    text = (
        "Another Claude session sent a message:\r\n"
        '<teammate-message teammate_id="x" color="red">\r\n'
        '{"type":"idle_notification","from":"x","timestamp":"2026-07-09T00:00:00Z"}'
        "\r\n</teammate-message>"
    )
    assert is_teammate_message(text) is True


def test_envelope_closing_beyond_scan_bound_rejects():
    filler = "x" * (TEAMMATE_ENVELOPE_SCAN_BYTES + 10)
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="x" color="red">\n'
        + filler
        + "\n</teammate-message>"
    )
    assert is_teammate_message(text) is False


# ── parse_teammate_idle_notification ────────────────────────────────────


def test_parse_bare_happy_path():
    idle = parse_teammate_idle_notification(_bare_text())
    assert idle is not None
    assert idle.name == "skill-inventory"
    assert idle.park_ts == parse_iso_timestamp("2026-07-09T15:55:48.387Z")
    assert idle.park_ts_unparseable is False


def test_parse_summary_happy_path():
    idle = parse_teammate_idle_notification(_summary_text())
    assert idle is not None
    # Parses the FIRST envelope only (skill-inventory, with a summary field).
    assert idle.name == "skill-inventory"
    assert idle.park_ts == parse_iso_timestamp("2026-07-09T15:56:41.351Z")
    assert idle.park_ts_unparseable is False


def test_parse_non_teammate_returns_none():
    assert parse_teammate_idle_notification("just text") is None
    assert parse_teammate_idle_notification(_spawn_text()) is None


def _envelope(payload: str) -> str:
    return (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="x" color="red">\n'
        + payload
        + "\n</teammate-message>\n\ntrailing prose"
    )


def test_parse_malformed_inner_json_returns_none():
    assert parse_teammate_idle_notification(_envelope("{not valid json,,,}")) is None


def test_parse_missing_from_returns_none():
    payload = '{"type":"idle_notification","timestamp":"2026-07-09T00:00:00Z"}'
    assert parse_teammate_idle_notification(_envelope(payload)) is None


def test_parse_wrong_type_returns_none():
    payload = '{"type":"something_else","from":"x","timestamp":"2026-07-09T00:00:00Z"}'
    assert parse_teammate_idle_notification(_envelope(payload)) is None


def test_parse_missing_timestamp_is_unparseable():
    payload = '{"type":"idle_notification","from":"peer"}'
    idle = parse_teammate_idle_notification(_envelope(payload))
    assert idle is not None
    assert idle.name == "peer"
    assert idle.park_ts is None
    assert idle.park_ts_unparseable is True


def test_parse_present_but_unparseable_timestamp():
    payload = '{"type":"idle_notification","from":"peer","timestamp":"not-a-date"}'
    idle = parse_teammate_idle_notification(_envelope(payload))
    assert idle is not None
    assert idle.name == "peer"
    assert idle.park_ts is None
    assert idle.park_ts_unparseable is True


# ── the never-tombstoned shape (defect B premise, real data) ─────────────


def test_sidechain_final_leg_ends_without_a_turn_end_reason():
    """Defect (B): a teammate leg's LAST assistant entry is plain text with
    ``stop_reason=None`` — so the sidechain-done detector (``_TURN_END_REASONS``)
    NEVER fires, and (teammates emit no ``<task-notification>``) the key would
    strand to the 2 h TTL without the park-close lane. Pinned against the real
    sanitized sidechain-leg capture."""
    from cctelegram.session_monitor import _TURN_END_REASONS

    lines = [
        json.loads(ln)
        for ln in (_FIXTURES / "teammate_sidechain_final_leg_v2.1.204.jsonl")
        .read_text()
        .splitlines()
    ]
    final = lines[-1]
    assert final["type"] == "assistant"
    assert final["message"]["stop_reason"] is None
    assert final["message"]["stop_reason"] not in _TURN_END_REASONS
    # The leg's SendMessage-to-main + final text carry the teammate's agentId,
    # whose normalized key is the a<name>-<hex> park-close key shape.
    assert final["agentId"] == "aexplore-skill-dispatch-23a8cdc461b7635f"
