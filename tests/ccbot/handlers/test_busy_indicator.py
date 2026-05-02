"""Tests for the event-driven RunState machine.

Covers the §2.2.1 transition table:
  - Single-tool turn walks RUNNING_TOOL → RUNNING → IDLE_RECENT → IDLE_CLEARED
  - Parallel-tool turn tracks both ids; clears only when both close
  - Long single tool stays RUNNING_TOOL across no-event gaps
  - Thinking-only with stop_reason="tool_use" does NOT transition
  - end_turn / stop_sequence with no open tools moves to IDLE_RECENT,
    decays to IDLE_CLEARED after IDLE_CLEAR_DELAY_SECONDS
  - Interactive tool (AskUserQuestion) → WAITING_ON_USER, then RUNNING on result
  - BROKEN_TOPIC restored on next event
  - context_pct cache round-trip
"""

from __future__ import annotations

import pytest

from ccbot.handlers import busy_indicator
from ccbot.handlers.busy_indicator import RunState
from ccbot.session_monitor import TranscriptEvent

ROUTE: busy_indicator.Route = (1, 42, "@7")


def _event(
    *,
    role: str,
    block_type: str,
    tool_use_id: str | None = None,
    tool_name: str | None = None,
    stop_reason: str | None = None,
    text: str = "",
) -> TranscriptEvent:
    return TranscriptEvent(
        session_id="sess-1",
        role=role,  # type: ignore[arg-type]
        block_type=block_type,  # type: ignore[arg-type]
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        stop_reason=stop_reason,
        timestamp=None,
        text=text,
        image_data=None,
    )


@pytest.fixture(autouse=True)
def _reset():
    busy_indicator.reset_for_tests()
    yield
    busy_indicator.reset_for_tests()


@pytest.mark.asyncio
async def test_single_tool_turn_walks_states(monkeypatch: pytest.MonkeyPatch):
    # Pretend "now" advances on demand so we can test idle decay.
    fake_now = [1000.0]
    monkeypatch.setattr(busy_indicator, "_now", lambda: fake_now[0])

    # 1. Thinking with stop_reason=tool_use from idle → RUNNING.
    # Preliminary thinking is the most common pre-output signal; lighting
    # the indicator here closes the gap before the first text/tool_use.
    await busy_indicator.on_transcript_event(
        _event(role="assistant", block_type="thinking", stop_reason="tool_use"),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING

    # 2. tool_use → RUNNING_TOOL
    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="tool_use",
            tool_use_id="t1",
            tool_name="Bash",
            stop_reason="tool_use",
        ),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING_TOOL

    # 3. tool_result → RUNNING (open_tools empty)
    await busy_indicator.on_transcript_event(
        _event(role="assistant", block_type="tool_result", tool_use_id="t1"),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING

    # 4. final text with end_turn → IDLE_RECENT
    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="text",
            stop_reason="end_turn",
            text="all done",
        ),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.IDLE_RECENT

    # 5. Advance time past the decay window → IDLE_CLEARED on read
    fake_now[0] += busy_indicator.IDLE_CLEAR_DELAY_SECONDS + 0.1
    assert busy_indicator.state(ROUTE) is RunState.IDLE_CLEARED


@pytest.mark.asyncio
async def test_parallel_tool_use_tracks_both_ids():
    # Two tool_use blocks in one assistant message
    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="tool_use",
            tool_use_id="A",
            tool_name="Bash",
            stop_reason="tool_use",
        ),
        [ROUTE],
    )
    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="tool_use",
            tool_use_id="B",
            tool_name="Read",
            stop_reason="tool_use",
        ),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING_TOOL

    # First tool_result: still RUNNING_TOOL because B is open
    await busy_indicator.on_transcript_event(
        _event(role="assistant", block_type="tool_result", tool_use_id="A"),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING_TOOL

    # Second tool_result: open_tools empty → RUNNING
    await busy_indicator.on_transcript_event(
        _event(role="assistant", block_type="tool_result", tool_use_id="B"),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING


@pytest.mark.asyncio
async def test_long_single_tool_stays_running_tool(monkeypatch: pytest.MonkeyPatch):
    fake_now = [1000.0]
    monkeypatch.setattr(busy_indicator, "_now", lambda: fake_now[0])

    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="tool_use",
            tool_use_id="bash-1",
            tool_name="Bash",
            stop_reason="tool_use",
        ),
        [ROUTE],
    )
    # 60s gap with no events: state must NOT decay (only IDLE_RECENT decays).
    fake_now[0] += 60.0
    assert busy_indicator.state(ROUTE) is RunState.RUNNING_TOOL


