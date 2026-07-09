"""GH #46 PR-1 — teammate ``idle_notification`` predicate + parser tests.

Pins the two leaf primitives that let the bridge tombstone an agent-teams
teammate's background key when the teammate parks (reports idle) instead of
stranding typing/Busy for up to 2 h:

  - ``utils.is_teammate_message`` — the byte-0-anchored envelope predicate,
    backed by the SHARED bounded scanner ``teammate_envelope_payloads``
    (review P2: predicate/parser structural parity; the 64 KiB bound is UTF-8
    BYTES, and a close token inside the opening tag's quoted attributes never
    counts). Fail-closed to genuine-user on any drift.
  - ``response_builder.parse_teammate_idle_notifications`` — the strict
    per-envelope parser returning EVERY idle notification (review P1: one
    parent entry can carry multiple envelopes; each yields name + park_ts +
    park_ts_unparseable).

Uses the real sanitized captures in ``fixtures/teammate_idle_notification_
v2.1.197.jsonl`` (Claude Code 2.1.197 agent-teams).
"""

from __future__ import annotations

import json
from pathlib import Path

from cctelegram.handlers.response_builder import (
    is_teammate_message,
    parse_teammate_idle_notifications,
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
        for ln in (_FIXTURES / "teammate_idle_notification_v2.1.197.jsonl")
        .read_text()
        .splitlines()
    ]


def _bare_text() -> str:
    # line 1 in the fixture: a bare idle_notification (no summary field).
    return _content_str(_fixture_lines()[1])


def _multi_text() -> str:
    # line 2: TWO envelopes — skill-inventory (with a ``summary`` field) then
    # explore-skill-dispatch (bare). The real two-envelope entry Codex cited
    # (outer ts 2026-07-09T15:56:55.336Z).
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


def test_multi_envelope_fixture_is_teammate_message():
    assert is_teammate_message(_multi_text()) is True


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
    """An envelope whose payload+close only complete BEYOND the 64 KiB bound
    rejects (the truncated payload never raw_decodes)."""
    pad = "x" * (TEAMMATE_ENVELOPE_SCAN_BYTES + 10)
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="x" color="red">\n'
        '{"type":"idle_notification","from":"x","pad":"' + pad + '"}\n'
        "</teammate-message>"
    )
    assert is_teammate_message(text) is False
    assert parse_teammate_idle_notifications(text) == []


def test_multibyte_utf8_close_beyond_byte_bound_rejects():
    """Review r2 P2a (Hermes repro): the bound is UTF-8 BYTES, not characters.
    43,650 '€' (3 bytes each) = 130,950 bytes but only ~43.7k chars — a
    CHARACTER-sliced bound would keep the whole envelope in view and accept."""
    pad = "€" * 43_650
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="x" color="red">\n'
        '{"type":"idle_notification","from":"x","pad":"' + pad + '"}\n'
        "</teammate-message>"
    )
    assert len(text.encode("utf-8")) > TEAMMATE_ENVELOPE_SCAN_BYTES
    assert len(text) < TEAMMATE_ENVELOPE_SCAN_BYTES  # the char-slice trap
    assert is_teammate_message(text) is False
    assert parse_teammate_idle_notifications(text) == []


def test_close_tag_only_inside_opening_tag_quoted_attribute_rejects():
    """Review P2b: a close token inside the opening tag's quoted attribute text
    must NOT satisfy the close — the old ``close in prefix`` check accepted
    this where the parser returned None (the verified divergence)."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="x" note="</teammate-message>">\n'
        '{"type":"idle_notification","from":"x"}\n'
    )
    assert is_teammate_message(text) is False
    assert parse_teammate_idle_notifications(text) == []


def test_opening_tag_never_completes_rejects():
    """A tag whose only unquoted '>' belongs to an embedded close token leaves
    no payload after 'completion' — no structurally-valid envelope exists."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="x" broken </teammate-message>'
    )
    assert is_teammate_message(text) is False
    assert parse_teammate_idle_notifications(text) == []


