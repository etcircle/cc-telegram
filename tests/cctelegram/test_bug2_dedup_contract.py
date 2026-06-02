"""Bug 2 RED gate (unit): the data-model prerequisites for live-prose dedup.

Bug 2: assistant free-text prose written in the SAME turn as an
``AskUserQuestion`` / ``ExitPlanMode`` ``tool_use`` is buffered in JSONL until
the prompt resolves, so the Telegram user chooses blind. The true fix delivers
the prose LIVE (via the ``MessageDisplay`` hook) before the picker card, then
**dedups** the post-resolution JSONL copy. That dedup groups the sibling prose
with the interactive ``tool_use`` by the JSONL ``message.id`` and must exclude
the synthetic ``ExitPlanMode`` plan text (which the parser emits as a plain
``text`` entry).

These are RED-baseline asserts in the repo's established style (see commit
1dcccff, Bug 1 PR-A): each test asserts the CURRENT limitation and PASSES today
(so the suite stays green), with a docstring naming the assertion the fix PR
must flip. They lock §3.0 of the plan
(``temp/2026-06-02-bug2-true-fix-plan-v3.md``): plumb JSONL ``message.id`` +
a block-origin marker through ``ParsedEntry``.

Shape oracle: ``temp/auq-fixtures/2026-06-02-messagedisplay-live-capture/
scratch_session.jsonl`` — one ``message.id`` (``msg_012k4ogm6rHomKXfuL31jCmM``)
spans the thinking/text/tool_use lines of the captured turn, each its own uuid.

Scope boundary: the dedup *function* behaviors (normalized text equality,
identical-text-under-a-different-message.id NOT suppressed, two-unresolved-EPM
ambiguity → suppress none) assert against machinery that lands in PR-C+D; their
failing→passing tests ship with that function. PR-A locks the data-model
prerequisites here + proves the observable bug in
``tests/scenarios/test_bug2_prose_before_picker.py``.
"""

from __future__ import annotations

from cctelegram.transcript_parser import TranscriptParser

# Anchored to the real capture so the corpus mirrors production JSONL shape.
_REAL_MID = "msg_012k4ogm6rHomKXfuL31jCmM"
_OTHER_MID = "msg_0000000000000000000000"
_PROSE = "SQLite is a zero config serverless embedded relational database"


def _auq_input() -> dict:
    return {
        "questions": [
            {
                "question": "Which DB?",
                "header": "DB",
                "multiSelect": False,
                "options": [
                    {"label": "A) SQLite", "description": "Embedded."},
                    {"label": "B) Postgres", "description": "Server."},
                ],
            }
        ]
    }


def test_red_baseline_parsed_entry_exposes_no_message_id(
    make_assistant_message, make_text_block
) -> None:
    """RED baseline: the parser drops the JSONL ``message.id`` — a ``ParsedEntry``
    exposes only the per-line ``uuid``. PR-B flips this: assert
    ``entry.message_id == _REAL_MID`` (so dedup can group by it)."""
    entry = make_assistant_message(
        blocks=[make_text_block(_PROSE)], message_id=_REAL_MID, uuid="u-prose"
    )
    parsed, _ = TranscriptParser.parse_entries([entry])

    text_entries = [e for e in parsed if e.content_type == "text"]
    assert text_entries, "expected the prose text entry"
    for e in text_entries:
        # `not hasattr`, NOT `getattr(...) is None`: a RED gate must distinguish
        # "field absent" (today) from "field present but None" (a broken PR-B
        # that adds the field yet never populates it). The latter would pass a
        # `getattr(...) is None` check and let an incomplete fix slip the gate.
        assert not hasattr(e, "message_id"), (
            "RED baseline: ParsedEntry has no message_id field yet. "
            "PR-B adds it; flip this to assert e.message_id == _REAL_MID."
        )


def test_red_baseline_prose_and_auq_ungroupable_today(
    make_assistant_message, make_text_block, make_tool_use_block
) -> None:
    """RED baseline: prose and its sibling AUQ ``tool_use`` arrive as SEPARATE
    JSONL lines sharing one ``message.id`` (the real capture shape), but the
    parser exposes no id to group them by. PR-B flips this: assert both parsed
    entries expose ``message_id == _REAL_MID`` so ``(session_id, message_id)``
    grouping yields a single group."""
    prose_line = make_assistant_message(
        blocks=[make_text_block(_PROSE)], message_id=_REAL_MID, uuid="u-prose"
    )
    auq_line = make_assistant_message(
        blocks=[make_tool_use_block(name="AskUserQuestion", input_data=_auq_input())],
        message_id=_REAL_MID,
        uuid="u-auq",
    )
    parsed, _ = TranscriptParser.parse_entries([prose_line, auq_line])

    # `not hasattr` (absent), not `getattr(...) is None` — see the note above.
    assert all(not hasattr(e, "message_id") for e in parsed), (
        "RED baseline: ParsedEntry exposes no message_id field, so the prose "
        "and the AUQ tool_use cannot be grouped by (session_id, message_id). "
        "PR-B adds the field; flip this to assert every entry exposes "
        "message_id == _REAL_MID."
    )


