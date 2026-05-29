"""Scenario: route busy lifecycle (c313657 regression).

Commit ``c313657 busy: wire activity callback to re-arm idle-clear on events``
fixed a class of bugs where:

  - A route reached ``IDLE_CLEARED`` (idle delay elapsed, pane scrape said
    no spinner), so the debounced "🟡 Busy" card was cleared.
  - The next sub-agent / quick-tool turn finished between two 10-second
    pane scrapes — the only re-arm signal was a pane scrape that showed
    ``is_running == True``.
  - The run-state machine accumulated open tools, the typing indicator ran
    forever, no card was ever published.

``route_runtime`` owns the idle-clear debounce deadline
(``arm_pane_idle_clear`` / ``pane_idle_clear_due`` /
``commit_pane_idle_clear``); real transcript / inbound activity re-arms it
inside ``route_runtime`` (``ingest_transcript_event`` /
``mark_inbound_sent``). This scenario asserts the c313657 guard (activity
cancels a pending clear) and the debounce timing (card stays during the
delay, clears once after).
"""

from __future__ import annotations

from typing import Any

import pytest

from cctelegram import route_runtime, transcript_event_adapter
from cctelegram.route_runtime import RunState
from cctelegram.session_monitor import TranscriptEvent
from tests.conftest import ScenarioHarness


pytestmark = pytest.mark.scenario

_DELAY = route_runtime.IDLE_CLEAR_DELAY_SECONDS


def _event(**kw: Any) -> TranscriptEvent:
    defaults: dict[str, Any] = dict(
        session_id="sess-1",
        role="assistant",
        block_type="text",
        tool_use_id=None,
        tool_name=None,
        stop_reason=None,
        timestamp=None,
        text="",
        image_data=None,
    )
    defaults.update(kw)
    return TranscriptEvent(**defaults)


@pytest.mark.asyncio
async def test_debounce_holds_then_clears_once(
    scenario: ScenarioHarness,
) -> None:
    """The card stays up during IDLE_CLEAR_DELAY_SECONDS of confirmed idle
    and clears exactly once after — owned by route_runtime."""
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    route = (scenario.user_id, 42, wid)

    # First confirmed-idle observation arms the deadline.
    route_runtime.arm_pane_idle_clear(route, now=100.0)
    assert route_runtime.snapshot(route).pane_idle_clear_at == 100.0 + _DELAY

    # Inside the window: not due, card must stay.
    assert route_runtime.pane_idle_clear_due(route, now=100.0 + _DELAY - 0.5) is False

    # After the window: due → commit clears exactly once (returns True).
    assert route_runtime.pane_idle_clear_due(route, now=100.0 + _DELAY) is True
    assert await route_runtime.commit_pane_idle_clear(route, now=100.0 + _DELAY) is True
    assert route_runtime.snapshot(route).run_state is RunState.IDLE_CLEARED

    # A second arm in the same stretch is a no-op (cleared once).
    route_runtime.arm_pane_idle_clear(route, now=200.0)
    assert route_runtime.pane_idle_clear_due(route, now=10_000.0) is False


@pytest.mark.asyncio
async def test_transcript_event_cancels_pending_clear(
    scenario: ScenarioHarness,
) -> None:
    """c313657 guard: a transcript event during the debounce window cancels
    the pending clear (route_runtime re-arm), so the card is NOT cleared
    even though the original deadline elapsed."""
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    route = (scenario.user_id, 42, wid)

    route_runtime.arm_pane_idle_clear(route, now=100.0)
    assert route_runtime.pane_idle_clear_due(route, now=100.0 + _DELAY) is True

    # Real transcript activity lands before the poller commits the clear.
    await transcript_event_adapter.dispatch_transcript_event(
        _event(
            block_type="tool_use",
            tool_use_id="t1",
            tool_name="Bash",
            stop_reason="tool_use",
        ),
        [route],
    )

    # Pending clear cancelled — not due any more.
    assert route_runtime.pane_idle_clear_due(route, now=100.0 + _DELAY) is False
    assert route_runtime.snapshot(route).pane_idle_clear_at is None
    assert route_runtime.snapshot(route).run_state is RunState.RUNNING_TOOL


@pytest.mark.asyncio
async def test_inbound_sent_cancels_pending_clear(
    scenario: ScenarioHarness,
) -> None:
    """Inbound prompt delivery is also real activity and cancels a pending
    clear."""
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    route = (scenario.user_id, 42, wid)

    route_runtime.arm_pane_idle_clear(route, now=100.0)
    await route_runtime.mark_inbound_sent(route)

    assert route_runtime.pane_idle_clear_due(route, now=100.0 + _DELAY) is False
    assert route_runtime.snapshot(route).pane_idle_clear_at is None


@pytest.mark.asyncio
async def test_full_tool_turn_walks_states(
    scenario: ScenarioHarness,
) -> None:
    """Single-tool turn walks RUNNING_TOOL → RUNNING → IDLE_RECENT via the
    route_runtime snapshot."""
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    route = (scenario.user_id, 42, wid)

    snaps = await transcript_event_adapter.dispatch_transcript_event(
        _event(
            block_type="tool_use",
            tool_use_id="t1",
            tool_name="Bash",
            stop_reason="tool_use",
        ),
        [route],
    )
    assert snaps[0].run_state is RunState.RUNNING_TOOL

    snaps = await transcript_event_adapter.dispatch_transcript_event(
        _event(role="user", block_type="tool_result", tool_use_id="t1"),
        [route],
    )
    assert snaps[0].run_state is RunState.RUNNING

    snaps = await transcript_event_adapter.dispatch_transcript_event(
        _event(block_type="text", stop_reason="end_turn", text="done"),
        [route],
    )
    assert snaps[0].run_state is RunState.IDLE_RECENT
