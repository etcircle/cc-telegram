"""Unit tests for the Bug 2 live-prose delivery + dedup primitives (PR-C+D).

Covers the freshness selection + shown-live marker store in ``md_capture`` and
the batch/group dedup ``session_monitor.filter_live_prose_duplicates`` — the
adversarial cases the plan calls out: identical text under a different message
id must NOT suppress, a group without an interactive tool_use must NOT suppress,
the synthetic ExitPlanMode plan text is excluded from the aggregate, and a
two-candidate ambiguity suppresses none.
"""

from __future__ import annotations

import json
import time

import pytest

from cctelegram import md_capture
from cctelegram.md_capture import (
    ProseRecord,
    prose_norm_hash,
    read_prose_records,
    select_fresh_prose,
)
from cctelegram.session_monitor import NewMessage, filter_live_prose_duplicates

_SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_PROSE = "SQLite is a zero config serverless embedded relational database"


@pytest.fixture
def cc_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    md_capture.msg_display_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    return tmp_path


def _seed(session_id: str, *, message_id: str, delta: str, captured_at: float) -> None:
    line = {
        "captured_at": captured_at,
        "payload": {
            "message_id": message_id,
            "index": 0,
            "final": True,
            "delta": delta,
            "transcript_path": f"/p/{session_id}.jsonl",
        },
    }
    path = md_capture.session_ndjson_path(session_id)
    with path.open("a") as f:
        f.write(json.dumps(line) + "\n")


def _nm(
    *,
    text: str,
    content_type: str,
    message_id: str,
    session_id: str = _SID,
    tool_name: str | None = None,
    block_origin: str | None = None,
) -> NewMessage:
    return NewMessage(
        session_id=session_id,
        text=text,
        content_type=content_type,
        role="assistant",
        tool_name=tool_name,
        image_data=None,
        message_id=message_id,
        block_origin=block_origin,
    )


# ── Freshness selection ──────────────────────────────────────────────────────


def test_select_fresh_prose_picks_most_recent_within_ttl(cc_dir):
    now = time.time()
    _seed(_SID, message_id="OLD", delta="stale", captured_at=now - 50)
    _seed(_SID, message_id="FRESH", delta=_PROSE, captured_at=now - 2)
    rec = select_fresh_prose(_SID, now=now, ttl_seconds=8.0)
    assert rec is not None and rec.md_message_id == "FRESH"
    assert rec.text == _PROSE


def test_select_fresh_prose_rejects_all_stale(cc_dir):
    now = time.time()
    _seed(_SID, message_id="OLD", delta="stale", captured_at=now - 50)
    assert select_fresh_prose(_SID, now=now, ttl_seconds=8.0) is None


def test_select_fresh_prose_missing_file(cc_dir):
    assert select_fresh_prose("no-such", now=time.time(), ttl_seconds=8.0) is None


def test_freshness_ttls_are_named_constants():
    assert md_capture.AUQ_PROSE_TTL_S > 0
    assert md_capture.EPM_PROSE_TTL_S >= md_capture.AUQ_PROSE_TTL_S


# ── Shown-live markers ───────────────────────────────────────────────────────


def test_marker_record_read_consume_roundtrip(cc_dir):
    nh = prose_norm_hash(_PROSE)
    md_capture.record_shown_live(_SID, md_message_id="M1", norm_hash=nh, shown_at=1.0)
    markers = md_capture.read_shown_live_markers(_SID)
    assert [(m.md_message_id, m.norm_hash) for m in markers] == [("M1", nh)]
    md_capture.consume_shown_live(_SID, "M1")
    assert md_capture.read_shown_live_markers(_SID) == []


def test_was_shown_live_is_consume_inclusive(cc_dir):
    nh = prose_norm_hash(_PROSE)
    assert md_capture.was_shown_live(_SID, "M1") is False
    md_capture.record_shown_live(_SID, md_message_id="M1", norm_hash=nh, shown_at=1.0)
    assert md_capture.was_shown_live(_SID, "M1") is True
    # Still True after consume — the render-path idempotency must survive the
    # dedup consuming the marker (regression for the scenario double-post).
    md_capture.consume_shown_live(_SID, "M1")
    assert md_capture.read_shown_live_markers(_SID) == []
    assert md_capture.was_shown_live(_SID, "M1") is True


def test_markers_coexist_with_delta_lines(cc_dir):
    now = time.time()
    _seed(_SID, message_id="MD", delta=_PROSE, captured_at=now)
    md_capture.record_shown_live(
        _SID, md_message_id="MD", norm_hash=prose_norm_hash(_PROSE), shown_at=now
    )
    # delta reader ignores the marker line; marker reader ignores the delta line.
    assert len(read_prose_records(_SID)) == 1
    assert len(md_capture.read_shown_live_markers(_SID)) == 1


def test_prose_norm_hash_matches_record(cc_dir):
    now = time.time()
    _seed(_SID, message_id="MD", delta=_PROSE, captured_at=now)
    rec = read_prose_records(_SID)[0]
    assert rec.norm_hash == prose_norm_hash(_PROSE)


# ── Batch dedup ──────────────────────────────────────────────────────────────


def _mark(session_id: str, text: str) -> None:
    md_capture.record_shown_live(
        session_id,
        md_message_id="MDLIVE",
        norm_hash=prose_norm_hash(text),
        shown_at=time.time(),
    )


