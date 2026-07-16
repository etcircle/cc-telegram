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
from cctelegram.utils import (
    TEAMMATE_ENVELOPE_SCAN_BYTES,
    parse_iso_timestamp,
    teammate_envelope_payloads,
)

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


def test_parse_non_json_body_skips_to_next_envelope():
    """GH #57 (INVERTS the r3 ``..._stops_enumeration``): a brace-less non-JSON
    body (case (a) — the markdown-report shape) now runs the GUARDED RESYNC and
    CONTINUES, so a later valid park envelope in the same entry IS reached. The
    resync skips past the report envelope's OWN line-anchored close (no opener
    lies between → ownership guard passes) and yields the ``peer-two`` park;
    predicate True. (The r3 stop-on-invalid rule dropped the trailing park —
    the incident. Foreign JSON BETWEEN envelopes is still never borrowed:
    ``test_json_search_never_crosses_envelope_boundary`` stays green.)"""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="x">\nnot json at all\n</teammate-message>\n'
        "\n"
        '<teammate-message teammate_id="y">\n'
        '{"type":"idle_notification","from":"peer-two",'
        '"timestamp":"2026-07-09T00:00:00Z"}\n'
        "</teammate-message>\n\ntrailing prose"
    )
    parsed = parse_teammate_idle_notifications(text)
    assert len(parsed) == 1
    assert parsed[0].name == "peer-two"
    assert parsed[0].park_ts == parse_iso_timestamp("2026-07-09T00:00:00Z")
    assert is_teammate_message(text) is True


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


def test_parse_undecodable_first_envelope_skips_to_next_envelope():
    """GH #57 (INVERTS the r2 ``..._stops_enumeration``, Codex r3 P2): a
    ``{``-leading but UNDECODABLE body (case (b) — raw_decode raises) now runs
    the guarded resync and CONTINUES, so the trailing ``peer-two`` park IS
    yielded and the predicate is True. This is the in-place twin of the
    brace-leading case-(b) test ``test_brace_leading_report_body_resyncs``."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="x">\n{broken json,,,\n</teammate-message>\n'
        "\n"
        '<teammate-message teammate_id="y">\n'
        '{"type":"idle_notification","from":"peer-two",'
        '"timestamp":"2026-07-09T00:00:00Z"}\n'
        "</teammate-message>\n\ntrailing prose"
    )
    parsed = parse_teammate_idle_notifications(text)
    assert len(parsed) == 1
    assert parsed[0].name == "peer-two"
    assert parsed[0].park_ts == parse_iso_timestamp("2026-07-09T00:00:00Z")
    assert is_teammate_message(text) is True


# ── GH #57: guarded-resync past a report envelope (report+park entries) ───


def test_incident_report_then_park_yields_the_park():
    """GH #57 incident shape (near-verbatim, real entry 2026-07-16): the
    teammate ``aw1a-sat`` finished with ONE parent entry carrying TWO envelopes
    — a markdown REPORT (multi-line body, blank lines, backticks, a ``summary=``
    attribute), then the JSON ``idle_notification`` PARK (the key's ONLY close
    signal). The r3 scanner broke at the report and dropped the park → a 2 h
    strand. The guarded resync now skips the report and yields exactly the
    park."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="aw1a-sat" color="purple" '
        'summary="Re-run already done — S1–S5 all PASS">\n'
        "## Re-run status\n"
        "\n"
        "All checks complete. Details:\n"
        "\n"
        "```\n"
        "S1 PASS\n"
        "S2 PASS\n"
        "```\n"
        "\n"
        "Nothing further needed.\n"
        "</teammate-message>\n"
        '<teammate-message teammate_id="aw1a-sat" color="purple">\n'
        '{"type":"idle_notification","from":"aw1a-sat",'
        '"timestamp":"2026-07-16T03:37:46.169Z","idleReason":"available"}\n'
        "</teammate-message>\n"
        "\n"
        "This came from another Claude session working with you as part of a "
        "team.\n"
    )
    parsed = parse_teammate_idle_notifications(text)
    assert len(parsed) == 1
    assert parsed[0].name == "aw1a-sat"
    assert parsed[0].park_ts == parse_iso_timestamp("2026-07-16T03:37:46.169Z")
    assert parsed[0].park_ts_unparseable is False
    assert is_teammate_message(text) is True