@pytest.mark.asyncio
async def test_thinking_only_with_tool_use_stop_reason_no_transition():
    # Force a starting state that's not IDLE_CLEARED so we can prove we don't move.
    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="tool_use",
            tool_use_id="x",
            tool_name="Bash",
            stop_reason="tool_use",
        ),
        [ROUTE],
    )
    before = busy_indicator.state(ROUTE)
    assert before is RunState.RUNNING_TOOL

    # Thinking with stop_reason=tool_use must not change anything.
    await busy_indicator.on_transcript_event(
        _event(
            role="assistant", block_type="thinking", stop_reason="tool_use", text="..."
        ),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING_TOOL


@pytest.mark.asyncio
async def test_end_turn_with_no_tools_decays_idle(monkeypatch: pytest.MonkeyPatch):
    fake_now = [1000.0]
    monkeypatch.setattr(busy_indicator, "_now", lambda: fake_now[0])

    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="text",
            stop_reason="end_turn",
            text="ok done.",
        ),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.IDLE_RECENT

    # Just shy of the decay delay: still recent
    fake_now[0] += busy_indicator.IDLE_CLEAR_DELAY_SECONDS - 0.5
    assert busy_indicator.state(ROUTE) is RunState.IDLE_RECENT

    # Past the delay: cleared on read
    fake_now[0] += 1.0
    assert busy_indicator.state(ROUTE) is RunState.IDLE_CLEARED


@pytest.mark.asyncio
async def test_interactive_tool_waiting_on_user():
    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="tool_use",
            tool_use_id="ask-1",
            tool_name="AskUserQuestion",
            stop_reason="tool_use",
        ),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.WAITING_ON_USER

    # Result closes it → RUNNING (open_tools empty)
    await busy_indicator.on_transcript_event(
        _event(role="assistant", block_type="tool_result", tool_use_id="ask-1"),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING


@pytest.mark.asyncio
async def test_broken_topic_recovery_restores_prior_state():
    # Walk to RUNNING_TOOL.
    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="tool_use",
            tool_use_id="t",
            tool_name="Bash",
            stop_reason="tool_use",
        ),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING_TOOL

    # Simulate a topic-broken event: classifier flips us to BROKEN_TOPIC.
    await busy_indicator.mark_topic_broken(ROUTE)
    assert busy_indicator.state(ROUTE) is RunState.BROKEN_TOPIC

    # Next OK transcript event recovers prior state and applies the rule.
    # tool_result for the open tool → open_tools empty → RUNNING.
    await busy_indicator.on_transcript_event(
        _event(role="assistant", block_type="tool_result", tool_use_id="t"),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING


@pytest.mark.asyncio
async def test_stale_tool_result_ignored():
    # tool_result for an unknown id while idle: do not transition, do not crash.
    await busy_indicator.on_transcript_event(
        _event(role="assistant", block_type="tool_result", tool_use_id="ghost"),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.IDLE_CLEARED


def test_context_pct_cache_round_trip():
    busy_indicator.update_context_pct(ROUTE, 89)
    assert busy_indicator.context_pct(ROUTE) == 89
    busy_indicator.update_context_pct(ROUTE, None)
    assert busy_indicator.context_pct(ROUTE) is None


def test_clear_route_drops_state():
    busy_indicator.update_context_pct(ROUTE, 50)
    busy_indicator._run_state[ROUTE] = RunState.RUNNING_TOOL
    busy_indicator._open_tools[ROUTE] = {"x": False}

    busy_indicator.clear_route(ROUTE)
    assert busy_indicator.state(ROUTE) is RunState.IDLE_CLEARED
    assert busy_indicator.context_pct(ROUTE) is None


@pytest.mark.asyncio
async def test_state_callback_fires_on_transition():
    seen: list[tuple[RunState, RunState]] = []

    async def cb(route: busy_indicator.Route, old: RunState, new: RunState) -> None:
        assert route == ROUTE
        seen.append((old, new))

    busy_indicator.register_state_callback(cb)

    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="tool_use",
            tool_use_id="t1",
            tool_name="Bash",
            stop_reason="tool_use",
        ),
        [ROUTE],
    )
    assert seen == [(RunState.IDLE_CLEARED, RunState.RUNNING_TOOL)]