def test_dedup_suppresses_matched_prose_and_keeps_tool_use(cc_dir):
    _mark(_SID, _PROSE)
    batch = [
        _nm(text=_PROSE, content_type="text", message_id="MID"),
        _nm(
            text="**AskUserQuestion**(Which DB?)",
            content_type="tool_use",
            message_id="MID",
            tool_name="AskUserQuestion",
        ),
    ]
    out = filter_live_prose_duplicates(batch)
    assert [m.content_type for m in out] == ["tool_use"]
    # marker consumed
    assert md_capture.read_shown_live_markers(_SID) == []


def test_dedup_no_marker_keeps_prose(cc_dir):
    batch = [
        _nm(text=_PROSE, content_type="text", message_id="MID"),
        _nm(
            text="x",
            content_type="tool_use",
            message_id="MID",
            tool_name="AskUserQuestion",
        ),
    ]
    out = filter_live_prose_duplicates(batch)
    assert any(m.content_type == "text" for m in out)


def test_dedup_requires_interactive_tool_use_in_group(cc_dir):
    _mark(_SID, _PROSE)
    # Same prose + a NON-interactive tool_use → not a candidate group.
    batch = [
        _nm(text=_PROSE, content_type="text", message_id="MID"),
        _nm(
            text="**Read**(x)",
            content_type="tool_use",
            message_id="MID",
            tool_name="Read",
        ),
    ]
    out = filter_live_prose_duplicates(batch)
    assert any(m.content_type == "text" for m in out)


def test_dedup_identical_text_different_message_id_not_suppressed(cc_dir):
    """The look-alike sibling contract: identical prose under a DIFFERENT
    message_id (no interactive tool_use in ITS group) must NOT be suppressed,
    even when a marker for that text exists."""
    _mark(_SID, _PROSE)
    batch = [
        # The real paired group (suppressed).
        _nm(text=_PROSE, content_type="text", message_id="MID_A"),
        _nm(
            text="x",
            content_type="tool_use",
            message_id="MID_A",
            tool_name="AskUserQuestion",
        ),
        # A look-alike prose in a different message with no interactive tool_use.
        _nm(text=_PROSE, content_type="text", message_id="MID_B"),
    ]
    out = filter_live_prose_duplicates(batch)
    texts = [m for m in out if m.content_type == "text"]
    assert len(texts) == 1 and texts[0].message_id == "MID_B"


def test_dedup_excludes_exitplan_plan_text_from_aggregate(cc_dir):
    """A group whose only text is the synthetic ExitPlanMode plan body
    (block_origin set) does NOT match a REAL-prose marker — the plan text is
    excluded from the aggregate, so its norm_hash differs."""
    _mark(_SID, _PROSE)  # marker for the real prose
    batch = [
        _nm(
            text=_PROSE, content_type="text", message_id="MID", block_origin="exit_plan"
        ),
        _nm(
            text="**ExitPlanMode**",
            content_type="tool_use",
            message_id="MID",
            tool_name="ExitPlanMode",
        ),
    ]
    out = filter_live_prose_duplicates(batch)
    # plan text not excluded would have matched + suppressed; it must survive.
    assert any(m.content_type == "text" for m in out)


def test_dedup_two_candidate_ambiguity_suppresses_none(cc_dir):
    """EPM ambiguity: >1 group sharing one (session, norm_hash) marker →
    suppress NONE, consume no marker."""
    _mark(_SID, _PROSE)
    batch = [
        _nm(text=_PROSE, content_type="text", message_id="MID_1"),
        _nm(
            text="e",
            content_type="tool_use",
            message_id="MID_1",
            tool_name="ExitPlanMode",
        ),
        _nm(text=_PROSE, content_type="text", message_id="MID_2"),
        _nm(
            text="e",
            content_type="tool_use",
            message_id="MID_2",
            tool_name="ExitPlanMode",
        ),
    ]
    out = filter_live_prose_duplicates(batch)
    assert sum(1 for m in out if m.content_type == "text") == 2
    # marker NOT consumed
    assert len(md_capture.read_shown_live_markers(_SID)) == 1


def test_dedup_multiblock_adjacent_blocks_match(cc_dir):
    """Adjacent multi-block prose ("A" + "B") aggregates to "A\\nB" and matches
    a marker hashed from the same joined form."""
    md_capture.record_shown_live(
        _SID,
        md_message_id="MDLIVE",
        norm_hash=prose_norm_hash("Part one.\nPart two."),
        shown_at=time.time(),
    )
    batch = [
        _nm(text="Part one.", content_type="text", message_id="MID"),
        _nm(text="Part two.", content_type="text", message_id="MID"),
        _nm(
            text="x",
            content_type="tool_use",
            message_id="MID",
            tool_name="AskUserQuestion",
        ),
    ]
    out = filter_live_prose_duplicates(batch)
    assert not any(m.content_type == "text" for m in out)


def test_dedup_empty_and_no_group_passthrough(cc_dir):
    assert filter_live_prose_duplicates([]) == []
    batch = [_nm(text="hi", content_type="text", message_id="MID")]
    assert filter_live_prose_duplicates(batch) == batch


def test_prose_record_is_frozen_and_fields():
    rec = ProseRecord(
        session_id=_SID,
        transcript_path="t",
        md_message_id="M",
        text="x",
        raw_hash="r",
        norm_hash="n",
        first_seen_at=1.0,
        final_at=2.0,
    )
    with pytest.raises(Exception):
        rec.text = "y"  # type: ignore[misc]
