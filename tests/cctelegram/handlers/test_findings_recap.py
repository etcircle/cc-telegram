"""Wave F tests for the AUQ findings recap and frozen surface floor."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cctelegram import md_capture
from cctelegram.handlers import auq_source, interactive_ui, message_queue, output_prefs
from cctelegram.markdown_v2 import convert_markdown
from cctelegram.transcript_parser import TranscriptParser

SID = "feedface-0000-1111-2222-333344445555"


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("CC_TELEGRAM_DIR", str(tmp_path))
    md_capture.ensure_capture_dir()
    message_queue._route_user_turn_at.clear()
    interactive_ui._interactive_msgs.clear()
    monkeypatch.setattr(interactive_ui, "session_id_for_window", lambda _wid: SID)
    monkeypatch.setattr(interactive_ui, "_LIVE_PROSE_RETRY_BUDGET_S", 0.0)
    monkeypatch.setattr(interactive_ui, "_LIVE_PROSE_STREAM_WAIT_BUDGET_S", 0.0)
    yield tmp_path
    message_queue._route_user_turn_at.clear()
    interactive_ui._interactive_msgs.clear()


def _tool_input(label: str = "A") -> dict:
    return {
        "questions": [
            {
                "question": "Choose?",
                "header": "Choice",
                "multiSelect": False,
                "options": [
                    {"label": label, "description": "first"},
                    {"label": "B", "description": "second"},
                ],
            }
        ]
    }


def _write_side(
    root: Path,
    *,
    written_at: float,
    tool_use_id: str = "toolu_1",
    label: str = "A",
) -> None:
    pending = root / "auq_pending"
    pending.mkdir(mode=0o700, exist_ok=True)
    (pending / f"{SID}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": SID,
                "tool_use_id": tool_use_id,
                "written_at": written_at,
                "tool_input": _tool_input(label),
            }
        )
    )


def _seed(*, message_id: str, text: str, first_seen_at: float, final_at: float) -> None:
    path = md_capture.session_ndjson_path(SID)
    deltas = [(first_seen_at, first_seen_at == final_at, text)]
    if first_seen_at != final_at:
        deltas.append((final_at, True, ""))
    with path.open("a") as fh:
        for index, (captured_at, final, delta) in enumerate(deltas):
            fh.write(
                json.dumps(
                    {
                        "captured_at": captured_at,
                        "payload": {
                            "message_id": message_id,
                            "index": index,
                            "final": final,
                            "delta": delta,
                            "transcript_path": f"/p/{SID}.jsonl",
                        },
                    }
                )
                + "\n"
            )


@pytest.fixture
def sends(monkeypatch):
    posted: list[str] = []
    outcomes: list[bool] = []

    async def fake_send(_bot, **kwargs):
        posted.append(kwargs["text"])
        ok = outcomes.pop(0) if outcomes else True
        return (SimpleNamespace(message_id=len(posted)), None) if ok else (None, None)

    monkeypatch.setattr(interactive_ui, "topic_send", fake_send)
    return posted, outcomes


async def _render() -> None:
    await interactive_ui._maybe_post_live_prose(
        AsyncMock(),
        user_id=1,
        thread_id=100,
        chat_id=42,
        window_id="@0",
        ui_name="AskUserQuestion",
    )


def _recap_body(source: str) -> str:
    start = TranscriptParser.EXPANDABLE_QUOTE_START
    end = TranscriptParser.EXPANDABLE_QUOTE_END
    return source.split(start, 1)[1].split(end, 1)[0]


@pytest.mark.asyncio
async def test_same_render_recap_and_chained_auq_floor_reject(_state, sends):
    now = time.time()
    message_queue._route_user_turn_at[(1, 100, "@0")] = now - 100
    _seed(
        message_id="FINDINGS",
        text="incident findings",
        first_seen_at=now - 90,
        final_at=now - 80,
    )
    _write_side(_state, written_at=now, tool_use_id="toolu_first")

    await _render()
    assert [_recap_body(text) for text in sends[0]] == ["incident findings"]
    first_floor = md_capture.get_or_create_surface_floor(SID, "toolu_first", now + 5)
    assert first_floor.floor_at is None

    _write_side(_state, written_at=now + 1, tool_use_id="toolu_second")
    await _render()
    assert len(sends[0]) == 1, "the first AUQ's prose must not recap at S+1"
    second_floor = md_capture.get_or_create_surface_floor(SID, "toolu_second", now + 6)
    assert second_floor.floor_at == first_floor.render_at


@pytest.mark.asyncio
async def test_same_surface_retry_reuses_floor_and_marker_decides_send(_state, sends):
    now = time.time()
    message_queue._route_user_turn_at[(1, 100, "@0")] = now - 100
    _seed(
        message_id="RETRY",
        text="retryable findings",
        first_seen_at=now - 90,
        final_at=now - 80,
    )
    _write_side(_state, written_at=now, tool_use_id="toolu_retry")
    sends[1].append(False)
    await _render()
    stored = md_capture.get_or_create_surface_floor(SID, "toolu_retry", now + 50)
    assert stored.render_at < now + 50
    assert not md_capture.was_recap_shown(
        SID, norm_hash=md_capture.prose_norm_hash("retryable findings"), emitted_at=now
    )

    await _render()
    assert len(sends[0]) == 2
    await _render()
    assert len(sends[0]) == 2, "successful retry marker prevents another recap"


def test_spanning_record_rejected_at_successor_surface(_state):
    _seed(message_id="SPAN", text="spanning", first_seen_at=10.0, final_at=30.0)
    assert (
        md_capture.select_recap_prose(
            SID,
            not_before=5.0,
            effective_floor=20.0,
            emitted_at=100.0,
            emit_anchor_lookback_s=10.0,
        )
        is None
    )


def test_freshest_rejection_does_not_fall_back_to_older_record(_state):
    _seed(message_id="ELIGIBLE", text="older", first_seen_at=10.0, final_at=20.0)
    _seed(message_id="NEWER", text="newer", first_seen_at=5.0, final_at=30.0)
    assert (
        md_capture.select_recap_prose(
            SID,
            not_before=1.0,
            effective_floor=8.0,
            emitted_at=100.0,
            emit_anchor_lookback_s=10.0,
        )
        is None
    )


def test_same_instant_tool_ids_are_distinct_surface_occurrences(_state):
    first = md_capture.get_or_create_surface_floor(SID, "toolu_a", 50.0)
    second = md_capture.get_or_create_surface_floor(SID, "toolu_b", 50.0)
    assert first.surface_id != second.surface_id
    assert first.floor_at is None
    assert second.floor_at == 50.0


@pytest.mark.asyncio
async def test_restart_no_recap_and_fresh_prose_no_recap(_state, sends):
    now = time.time()
    _seed(
        message_id="OLD", text="old findings", first_seen_at=now - 90, final_at=now - 80
    )
    _write_side(_state, written_at=now)
    await _render()
    assert sends[0] == [], "not_before=None must fail closed for recap"

    md_capture.teardown_session(SID)
    message_queue._route_user_turn_at[(1, 100, "@0")] = now - 1
    _seed(
        message_id="FRESH",
        text="fresh prose",
        first_seen_at=now - 0.5,
        final_at=now - 0.5,
    )
    await _render()
    assert sends[0] == ["fresh prose"]


@pytest.mark.asyncio
async def test_quiet_and_missing_anchor_suppress_recap(
    _state, sends, monkeypatch, caplog
):
    now = time.time()
    message_queue._route_user_turn_at[(1, 100, "@0")] = now - 100
    _seed(
        message_id="OLD",
        text="quiet findings",
        first_seen_at=now - 90,
        final_at=now - 80,
    )
    _write_side(_state, written_at=now)
    monkeypatch.setattr(
        output_prefs, "resolve", lambda _uid: output_prefs.PRESETS["quiet"]
    )
    await _render()
    assert sends[0] == []

    (_state / "auq_pending" / f"{SID}.json").unlink()
    monkeypatch.setattr(
        output_prefs, "resolve", lambda _uid: output_prefs.PRESETS["standard"]
    )
    with caplog.at_level(logging.DEBUG):
        await _render()
    assert "reason=no_anchor" in caplog.text
    assert sends[0] == []


@pytest.mark.asyncio
async def test_surface_identity_atomic_read_tool_id_and_full_fingerprint(
    _state, sends, monkeypatch
):
    now = time.time()
    message_queue._route_user_turn_at[(1, 100, "@0")] = now - 100
    calls = 0
    real_read = auq_source.read_side_file_for_recovery

    def one_read(session_id):
        nonlocal calls
        calls += 1
        return real_read(session_id)

    monkeypatch.setattr(auq_source, "read_side_file_for_recovery", one_read)
    monkeypatch.setattr(
        auq_source,
        "peek_side_file_written_at",
        lambda _sid: pytest.fail(
            "a second side-file read would permit a torn identity"
        ),
    )
    _write_side(_state, written_at=now, tool_use_id="toolu_primary")
    await _render()
    assert calls == 1
    assert (
        md_capture.get_or_create_surface_floor(SID, "toolu_primary", now).surface_id
        == "toolu_primary"
    )

    first = real_read(SID)
    assert first is not None
    _write_side(_state, written_at=now, tool_use_id="", label="Changed")
    second = real_read(SID)
    assert second is not None
    fallback_a = f"{first.written_at!r}:{first.source_fingerprint}"
    fallback_b = f"{second.written_at!r}:{second.source_fingerprint}"
    assert fallback_a != fallback_b
    assert len(second.source_fingerprint) == 64


@pytest.mark.parametrize(
    "text",
    [
        "_*[]()~`>#+-=|{}.!\\" * 600,
        "\\\\\\\\ punctuation! [x](y). " * 500,
        "".join(f"line {i}!\n" for i in range(1200)),
    ],
)
def test_rendered_cost_chunks_never_truncate_and_fully_reassemble(text):
    chunks = interactive_ui._split_recap_source(text)
    assert "".join(chunks) == text
    for chunk in chunks:
        source = interactive_ui._recap_message_source(chunk)
        rendered = convert_markdown(source)
        assert len(rendered) <= 4096
        assert "truncated" not in rendered
        assert source.count(TranscriptParser.EXPANDABLE_QUOTE_START) == 1
        assert source.count(TranscriptParser.EXPANDABLE_QUOTE_END) == 1


@pytest.mark.asyncio
async def test_0854_incident_33_rejections_recaps_freshest_turn_record(_state, sends):
    now = time.time()
    not_before = now - 200
    message_queue._route_user_turn_at[(1, 100, "@0")] = not_before
    for idx in range(32):
        _seed(
            message_id=f"STALE-{idx}",
            text=f"stale {idx}",
            first_seen_at=not_before - 50 + idx,
            final_at=not_before - 40 + idx,
        )
    _seed(
        message_id="0852-FINDINGS",
        text="08:52:32 findings",
        first_seen_at=not_before + 1,
        final_at=not_before + 2,
    )
    _write_side(_state, written_at=now, tool_use_id="toolu_0854")
    await _render()
    assert [_recap_body(text) for text in sends[0]] == ["08:52:32 findings"]