def test_ownership_guard_unclosed_report_swallows_no_park():
    """GH #57 / Codex P2-1 repro (verbatim): an UNCLOSED markdown envelope, then
    envelope y (park), then envelope z (park). The resync from the unclosed
    report finds y's line-anchored close, but y's opener sits BETWEEN the
    report's start and that close — a crossed envelope boundary → the ownership
    guard hard-breaks; z is NOT yielded (fail-closed)."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="report">\n'
        "a markdown report with no close of its own\n"
        '<teammate-message teammate_id="y">\n'
        '{"type":"idle_notification","from":"peer-y",'
        '"timestamp":"2026-07-09T00:00:00Z"}\n'
        "</teammate-message>\n"
        '<teammate-message teammate_id="z">\n'
        '{"type":"idle_notification","from":"peer-z",'
        '"timestamp":"2026-07-09T00:00:01Z"}\n'
        "</teammate-message>"
    )
    assert parse_teammate_idle_notifications(text) == []
    assert is_teammate_message(text) is False


def test_ownership_guard_r2_indented_and_midline_openers():
    """GH #57 / Codex r2 P2: the ownership guard uses the scanner's FULL
    unanchored opener grammar, so (i) an INDENTED y-opener and (ii) a MID-LINE
    ``report <teammate-message …>`` y-opener between the unclosed report and the
    next line-anchored close BOTH hard-break the resync → ``[]`` / False."""
    indented = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="report">\n'
        "unclosed markdown report\n"
        '  <teammate-message teammate_id="y">\n'
        '{"type":"idle_notification","from":"peer-y",'
        '"timestamp":"2026-07-09T00:00:00Z"}\n'
        "</teammate-message>"
    )
    midline = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="report">\n'
        "unclosed markdown report\n"
        'report <teammate-message teammate_id="y">\n'
        '{"type":"idle_notification","from":"peer-y",'
        '"timestamp":"2026-07-09T00:00:00Z"}\n'
        "</teammate-message>"
    )
    for text in (indented, midline):
        assert parse_teammate_idle_notifications(text) == []
        assert is_teammate_message(text) is False


def test_same_line_close_report_then_park_fails_closed():
    """GH #57 / Codex P2-2 (renderer-drift audit surface): a report envelope
    whose close is SAME-LINE (``report </teammate-message>``, not line-anchored)
    followed by a park envelope. The resync ignores the same-line close and
    resyncs onto the PARK's line-anchored close, but the park's opener sits
    between → the ownership guard fires → ``[]`` (pinned fail-closed residual;
    verified 213/213 line-anchored across the corpus, so this shape is
    hypothetical)."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="report">\n'
        "a report ending on its own line </teammate-message>\n"
        '<teammate-message teammate_id="y">\n'
        '{"type":"idle_notification","from":"peer-y",'
        '"timestamp":"2026-07-09T00:00:00Z"}\n'
        "</teammate-message>"
    )
    assert parse_teammate_idle_notifications(text) == []
    assert is_teammate_message(text) is False