@pytest.mark.asyncio
async def test_parallel_interactive_and_non_interactive_tools():
    """Locks Bug 1: closing the interactive tool must drop back to RUNNING_TOOL
    while a non-interactive tool is still pending — not stay WAITING_ON_USER."""
    # 1. Bash (non-interactive) opens → RUNNING_TOOL.
    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="tool_use",
            tool_use_id="bash-1",
            tool_name="Bash",
            stop_reason="tool_use",
        ),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING_TOOL

    # 2. AskUserQuestion (interactive) opens alongside → WAITING_ON_USER.
    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="tool_use",
            tool_use_id="ask-1",
            tool_name="AskUserQuestion",
            stop_reason="tool_use",
        ),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.WAITING_ON_USER

    # 3. tool_result for AskUQ — only Bash remains → must drop to RUNNING_TOOL.
    # This is the regression: pre-fix code kept the route stuck at
    # WAITING_ON_USER because open_tools was non-empty.
    await busy_indicator.on_transcript_event(
        _event(role="assistant", block_type="tool_result", tool_use_id="ask-1"),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING_TOOL

    # 4. tool_result for Bash → open_tools empty → RUNNING.
    await busy_indicator.on_transcript_event(
        _event(role="assistant", block_type="tool_result", tool_use_id="bash-1"),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING


@pytest.mark.asyncio
async def test_mark_topic_recovered_restores_prior_state():
    """Locks Bug 2: BROKEN_TOPIC has an explicit recovery path that fires
    callbacks and restores the pre-broken state without needing a new
    JSONL event."""
    seen: list[tuple[RunState, RunState]] = []

    async def cb(route: busy_indicator.Route, old: RunState, new: RunState) -> None:
        seen.append((old, new))

    # 1. Walk to RUNNING via a tool_use + tool_result.
    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="tool_use",
            tool_use_id="t",
            tool_name="Bash",
            stop_reason="tool_use",
        ),
        [ROUTE],
    )
    await busy_indicator.on_transcript_event(
        _event(role="assistant", block_type="tool_result", tool_use_id="t"),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING

    # Register the callback after the warm-up transitions so we only see the
    # broken/recovered pair.
    busy_indicator.register_state_callback(cb)

    # 2. Mark broken.
    await busy_indicator.mark_topic_broken(ROUTE)
    assert busy_indicator.state(ROUTE) is RunState.BROKEN_TOPIC

    # 3. Explicit recovery — back to RUNNING without any TranscriptEvent.
    await busy_indicator.mark_topic_recovered(ROUTE)
    assert busy_indicator.state(ROUTE) is RunState.RUNNING

    # 4. Both transitions visible to the callback.
    assert (RunState.RUNNING, RunState.BROKEN_TOPIC) in seen
    assert (RunState.BROKEN_TOPIC, RunState.RUNNING) in seen


@pytest.mark.asyncio
async def test_mark_topic_broken_idempotent_preserves_original_prior():
    # Walk to RUNNING_TOOL.
    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="tool_use",
            tool_use_id="t",
            tool_name="Bash",
            stop_reason="tool_use",
        ),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING_TOOL

    # First mark_topic_broken: prior = RUNNING_TOOL.
    await busy_indicator.mark_topic_broken(ROUTE)
    # Second call: must NOT overwrite the prior with BROKEN_TOPIC.
    await busy_indicator.mark_topic_broken(ROUTE)

    # Recovery still restores RUNNING_TOOL, not BROKEN_TOPIC.
    await busy_indicator.mark_topic_recovered(ROUTE)
    assert busy_indicator.state(ROUTE) is RunState.RUNNING_TOOL


@pytest.mark.asyncio
async def test_mark_topic_recovered_noop_when_not_broken():
    # Walk to RUNNING_TOOL.
    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="tool_use",
            tool_use_id="t",
            tool_name="Bash",
            stop_reason="tool_use",
        ),
        [ROUTE],
    )
    # Recovery on a not-broken route: no-op, state preserved.
    await busy_indicator.mark_topic_recovered(ROUTE)
    assert busy_indicator.state(ROUTE) is RunState.RUNNING_TOOL


