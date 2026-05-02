"""Unit tests for SessionMonitor JSONL reading and offset handling."""

import json

import pytest

from ccbot.monitor_state import TrackedSession
from ccbot.session_monitor import (
    NewMessage,
    SessionInfo,
    SessionMonitor,
    TranscriptEvent,
)


class TestReadNewLinesOffsetRecovery:
    """Tests for _read_new_lines offset corruption recovery."""

    @pytest.fixture
    def monitor(self, tmp_path):
        """Create a SessionMonitor with temp state file."""
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_mid_line_offset_recovery(self, monitor, tmp_path, make_jsonl_entry):
        """Recover from corrupted offset pointing mid-line."""
        # Create JSONL file with two valid lines
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first message")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second message")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Calculate offset pointing into the middle of line 1
        line1_bytes = len(json.dumps(entry1).encode("utf-8")) // 2
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=line1_bytes,  # Mid-line (corrupted)
        )

        # Read should recover and return empty (offset moved to next line)
        result = await monitor._read_new_lines(session, jsonl_file)

        # Should return empty list (recovery skips to next line, no new content yet)
        assert result == []

        # Offset should now point to start of line 2
        line1_full = len(json.dumps(entry1).encode("utf-8")) + 1  # +1 for newline
        assert session.last_byte_offset == line1_full

    @pytest.mark.asyncio
    async def test_valid_offset_reads_normally(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """Normal reading when offset points to line start."""
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Offset at 0 should read both lines
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        assert len(result) == 2
        assert session.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_truncation_detection(self, monitor, tmp_path, make_jsonl_entry):
        """Detect file truncation and reset offset."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="content")
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        # Set offset beyond file size (simulates truncation)
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=9999,  # Beyond file size
        )

        result = await monitor._read_new_lines(session, jsonl_file)

        # Should reset offset to 0 and read the line
        assert session.last_byte_offset == jsonl_file.stat().st_size
        assert len(result) == 1


class TestRegisterSession:
    """Tests for SessionMonitor.register_session pre-registration."""

    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    def test_registers_unknown_session_at_offset_zero(self, monitor, tmp_path):
        jsonl_file = tmp_path / "session.jsonl"

        registered = monitor.register_session("sid-new", jsonl_file, offset=0)

        assert registered is True
        tracked = monitor.state.get_session("sid-new")
        assert tracked is not None
        assert tracked.file_path == str(jsonl_file)
        assert tracked.last_byte_offset == 0

    def test_noop_when_session_already_tracked(self, monitor, tmp_path):
        jsonl_file = tmp_path / "session.jsonl"
        monitor.state.update_session(
            TrackedSession(
                session_id="sid-existing",
                file_path=str(jsonl_file),
                last_byte_offset=42,
            )
        )

        registered = monitor.register_session("sid-existing", jsonl_file, offset=0)

        assert registered is False
        # Existing offset preserved.
        assert monitor.state.get_session("sid-existing").last_byte_offset == 42

    @pytest.mark.asyncio
    async def test_pre_registered_offset_zero_picks_up_first_exchange(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """Regression: a freshly bound session must read from offset 0.

        Without pre-registration, ``check_for_updates`` initializes new
        sessions at end-of-file, dropping the seed user message and the
        first assistant reply that were already written between hook fire
        and the first poll cycle.
        """
        jsonl_file = tmp_path / "session.jsonl"

        # Pre-register before any content exists (mirrors the bot flow:
        # hook fires → register → send pending text → Claude appends).
        monitor.register_session("sid-fresh", jsonl_file, offset=0)

        # Now simulate Claude appending the seed exchange.
        user_entry = make_jsonl_entry(msg_type="user", content="Hi")
        assistant_entry = make_jsonl_entry(
            msg_type="assistant", content="Hi! What can I help you with?"
        )
        jsonl_file.write_text(
            json.dumps(user_entry) + "\n" + json.dumps(assistant_entry) + "\n",
            encoding="utf-8",
        )

        tracked = monitor.state.get_session("sid-fresh")
        result = await monitor._read_new_lines(tracked, jsonl_file)

        assert len(result) == 2
        assert tracked.last_byte_offset == jsonl_file.stat().st_size


class TestEventCallback:
    """TranscriptEvent dispatch and legacy NewMessage co-emission."""

    @pytest.fixture
    def monitor(self, tmp_path):
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    def _write_jsonl(self, path, lines: list[dict]) -> None:
        path.write_text(
            "\n".join(json.dumps(line) for line in lines) + "\n",
            encoding="utf-8",
        )

    def _patch_scan(self, monitor, session_id: str, jsonl_file):
        async def _scan():
            return [SessionInfo(session_id=session_id, file_path=jsonl_file)]

        monitor.scan_projects = _scan  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_event_callback_fires_with_assistant_text(
        self, monitor, tmp_path, make_jsonl_entry, make_text_block
    ):
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(
            "assistant", [make_text_block("hello world")], session_id="sid"
        )
        entry["message"]["stop_reason"] = "end_turn"
        entry["uuid"] = "evt-uuid-1"
        self._write_jsonl(jsonl_file, [entry])

        monitor.register_session("sid", jsonl_file, offset=0)
        self._patch_scan(monitor, "sid", jsonl_file)

        events: list[TranscriptEvent] = []

        async def on_event(ev: TranscriptEvent) -> None:
            events.append(ev)

        monitor.set_event_callback(on_event)

        msgs = await monitor.check_for_updates({"sid"})

        assert len(events) == 1
        ev = events[0]
        assert ev.session_id == "sid"
        assert ev.role == "assistant"
        assert ev.block_type == "text"
        assert ev.stop_reason == "end_turn"
        assert ev.timestamp is not None
        assert ev.text == "hello world"
        assert ev.transcript_uuid == "evt-uuid-1"
        # Legacy NewMessage callback path still emits the message.
        assert len(msgs) == 1
        assert isinstance(msgs[0], NewMessage)
        assert msgs[0].text == "hello world"
        assert msgs[0].transcript_uuid == "evt-uuid-1"

    @pytest.mark.asyncio
    async def test_event_callback_carries_tool_use_metadata(
        self,
        monitor,
        tmp_path,
        make_jsonl_entry,
        make_tool_use_block,
        make_tool_result_block,
    ):
        jsonl_file = tmp_path / "session.jsonl"
        assistant_entry = make_jsonl_entry(
            "assistant",
            [make_tool_use_block("t1", "Read", {"file_path": "a.py"})],
            session_id="sid",
        )
        assistant_entry["message"]["stop_reason"] = "tool_use"
        user_entry = make_jsonl_entry(
            "user",
            [make_tool_result_block("t1", "ok")],
            session_id="sid",
        )
        self._write_jsonl(jsonl_file, [assistant_entry, user_entry])

        monitor.register_session("sid", jsonl_file, offset=0)
        self._patch_scan(monitor, "sid", jsonl_file)

        events: list[TranscriptEvent] = []
        msgs_seen: list[NewMessage] = []

        async def on_event(ev: TranscriptEvent) -> None:
            events.append(ev)

        monitor.set_event_callback(on_event)
        msgs_seen = await monitor.check_for_updates({"sid"})

        tool_use_events = [e for e in events if e.block_type == "tool_use"]
        tool_result_events = [e for e in events if e.block_type == "tool_result"]
        assert len(tool_use_events) == 1
        assert tool_use_events[0].tool_use_id == "t1"
        assert tool_use_events[0].tool_name == "Read"
        assert tool_use_events[0].stop_reason == "tool_use"
        assert len(tool_result_events) == 1
        assert tool_result_events[0].tool_use_id == "t1"
        # User-role message → no stop_reason on the resulting event.
        assert tool_result_events[0].stop_reason is None
        # Regression: NewMessage co-emission is preserved for both blocks.
        assert len(msgs_seen) == 2

    @pytest.mark.asyncio
    async def test_no_event_callback_still_emits_messages(
        self, monitor, tmp_path, make_jsonl_entry, make_text_block
    ):
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry("assistant", [make_text_block("hi")], session_id="sid")
        self._write_jsonl(jsonl_file, [entry])

        monitor.register_session("sid", jsonl_file, offset=0)
        self._patch_scan(monitor, "sid", jsonl_file)

        # No event callback set.
        msgs = await monitor.check_for_updates({"sid"})

        assert len(msgs) == 1
        assert msgs[0].text == "hi"

    @pytest.mark.asyncio
    async def test_event_callback_raises_does_not_block_messages(
        self, monitor, tmp_path, make_jsonl_entry, make_text_block, caplog
    ):
        """A raising event callback must not crash the loop nor suppress NewMessage."""
        import logging

        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(
            "assistant", [make_text_block("hello")], session_id="sid"
        )
        self._write_jsonl(jsonl_file, [entry])

        monitor.register_session("sid", jsonl_file, offset=0)
        self._patch_scan(monitor, "sid", jsonl_file)

        async def on_event(ev: TranscriptEvent) -> None:
            raise RuntimeError("boom")

        monitor.set_event_callback(on_event)

        with caplog.at_level(logging.ERROR, logger="ccbot.session_monitor"):
            # (i) does not crash
            msgs = await monitor.check_for_updates({"sid"})

        # (ii) NewMessage still emitted
        assert len(msgs) == 1
        assert isinstance(msgs[0], NewMessage)
        assert msgs[0].text == "hello"

        # (iii) error logged
        assert any(
            "Event callback error" in record.getMessage()
            and record.levelno == logging.ERROR
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_per_cycle_order_events_before_messages(
        self, monitor, tmp_path, make_jsonl_entry, make_text_block
    ):
        """All TranscriptEvents for a cycle must complete before any NewMessage."""
        jsonl_file = tmp_path / "session.jsonl"
        e1 = make_jsonl_entry("assistant", [make_text_block("one")], session_id="sid")
        e2 = make_jsonl_entry("assistant", [make_text_block("two")], session_id="sid")
        e3 = make_jsonl_entry("assistant", [make_text_block("three")], session_id="sid")
        self._write_jsonl(jsonl_file, [e1, e2, e3])

        monitor.register_session("sid", jsonl_file, offset=0)
        self._patch_scan(monitor, "sid", jsonl_file)

        order: list[tuple[str, str, str]] = []

        async def on_event(ev: TranscriptEvent) -> None:
            order.append(("event", ev.session_id, ev.text))

        async def on_message(msg: NewMessage) -> None:
            order.append(("message", msg.session_id, msg.text))

        monitor.set_event_callback(on_event)
        monitor.set_message_callback(on_message)

        # Drive the same control flow as _monitor_loop: check_for_updates
        # awaits all events for the cycle inline, then the loop dispatches
        # messages.
        msgs = await monitor.check_for_updates({"sid"})
        for msg in msgs:
            await on_message(msg)

        assert len(order) == 6
        kinds = [k for (k, _, _) in order]
        # All three events fire before any of the three messages.
        assert kinds == ["event", "event", "event", "message", "message", "message"]
        # And payloads come through in source order on each side.
        event_texts = [t for (k, _, t) in order if k == "event"]
        message_texts = [t for (k, _, t) in order if k == "message"]
        assert event_texts == ["one", "two", "three"]
        assert message_texts == ["one", "two", "three"]

    @pytest.mark.asyncio
    async def test_multi_block_message_emits_event_per_block(
        self,
        monitor,
        tmp_path,
        make_jsonl_entry,
        make_thinking_block,
        make_tool_use_block,
    ):
        """One assistant message with thinking + tool_use → two events sharing
        stop_reason and timestamp."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(
            "assistant",
            [
                make_thinking_block("planning the call"),
                make_tool_use_block("t1", "Read", {"file_path": "a.py"}),
            ],
            session_id="sid",
            timestamp="2026-05-02T12:00:00.000Z",
        )
        entry["message"]["stop_reason"] = "tool_use"
        self._write_jsonl(jsonl_file, [entry])

        monitor.register_session("sid", jsonl_file, offset=0)
        self._patch_scan(monitor, "sid", jsonl_file)

        events: list[TranscriptEvent] = []

        async def on_event(ev: TranscriptEvent) -> None:
            events.append(ev)

        monitor.set_event_callback(on_event)

        await monitor.check_for_updates({"sid"})

        block_types = [e.block_type for e in events]
        assert "thinking" in block_types
        assert "tool_use" in block_types
        assert len(events) == 2

        # Both events carry the SAME stop_reason and timestamp from the
        # parent JSONL message.
        assert all(e.stop_reason == "tool_use" for e in events)
        assert all(e.timestamp == "2026-05-02T12:00:00.000Z" for e in events)