def test_brace_leading_report_body_resyncs():
    """GH #57 / Codex P2-3a: a body that LEADS with ``{`` but is not valid JSON
    (``{status}: all checks complete`` — a legitimate report, raw_decode raises)
    runs the case-(b) guarded resync; the trailing park IS yielded."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="report">\n'
        "{status}: all checks complete\n"
        "</teammate-message>\n"
        '<teammate-message teammate_id="y">\n'
        '{"type":"idle_notification","from":"peer-two",'
        '"timestamp":"2026-07-09T00:00:00Z"}\n'
        "</teammate-message>"
    )
    parsed = parse_teammate_idle_notifications(text)
    assert len(parsed) == 1
    assert parsed[0].name == "peer-two"
    assert is_teammate_message(text) is True


def test_decoded_fragment_then_prose_discards_fragment():
    """GH #57 / Codex P2-3c: a body ``{"a": 1} more prose`` decodes a fragment
    but has NO structural close after it → case (c): the decoded fragment is
    DISCARDED (never appended), the resync skips past the envelope's own close,
    and ONLY the trailing park is yielded."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="report">\n'
        '{"a": 1} more prose after the fragment\n'
        "</teammate-message>\n"
        '<teammate-message teammate_id="y">\n'
        '{"type":"idle_notification","from":"peer-two",'
        '"timestamp":"2026-07-09T00:00:00Z"}\n'
        "</teammate-message>"
    )
    parsed = parse_teammate_idle_notifications(text)
    assert len(parsed) == 1
    assert parsed[0].name == "peer-two"
    assert is_teammate_message(text) is True


def test_markdown_only_entry_is_genuine_user():
    """GH #57 doctrine pin: a markdown-ONLY entry (a report envelope with a
    line-anchored close and NO JSON envelope after it) yields ``[]`` / False —
    no valid JSON envelope is reachable through the resync, so it stays
    genuine-user (never suppressing a real human turn)."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="report">\n'
        "just a markdown report, no park follows\n"
        "</teammate-message>\n\ntrailing prose"
    )
    assert parse_teammate_idle_notifications(text) == []
    assert is_teammate_message(text) is False


def test_resync_close_never_appears_fails_closed():
    """GH #57: an unclosed markdown body with NO later line-anchored close (and
    no later opener) → the resync returns -1 → hard break → ``[]``."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="report">\n'
        "an unterminated markdown report with no close tag anywhere"
    )
    assert parse_teammate_idle_notifications(text) == []
    assert is_teammate_message(text) is False


def test_midline_close_mention_in_body_is_skipped():
    """GH #57 item 10: a MID-LINE ``</teammate-message>`` mention in the body
    (not line-anchored) is skipped by the resync's line-anchored close search;
    it resyncs onto the report's REAL line-anchored close, then yields the park.
    (The asymmetry vs the opener guard is deliberate — a close costs nothing to
    skip, an opener signals a crossed envelope boundary.)"""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="report">\n'
        "a report mentioning </teammate-message> mid-line then more prose\n"
        "</teammate-message>\n"
        '<teammate-message teammate_id="y">\n'
        '{"type":"idle_notification","from":"peer-two",'
        '"timestamp":"2026-07-09T00:00:00Z"}\n'
        "</teammate-message>"
    )
    parsed = parse_teammate_idle_notifications(text)
    assert len(parsed) == 1
    assert parsed[0].name == "peer-two"
    assert is_teammate_message(text) is True


def test_crlf_through_the_resync_path():
    """GH #57 / Codex r2 P3: the close search and the ownership guard agree on
    CRLF. (i) a CRLF markdown report + CRLF close + park yields the park (the
    ``\\r\\n`` close is line-anchored — the char before ``<`` is ``\\n``); (ii) a
    CRLF unclosed report + nested opener + later park fails closed."""
    ok = (
        "Another Claude session sent a message:\r\n"
        '<teammate-message teammate_id="report">\r\n'
        "a crlf markdown report\r\n"
        "</teammate-message>\r\n"
        '<teammate-message teammate_id="y">\r\n'
        '{"type":"idle_notification","from":"peer-two",'
        '"timestamp":"2026-07-09T00:00:00Z"}\r\n'
        "</teammate-message>"
    )
    parsed = parse_teammate_idle_notifications(ok)
    assert len(parsed) == 1
    assert parsed[0].name == "peer-two"
    assert is_teammate_message(ok) is True

    guarded = (
        "Another Claude session sent a message:\r\n"
        '<teammate-message teammate_id="report">\r\n'
        "a crlf unclosed report\r\n"
        '<teammate-message teammate_id="y">\r\n'
        '{"type":"idle_notification","from":"peer-y",'
        '"timestamp":"2026-07-09T00:00:00Z"}\r\n'
        "</teammate-message>"
    )
    assert parse_teammate_idle_notifications(guarded) == []
    assert is_teammate_message(guarded) is False