def test_quoted_gt_in_never_completed_tag_rejects_and_no_park():
    """Review r2 P2(i) (Hermes repro): the old quote-blind ``find('>')`` took a
    ``>`` INSIDE a quoted attribute as the tag completion — accepting a
    never-completed opening tag AND producing a park. The quote-aware scan
    skips it; the tag never genuinely completes → False, no park."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="x" note="a > b"\n'
        '{"type":"idle_notification","from":"x","timestamp":"2026-07-09T00:00:00Z"}\n'
        "</teammate-message>"
    )
    assert is_teammate_message(text) is False
    assert parse_teammate_idle_notifications(text) == []


def test_attribute_with_gt_and_close_token_rejects():
    """Review r2 P2 (Codex repro): an attribute value carrying
    ``"> x </teammate-message>"`` made the old predicate True while the parser
    returned [] (divergence). Quote-aware: both the ``>`` and the close token
    inside the quotes are skipped; with no genuine close after the payload →
    False on BOTH."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="x" note="> x </teammate-message>">\n'
        '{"type":"idle_notification","from":"x"}\n'
    )
    assert is_teammate_message(text) is False
    assert parse_teammate_idle_notifications(text) == []


def test_malformed_tag_name_delimiter_rejects():
    """Review r2 P2(ii): the char after ``<teammate-message`` must be
    whitespace or ``>`` — ``\\b`` accepted ``<teammate-message!broken>``."""
    text = (
        "Another Claude session sent a message:\n"
        "<teammate-message!broken>\n"
        '{"type":"idle_notification","from":"x"}\n'
        "</teammate-message>"
    )
    assert is_teammate_message(text) is False
    assert parse_teammate_idle_notifications(text) == []


def test_json_string_quoting_close_tag_parses_correctly():
    """Review r2 P2(iii) — the raw_decode robustness win: a literal
    ``</teammate-message>`` INSIDE a JSON string no longer terminates the
    envelope; the summary-quoting park parses with the correct name/ts."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="quoter" color="red">\n'
        '{"type":"idle_notification","from":"quoter",'
        '"timestamp":"2026-07-09T00:00:00Z",'
        '"summary":"note: </teammate-message> is the close tag"}\n'
        "</teammate-message>\n\ntrailing prose"
    )
    assert is_teammate_message(text) is True
    parsed = parse_teammate_idle_notifications(text)
    assert len(parsed) == 1
    assert parsed[0].name == "quoter"
    assert parsed[0].park_ts == parse_iso_timestamp("2026-07-09T00:00:00Z")
    assert parsed[0].park_ts_unparseable is False


def test_attribute_containing_lt_rejects_fail_closed():
    """r4 SUPERSEDES the r2 accept: a raw '<' ANYWHERE before the completing
    '>' — including inside quoted attribute text — rejects the opener.
    Legitimate CC-generated attribute values (teammate_id, color) never
    contain '<', so an in-quote '<' is always evidence the scan crossed into a
    following tag; accepting it is exactly the state machine the unterminated-
    quote repro below exploits. Fail-closed (was accepted in r2/r3)."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message a="</teammate-message>" b="x">\n'
        '{"type":"idle_notification","from":"peer","timestamp":"2026-07-09T00:00:00Z"}\n'
        "</teammate-message>"
    )
    assert is_teammate_message(text) is False
    assert parse_teammate_idle_notifications(text) == []