@pytest.mark.asyncio
async def test_mid_turn_assistant_text_with_open_tool_stays_running_tool():
    """Locks the architect's invariant: assistant text events while a tool is
    still open must NOT downgrade the route from RUNNING_TOOL."""
    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="tool_use",
            tool_use_id="bash-1",
            tool_name="Bash",
            stop_reason="tool_use",
        ),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING_TOOL

    # Assistant text with stop_reason=tool_use — the model is talking on its
    # way to invoking another tool.
    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="text",
            stop_reason="tool_use",
            text="Now I'll inspect the file.",
        ),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING_TOOL

    # Mid-turn streaming text with no stop_reason at all.
    await busy_indicator.on_transcript_event(
        _event(role="assistant", block_type="text", stop_reason=None, text="…"),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING_TOOL


@pytest.mark.asyncio
async def test_register_state_callback_dedupes_by_identity():
    calls: list[int] = []

    async def cb(route: busy_indicator.Route, old: RunState, new: RunState) -> None:
        calls.append(1)

    busy_indicator.register_state_callback(cb)
    busy_indicator.register_state_callback(cb)  # duplicate — should be ignored
    busy_indicator.register_state_callback(cb)  # third — also ignored

    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="tool_use",
            tool_use_id="t1",
            tool_name="Bash",
            stop_reason="tool_use",
        ),
        [ROUTE],
    )
    assert calls == [1]


@pytest.mark.asyncio
async def test_state_callback_exception_does_not_block_others(
    caplog: pytest.LogCaptureFixture,
):
    seen_b: list[tuple[RunState, RunState]] = []

    async def cb_a(route: busy_indicator.Route, old: RunState, new: RunState) -> None:
        raise RuntimeError("boom from cb_a")

    async def cb_b(route: busy_indicator.Route, old: RunState, new: RunState) -> None:
        seen_b.append((old, new))

    busy_indicator.register_state_callback(cb_a)
    busy_indicator.register_state_callback(cb_b)

    import logging

    with caplog.at_level(logging.ERROR, logger=busy_indicator.logger.name):
        await busy_indicator.on_transcript_event(
            _event(
                role="assistant",
                block_type="tool_use",
                tool_use_id="t1",
                tool_name="Bash",
                stop_reason="tool_use",
            ),
            [ROUTE],
        )

    # The transition still landed.
    assert busy_indicator.state(ROUTE) is RunState.RUNNING_TOOL
    # cb_b ran despite cb_a raising.
    assert seen_b == [(RunState.IDLE_CLEARED, RunState.RUNNING_TOOL)]
    # The error was logged.
    assert any("state callback error" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_mark_inbound_sent_from_idle_transitions_running():
    # Fresh route — default visible state IDLE_CLEARED.
    assert busy_indicator.state(ROUTE) is RunState.IDLE_CLEARED
    await busy_indicator.mark_inbound_sent(ROUTE)
    assert busy_indicator.state(ROUTE) is RunState.RUNNING


@pytest.mark.asyncio
async def test_mark_inbound_sent_does_not_downgrade_running_tool():
    # Walk to RUNNING_TOOL via a tool_use.
    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="tool_use",
            tool_use_id="t1",
            tool_name="Bash",
            stop_reason="tool_use",
        ),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING_TOOL

    # A second user prompt while a tool is still open: must not clobber
    # RUNNING_TOOL with RUNNING. Open tools still gate the state.
    await busy_indicator.mark_inbound_sent(ROUTE)
    assert busy_indicator.state(ROUTE) is RunState.RUNNING_TOOL


@pytest.mark.asyncio
async def test_mark_inbound_sent_does_not_overwrite_broken_topic():
    # Walk to RUNNING then mark broken.
    await busy_indicator.on_transcript_event(
        _event(role="assistant", block_type="text", text="hi"),
        [ROUTE],
    )
    await busy_indicator.mark_topic_broken(ROUTE)
    assert busy_indicator.state(ROUTE) is RunState.BROKEN_TOPIC

    # mark_inbound_sent must not pretend recovery happened — recovery is the
    # next real transcript event's job. Stay BROKEN_TOPIC.
    await busy_indicator.mark_inbound_sent(ROUTE)
    assert busy_indicator.state(ROUTE) is RunState.BROKEN_TOPIC


@pytest.mark.asyncio
async def test_thinking_from_idle_transitions_running(monkeypatch: pytest.MonkeyPatch):
    # IDLE_RECENT → RUNNING on subsequent thinking. Set up by ending a turn.
    fake_now = [1000.0]
    monkeypatch.setattr(busy_indicator, "_now", lambda: fake_now[0])

    await busy_indicator.on_transcript_event(
        _event(
            role="assistant",
            block_type="text",
            stop_reason="end_turn",
            text="done",
        ),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.IDLE_RECENT

    # New thinking event: thinking-from-idle should fire.
    await busy_indicator.on_transcript_event(
        _event(role="assistant", block_type="thinking", stop_reason="tool_use"),
        [ROUTE],
    )
    assert busy_indicator.state(ROUTE) is RunState.RUNNING