def test_prior_payload_continuity_and_chained_resync():
    """GH #57 / Codex r2 P3 (asserted on the DIRECT scanner return, r3): a valid
    envelope A → a case-(c) fragment envelope B → a valid envelope Z yields
    exactly ``[A, Z]`` (prior payloads kept, the failed one skipped); and TWO
    consecutive body-failure envelopes before a valid park still yields the
    park (repeated resync progress)."""
    a_z = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="a">\n'
        '{"type":"idle_notification","from":"peer-a",'
        '"timestamp":"2026-07-09T00:00:00Z"}\n'
        "</teammate-message>\n"
        '<teammate-message teammate_id="b">\n'
        '{"frag": 1} then prose, no close after the fragment\n'
        "</teammate-message>\n"
        '<teammate-message teammate_id="z">\n'
        '{"type":"idle_notification","from":"peer-z",'
        '"timestamp":"2026-07-09T00:00:01Z"}\n'
        "</teammate-message>"
    )
    payloads = teammate_envelope_payloads(a_z)
    assert len(payloads) == 2
    assert [p["from"] for p in payloads] == ["peer-a", "peer-z"]

    two_failures = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="r1">\n'
        "first markdown report, non-JSON\n"
        "</teammate-message>\n"
        '<teammate-message teammate_id="r2">\n'
        "{still not valid json,,,\n"
        "</teammate-message>\n"
        '<teammate-message teammate_id="y">\n'
        '{"type":"idle_notification","from":"peer-two",'
        '"timestamp":"2026-07-09T00:00:00Z"}\n'
        "</teammate-message>"
    )
    parsed = parse_teammate_idle_notifications(two_failures)
    assert len(parsed) == 1
    assert parsed[0].name == "peer-two"


def test_known_residual_quoted_close_in_fence_yields_crafted_payload():
    """GH #57 accepted residual: a report body containing a LINE-ANCHORED
    ``</teammate-message>`` (a fenced example) resyncs early onto that quoted
    close; a subsequent CRAFTED complete envelope's payload is then yielded.
    Bounded by the full structural gauntlet + the runtime TEAMMATE ts-gates;
    worst case a one-leg-early tombstone (fail-dark). Pinned as KNOWN."""
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="report">\n'
        "here is how the close tag looks in a fenced block:\n"
        "```\n"
        "</teammate-message>\n"
        "```\n"
        '<teammate-message teammate_id="crafted">\n'
        '{"type":"idle_notification","from":"crafted-name",'
        '"timestamp":"2026-07-09T00:00:00Z"}\n'
        "</teammate-message>"
    )
    parsed = parse_teammate_idle_notifications(text)
    assert len(parsed) == 1
    assert parsed[0].name == "crafted-name"
    assert is_teammate_message(text) is True


def test_resync_close_beyond_byte_bound_fails_closed():
    """GH #57 item 12: a non-JSON first envelope whose line-anchored close lies
    BEYOND the byte bound is invisible to the truncated scan → the resync finds
    no close → ``[]``."""
    pad = "x" * (TEAMMATE_ENVELOPE_SCAN_BYTES + 10)
    text = (
        "Another Claude session sent a message:\n"
        '<teammate-message teammate_id="report">\n'
        "report body " + pad + "\n"
        "</teammate-message>\n"
        '<teammate-message teammate_id="y">\n'
        '{"type":"idle_notification","from":"peer-two",'
        '"timestamp":"2026-07-09T00:00:00Z"}\n'
        "</teammate-message>"
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