def test_unterminated_quote_never_swallows_tag_boundary():
    """Hermes r4 P2 repro (verbatim shape): an UNTERMINATED quoted attribute
    kept the r3 scanner inside quote state across a later tag boundary; the
    line-3 quote char flipped the state closed and the unquoted '>' completed
    the opener there — the immediate-start rule then decoded the FOREIGN JSON
    into predicate True + TeammateIdle(name='foreign-z', park_ts=None,
    unparseable=True): an unconditional tombstone from human-shaped text. The
    in-quote '<' of the line-2 close tag now rejects the opener."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="x\n'
        "</teammate-message>\n"
        '">\n'
        '{"type":"idle_notification","from":"foreign-z"}\n'
        "</teammate-message>"
    )
    assert is_teammate_message(text) is False
    assert parse_teammate_idle_notifications(text) == []


# ── parse_teammate_idle_notifications ───────────────────────────────────


def test_parse_bare_happy_path():
    parsed = parse_teammate_idle_notifications(_bare_text())
    assert len(parsed) == 1
    idle = parsed[0]
    assert idle.name == "skill-inventory"
    assert idle.park_ts == parse_iso_timestamp("2026-07-09T15:55:48.387Z")
    assert idle.park_ts_unparseable is False


def test_parse_multi_envelope_returns_all():
    """Review P1 (REAL-DATA VERIFIED): one parent entry carries TWO envelopes;
    the SECOND names the teammate whose final leg ends stop_reason=None (its
    ONLY close signal) — dropping it reproduces the original 2 h strand."""
    parsed = parse_teammate_idle_notifications(_multi_text())
    assert len(parsed) == 2
    first, second = parsed
    assert first.name == "skill-inventory"  # the with-summary variant
    assert first.park_ts == parse_iso_timestamp("2026-07-09T15:56:41.351Z")
    assert first.park_ts_unparseable is False
    assert second.name == "explore-skill-dispatch"
    assert second.park_ts == parse_iso_timestamp("2026-07-09T15:56:45.564Z")
    assert second.park_ts_unparseable is False


def test_parse_non_teammate_returns_empty():
    assert parse_teammate_idle_notifications("just text") == []
    assert parse_teammate_idle_notifications(_spawn_text()) == []


def _envelope(payload: str) -> str:
    return (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="x" color="red">\n'
        + payload
        + "\n</teammate-message>\n\ntrailing prose"
    )


def test_parse_malformed_inner_json_skips():
    assert parse_teammate_idle_notifications(_envelope("{not valid json,,,}")) == []


def test_parse_missing_from_skips():
    payload = '{"type":"idle_notification","timestamp":"2026-07-09T00:00:00Z"}'
    assert parse_teammate_idle_notifications(_envelope(payload)) == []


def test_parse_wrong_type_skips():
    payload = '{"type":"something_else","from":"x","timestamp":"2026-07-09T00:00:00Z"}'
    assert parse_teammate_idle_notifications(_envelope(payload)) == []


def test_parse_missing_timestamp_is_unparseable():
    payload = '{"type":"idle_notification","from":"peer"}'
    parsed = parse_teammate_idle_notifications(_envelope(payload))
    assert len(parsed) == 1
    assert parsed[0].name == "peer"
    assert parsed[0].park_ts is None
    assert parsed[0].park_ts_unparseable is True


def test_parse_present_but_unparseable_timestamp():
    payload = '{"type":"idle_notification","from":"peer","timestamp":"not-a-date"}'
    parsed = parse_teammate_idle_notifications(_envelope(payload))
    assert len(parsed) == 1
    assert parsed[0].name == "peer"
    assert parsed[0].park_ts is None
    assert parsed[0].park_ts_unparseable is True


def test_parse_non_json_body_stops_enumeration():
    """Codex r3 P1 pinned semantics: the payload must start IMMEDIATELY after
    the tag completion, so a brace-less non-JSON body is structurally invalid
    and STOPS enumeration — the SAME stop-on-invalid rule as the
    undecodable-payload case (consistent, conservative); a later valid
    envelope in the same entry is not reached. The old free-ranging find
    CROSSED the boundary and borrowed the second envelope's JSON."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="x">\nnot json at all\n</teammate-message>\n'
        "\n"
        '<teammate-message teammate_id="y">\n'
        '{"type":"idle_notification","from":"peer-two",'
        '"timestamp":"2026-07-09T00:00:00Z"}\n'
        "</teammate-message>\n\ntrailing prose"
    )
    assert parse_teammate_idle_notifications(text) == []
    assert is_teammate_message(text) is False