def test_red_baseline_identical_text_different_message_ids_stay_separate(
    make_assistant_message, make_text_block
) -> None:
    """RED baseline: two assistant messages with IDENTICAL text but DIFFERENT
    ``message.id`` parse to two separate text entries today, and neither exposes
    a ``message_id`` to tell them apart. This pins the contract the dedup must
    honor: identical prose under a DIFFERENT message.id must NOT be suppressed.
    PR-B exposes ``message_id`` (flip: the two entries carry ``_REAL_MID`` and
    ``_OTHER_MID``); PR-D's dedup keys on it so only the shown-live group is
    suppressed, never the look-alike sibling."""
    a = make_assistant_message(
        blocks=[make_text_block(_PROSE)], message_id=_REAL_MID, uuid="u-a"
    )
    b = make_assistant_message(
        blocks=[make_text_block(_PROSE)], message_id=_OTHER_MID, uuid="u-b"
    )
    parsed, _ = TranscriptParser.parse_entries([a, b])

    texts = [e for e in parsed if e.content_type == "text" and _PROSE in (e.text or "")]
    assert len(texts) == 2, (
        "both identical-text blocks should parse as separate entries"
    )
    assert all(not hasattr(t, "message_id") for t in texts), (
        "RED baseline: no message_id exposed to distinguish the two identical "
        "texts. PR-B adds it; PR-D's dedup then suppresses only the shown-live "
        "group and leaves the different-message.id sibling untouched."
    )


def test_red_baseline_exitplan_plan_text_indistinguishable_from_prose(
    make_assistant_message, make_text_block, make_tool_use_block
) -> None:
    """RED baseline: ExitPlanMode's ``input.plan`` is emitted as a plain
    ``content_type='text'`` entry (transcript_parser.py:634-647), with no marker
    separating it from a REAL assistant prose block. A naive group-dedup would
    conflate the synthetic plan body with real prose. PR-B flips this: the plan
    entry carries a block-origin marker (``getattr(e, 'block_origin', None)`` is
    not None) so dedup excludes it; a real prose block stays origin-less/'real'."""
    real = make_assistant_message(
        blocks=[make_text_block(_PROSE)], message_id=_REAL_MID, uuid="u-real"
    )
    epm = make_assistant_message(
        blocks=[
            make_tool_use_block(
                name="ExitPlanMode", input_data={"plan": "## Plan\nDo X then Y."}
            )
        ],
        message_id=_OTHER_MID,
        uuid="u-epm",
        stop_reason="tool_use",
    )
    parsed_real, _ = TranscriptParser.parse_entries([real])
    parsed_epm, _ = TranscriptParser.parse_entries([epm])

    real_text = next(e for e in parsed_real if e.content_type == "text")
    plan_text = next(
        e for e in parsed_epm if e.content_type == "text" and "Do X" in (e.text or "")
    )
    # Both are content_type='text'. Use `not hasattr` per field, NOT an equality
    # of two getattr(...) defaults: the equality form is vacuous — a broken PR-B
    # that adds block_origin but forgets to mark the synthetic plan text leaves
    # both None/equal and would still pass, slipping the gate.
    assert real_text.content_type == "text"
    assert plan_text.content_type == "text"
    assert not hasattr(real_text, "block_origin"), (
        "RED baseline: ParsedEntry has no block_origin field yet. PR-B adds it; "
        "flip to assert the real prose block keeps the 'real' origin."
    )
    assert not hasattr(plan_text, "block_origin"), (
        "RED baseline: the ExitPlanMode plan text is indistinguishable from real "
        "prose (no block_origin field). PR-B adds a distinct origin marker on the "
        "plan entry so dedup never suppresses real prose by matching synthetic "
        "plan text; flip to assert plan_text.block_origin marks it synthetic."
    )


def test_red_baseline_multiblock_prose_emitted_as_separate_ungrouped_entries(
    make_assistant_message, make_text_block
) -> None:
    """RED baseline: a message with multiple text blocks parses to multiple
    SEPARATE ``ParsedEntry`` text entries, with no id to group/aggregate them.
    PR-C flips this: dedup aggregates all real text blocks sharing a
    ``message_id`` (join with ``\\n``, block order preserved) before comparing
    to the shown-live prose."""
    entry = make_assistant_message(
        blocks=[make_text_block("Part one."), make_text_block("Part two.")],
        message_id=_REAL_MID,
        uuid="u-multi",
    )
    parsed, _ = TranscriptParser.parse_entries([entry])

    texts = [e for e in parsed if e.content_type == "text"]
    assert [t.text for t in texts] == ["Part one.", "Part two."], (
        "expected two separate text entries in block order"
    )
    assert all(not hasattr(t, "message_id") for t in texts), (
        "RED baseline: the two blocks share a message.id in JSONL but the "
        "parser exposes no message_id field, so they cannot be aggregated as "
        "one group. PR-B adds the field; PR-C groups + joins them."
    )


def test_red_baseline_crlf_not_normalized_at_parse(
    make_assistant_message, make_text_block
) -> None:
    """RED baseline: the parser does not normalize line endings — a CRLF prose
    block keeps its ``\\r``. The dedup comparator (PR-D) normalizes CRLF→LF +
    trailing-whitespace trim before hashing so the live (display) text and the
    JSONL text compare equal. This test pins that no such normalization exists
    at the parse layer today; PR-D adds it in the dedup comparator (not here)."""
    entry = make_assistant_message(
        blocks=[make_text_block("line one\r\nline two")],
        message_id=_REAL_MID,
        uuid="u-crlf",
    )
    parsed, _ = TranscriptParser.parse_entries([entry])
    text = next(e for e in parsed if e.content_type == "text")
    assert "\r\n" in (text.text or ""), (
        "RED baseline: CRLF is preserved verbatim at parse. PR-D's dedup "
        "comparator normalizes CRLF→LF before hashing (in the dedup layer)."
    )
