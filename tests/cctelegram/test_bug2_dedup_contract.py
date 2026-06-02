"""Bug 2 (unit): the data-model prerequisites for live-prose dedup.

Bug 2: assistant free-text prose written in the SAME turn as an
``AskUserQuestion`` / ``ExitPlanMode`` ``tool_use`` is buffered in JSONL until
the prompt resolves, so the Telegram user chooses blind. The true fix delivers
the prose LIVE (via the ``MessageDisplay`` hook) before the picker card, then
**dedups** the post-resolution JSONL copy. That dedup groups the sibling prose
with the interactive ``tool_use`` by the JSONL ``message.id`` and must exclude
the synthetic ``ExitPlanMode`` plan text (which the parser emits as a plain
``text`` entry).

History: these started as RED baselines in commit 4c77293 (PR-A), each asserting
the CURRENT limitation with a docstring naming the assertion the fix PR must
flip. PR-B (§3.0 of ``temp/2026-06-02-bug2-true-fix-plan-v3.md``) plumbed JSONL
``message.id`` + a ``block_origin`` marker through ``ParsedEntry``; the asserts
below are the FLIPPED, now-passing contract.

Shape oracle: ``temp/auq-fixtures/2026-06-02-messagedisplay-live-capture/
scratch_session.jsonl`` — one ``message.id`` (``msg_012k4ogm6rHomKXfuL31jCmM``)
spans the thinking/text/tool_use lines of the captured turn, each its own uuid.

Scope boundary: the dedup *function* behaviors (normalized text equality,
identical-text-under-a-different-message.id NOT suppressed, two-unresolved-EPM
ambiguity → suppress none) assert against machinery that lands in PR-C+D; their
tests ship with that function. This module locks the §3.0 data-model
prerequisites; ``tests/scenarios/test_bug2_prose_before_picker.py`` still holds
the RED-baseline proof of the observable bug (it flips in PR-C).
"""

from __future__ import annotations

from cctelegram.transcript_parser import BLOCK_ORIGIN_EXIT_PLAN, TranscriptParser

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


def test_parsed_entry_exposes_message_id(
    make_assistant_message, make_text_block
) -> None:
    """A parsed prose entry exposes the JSONL ``message.id`` so dedup can group
    by it (flipped from PR-A's ``not hasattr`` RED baseline)."""
    entry = make_assistant_message(
        blocks=[make_text_block(_PROSE)], message_id=_REAL_MID, uuid="u-prose"
    )
    parsed, _ = TranscriptParser.parse_entries([entry])

    text_entries = [e for e in parsed if e.content_type == "text"]
    assert text_entries, "expected the prose text entry"
    # Assert the SPECIFIC id, not merely ``is not None``: a regression that
    # stamped the per-line uuid (or any wrong value) would pass a truthiness
    # check but break ``(session_id, message_id)`` grouping.
    for e in text_entries:
        assert e.message_id == _REAL_MID


def test_prose_and_auq_share_message_id(
    make_assistant_message, make_text_block, make_tool_use_block
) -> None:
    """Prose and its sibling AUQ ``tool_use`` arrive as SEPARATE JSONL lines
    sharing one ``message.id`` (the real capture shape); both parsed entries
    expose that id so ``(session_id, message_id)`` grouping yields one group."""
    prose_line = make_assistant_message(
        blocks=[make_text_block(_PROSE)], message_id=_REAL_MID, uuid="u-prose"
    )
    auq_line = make_assistant_message(
        blocks=[make_tool_use_block(name="AskUserQuestion", input_data=_auq_input())],
        message_id=_REAL_MID,
        uuid="u-auq",
    )
    parsed, _ = TranscriptParser.parse_entries([prose_line, auq_line])

    # Every entry (the prose text + the AUQ tool_use) carries the shared id, so
    # a group keyed on it collapses both lines into one group.
    assert parsed, "expected the prose + AUQ entries"
    assert all(e.message_id == _REAL_MID for e in parsed)
    assert {e.content_type for e in parsed} == {"text", "tool_use"}


def test_identical_text_different_message_ids_stay_distinguishable(
    make_assistant_message, make_text_block
) -> None:
    """Two assistant messages with IDENTICAL text but DIFFERENT ``message.id``
    parse to two separate entries that expose their distinct ids. This pins the
    contract the dedup honors: identical prose under a DIFFERENT message.id must
    NOT be suppressed — PR-D's dedup keys on the id so only the shown-live group
    is suppressed, never the look-alike sibling."""
    a = make_assistant_message(
        blocks=[make_text_block(_PROSE)], message_id=_REAL_MID, uuid="u-a"
    )
    b = make_assistant_message(
        blocks=[make_text_block(_PROSE)], message_id=_OTHER_MID, uuid="u-b"
    )
    parsed, _ = TranscriptParser.parse_entries([a, b])

    texts = [e for e in parsed if e.content_type == "text" and _PROSE in (e.text or "")]
    assert len(texts) == 2, "both identical-text blocks parse as separate entries"
    # Distinct ids, in order — the dedup can tell the two apart.
    assert sorted(t.message_id or "" for t in texts) == sorted([_REAL_MID, _OTHER_MID])


def test_exitplan_plan_text_marked_synthetic(
    make_assistant_message, make_text_block, make_tool_use_block
) -> None:
    """ExitPlanMode's ``input.plan`` is emitted as a ``content_type='text'``
    entry (transcript_parser.py) but carries a ``block_origin`` marker so dedup
    excludes it; a real prose block stays origin-less. Asserts the SPECIFIC
    sentinel on the plan entry AND ``None`` on the real one — the equality-of-
    two-defaults form would be vacuous (a fix that forgot to mark the plan text
    would leave both None/equal and still pass)."""
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
    assert real_text.content_type == "text"
    assert plan_text.content_type == "text"
    # Real prose: no synthetic marker. Synthetic plan body: the sentinel.
    assert real_text.block_origin is None
    assert plan_text.block_origin == BLOCK_ORIGIN_EXIT_PLAN


def test_multiblock_prose_shares_message_id(
    make_assistant_message, make_text_block
) -> None:
    """A message with multiple text blocks parses to multiple SEPARATE entries
    that all expose the same ``message_id``, so PR-C can aggregate them (join
    with ``\\n``, block order preserved) before comparing to the shown-live
    prose."""
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
    assert all(t.message_id == _REAL_MID for t in texts)


def test_crlf_preserved_at_parse_layer(make_assistant_message, make_text_block) -> None:
    """Invariant (not flipped): the parser does not normalize line endings — a
    CRLF prose block keeps its ``\\r``. The dedup comparator (PR-D) normalizes
    CRLF→LF + trailing-whitespace trim before hashing so the live (display) text
    and the JSONL text compare equal; that normalization lives in the dedup
    layer, never at parse, so this stays true across the whole fix."""
    entry = make_assistant_message(
        blocks=[make_text_block("line one\r\nline two")],
        message_id=_REAL_MID,
        uuid="u-crlf",
    )
    parsed, _ = TranscriptParser.parse_entries([entry])
    text = next(e for e in parsed if e.content_type == "text")
    assert "\r\n" in (text.text or ""), (
        "CRLF is preserved verbatim at parse; PR-D's dedup comparator "
        "normalizes CRLF→LF before hashing (in the dedup layer)."
    )