def test_json_search_never_crosses_envelope_boundary():
    """Codex r3 P1 (verified repro, verbatim): a non-JSON envelope body
    followed by FOREIGN JSON outside the envelope must not be borrowed — the
    old ``find("{")`` yielded predicate True + TeammateIdle(name="z",
    unparseable=True): genuine-user text classified machine-initiated AND an
    unconditional-tombstone park for a teammate the envelope never named."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="x">\n'
        "not json\n"
        "</teammate-message>\n"
        'random {"type":"idle_notification","from":"z"}\n'
        "</teammate-message>"
    )
    assert is_teammate_message(text) is False
    assert parse_teammate_idle_notifications(text) == []


def test_broken_opener_never_borrows_later_open_tags_gt():
    """Hermes r3 P2 repro 1 (verbatim): a malformed FIRST opener (no '>')
    followed by a VALID envelope — the completion scan must stop at the
    unquoted '<' of the later tag instead of borrowing its '>' (which yielded
    predicate True + a peer-two park from the foreign envelope)."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="x"\n'
        '<teammate-message teammate_id="y">\n'
        '{"type":"idle_notification","from":"peer-two",'
        '"timestamp":"2026-07-09T00:00:00Z"}\n'
        "</teammate-message>"
    )
    assert is_teammate_message(text) is False
    assert parse_teammate_idle_notifications(text) == []


def test_broken_opener_never_borrows_close_tags_gt():
    """Hermes r3 P2 repro 2 (verbatim): a malformed opener followed by a close
    tag, foreign JSON, and another close — the old scan borrowed the close
    tag's '>' as the completion and decoded the foreign JSON into an
    UNPARSEABLE 'z' park (an unconditional tombstone from human-shaped
    text). The unquoted '<' now rejects the opener."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="x"\n'
        "</teammate-message>\n"
        '{"type":"idle_notification","from":"z"}\n'
        "</teammate-message>"
    )
    assert is_teammate_message(text) is False
    assert parse_teammate_idle_notifications(text) == []


def test_parse_undecodable_first_envelope_stops_enumeration():
    """r2 documented degradation: an UNDECODABLE JSON payload (raw_decode
    raises) has no reliable envelope end, so enumeration STOPS — a later valid
    envelope in the same entry is not reached (fail-closed; real envelopes are
    machine-generated JSON, so this shape requires corruption)."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="x">\n{broken json,,,\n</teammate-message>\n'
        "\n"
        '<teammate-message teammate_id="y">\n'
        '{"type":"idle_notification","from":"peer-two",'
        '"timestamp":"2026-07-09T00:00:00Z"}\n'
        "</teammate-message>\n\ntrailing prose"
    )
    assert parse_teammate_idle_notifications(text) == []
    assert is_teammate_message(text) is False


# ── the never-tombstoned shape (defect B premise, real data) ─────────────


def test_sidechain_final_leg_ends_without_a_turn_end_reason():
    """Defect (B): a teammate leg's LAST assistant entry is plain text with
    ``stop_reason=None`` — so the sidechain-done detector (``_TURN_END_REASONS``)
    NEVER fires, and (teammates emit no ``<task-notification>``) the key would
    strand to the 2 h TTL without the park-close lane. Pinned against the real
    sanitized sidechain-leg capture (Claude Code 2.1.197)."""
    from cctelegram.session_monitor import _TURN_END_REASONS

    lines = [
        json.loads(ln)
        for ln in (_FIXTURES / "teammate_sidechain_final_leg_v2.1.197.jsonl")
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
