"""Scenario: route_runtime snapshot interface (architecture floor).

End-to-end coverage that the snapshot returned by ``route_runtime`` is
the authoritative view of a route's state. Walks one route through the
same lifecycle as
``test_route_busy_lifecycle.test_full_tool_turn_walks_states`` and asserts
via the ``RouteRuntimeSnapshot`` shape:

  - ``typing_eligible`` is True iff Claude is actively producing output.
  - ``status_card_visible`` flips when message_queue records a card.
  - ``context_usage`` flows through the snapshot, including the
    1M-cap latch.
  - ``mark_session_reset`` is symmetric with ``/clear`` mid-stream.
  - ``mark_pane_idle`` reconciles a stuck route to IDLE_CLEARED without
    fighting WAITING_ON_USER.
  - The adapter (``transcript_event_adapter``) is exercised, not just
    raw ``route_runtime`` — wiring is verified.

This file is part of the ``@pytest.mark.scenario`` net; production-code
changes must keep it green — breaking the snapshot semantics breaks an
external consumer contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from cctelegram import route_runtime, transcript_event_adapter
from cctelegram.route_runtime import ContextUsage, RunState
from cctelegram.session_monitor import TranscriptEvent
from tests.conftest import ScenarioHarness


pytestmark = pytest.mark.scenario


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


@pytest.fixture
def _enable_route_runtime_v2() -> None:
    """No-op kept so existing scenario signatures stay stable.

    route_runtime is now the sole run-state authority — there is no flag to
    flip. The conftest's ``_reset_all_handler_state`` drops the snapshot
    state between tests so leakage is impossible.
    """
    return None


@pytest.mark.asyncio
async def test_full_tool_turn_visible_through_snapshot(
    scenario: ScenarioHarness, _enable_route_runtime_v2: None
) -> None:
    """A single-tool turn walks RUNNING_TOOL → RUNNING → IDLE_RECENT."""
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    route: route_runtime.Route = (scenario.user_id, 42, wid)

    # tool_use → RUNNING_TOOL, typing_eligible True
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
    assert snaps[0].typing_eligible is True
    assert snaps[0].open_tools == frozenset({"t1"})

    # tool_result → RUNNING
    snaps = await transcript_event_adapter.dispatch_transcript_event(
        _event(role="user", block_type="tool_result", tool_use_id="t1"),
        [route],
    )
    assert snaps[0].run_state is RunState.RUNNING
    assert snaps[0].open_tools == frozenset()
    assert snaps[0].typing_eligible is True

    # end_turn text → IDLE_RECENT, typing_eligible False
    snaps = await transcript_event_adapter.dispatch_transcript_event(
        _event(block_type="text", stop_reason="end_turn", text="done"),
        [route],
    )
    assert snaps[0].run_state is RunState.IDLE_RECENT
    assert snaps[0].typing_eligible is False
    assert snaps[0].idle_clear_at is not None


@pytest.mark.asyncio
async def test_interactive_tool_blocks_typing_indicator(
    scenario: ScenarioHarness, _enable_route_runtime_v2: None
) -> None:
    """``AskUserQuestion`` should publish ``WAITING_ON_USER`` with
    ``typing_eligible=False`` — Claude is waiting on the user, not the
    other way around."""
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    route: route_runtime.Route = (scenario.user_id, 42, wid)

    snaps = await transcript_event_adapter.dispatch_transcript_event(
        _event(
            block_type="tool_use",
            tool_use_id="auq-1",
            tool_name="AskUserQuestion",
            stop_reason="tool_use",
        ),
        [route],
    )
    snap = snaps[0]
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.typing_eligible is False
    assert snap.waiting_on_user_tools == frozenset({"auq-1"})

    # mark_pane_idle MUST NOT clear an open interactive prompt.
    after_pane = await route_runtime.mark_pane_idle(route)
    assert after_pane.run_state is RunState.WAITING_ON_USER


@pytest.mark.asyncio
async def test_session_reset_clears_in_flight_tools(
    scenario: ScenarioHarness, _enable_route_runtime_v2: None
) -> None:
    """``/clear`` mid-stream drops ``open_tools`` for the dead session
    but preserves the status-card msg_id (mq may want to re-edit it for
    the new session's first reply)."""
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    route: route_runtime.Route = (scenario.user_id, 42, wid)

    await transcript_event_adapter.dispatch_transcript_event(
        _event(
            block_type="tool_use",
            tool_use_id="t1",
            tool_name="Bash",
            stop_reason="tool_use",
        ),
        [route],
    )
    route_runtime.mark_status_card_published(route, msg_id=12345)
    transcript_event_adapter.dispatch_context_usage([route], 50_000, "claude")

    snap = await route_runtime.mark_session_reset(route)
    assert snap.run_state is RunState.IDLE_CLEARED
    assert snap.open_tools == frozenset()
    assert snap.context_usage is None
    # Status-card msg_id outlives the reset.
    assert snap.status_card_msg_id == 12345


@pytest.mark.asyncio
async def test_context_usage_latches_one_million_through_adapter(
    scenario: ScenarioHarness, _enable_route_runtime_v2: None
) -> None:
    """Once a route is observed above 200k, the 1M cap latches."""
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    route: route_runtime.Route = (scenario.user_id, 42, wid)

    # First observation crosses the latch threshold.
    transcript_event_adapter.dispatch_context_usage(
        [route], tokens=250_000, model="claude"
    )
    snap = route_runtime.snapshot(route)
    assert snap.context_usage == ContextUsage(tokens=250_000, max_tokens=1_000_000)

    # Subsequent observation below threshold keeps the 1M cap.
    transcript_event_adapter.dispatch_context_usage(
        [route], tokens=80_000, model="claude"
    )
    snap = route_runtime.snapshot(route)
    assert snap.context_usage == ContextUsage(tokens=80_000, max_tokens=1_000_000)


@pytest.mark.asyncio
async def test_pane_idle_drops_lingering_tools_when_v2(
    scenario: ScenarioHarness, _enable_route_runtime_v2: None
) -> None:
    """Pane-idle backstop reconciles a stuck route to IDLE_CLEARED.

    The pane snapshot is a reconciliation event with lower authority than
    transcript lifecycle, so it preserves WAITING_ON_USER but clears
    RUNNING / RUNNING_TOOL after the debounce.
    """
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    route: route_runtime.Route = (scenario.user_id, 42, wid)

    await transcript_event_adapter.dispatch_transcript_event(
        _event(
            block_type="tool_use",
            tool_use_id="leaked",
            tool_name="Bash",
            stop_reason="tool_use",
        ),
        [route],
    )
    assert route_runtime.snapshot(route).run_state is RunState.RUNNING_TOOL

    after = await route_runtime.mark_pane_idle(route)
    assert after.run_state is RunState.IDLE_CLEARED
    assert after.open_tools == frozenset()


@pytest.mark.asyncio
async def test_monotonic_seq_visible_to_subscribers(
    scenario: ScenarioHarness, _enable_route_runtime_v2: None
) -> None:
    """Subscribers observe snapshots in commit order via ``monotonic_seq``.

    Property a future Slack/Discord transport adapter will need to
    detect dropped notifications and resync from ``snapshot()``.
    """
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    route: route_runtime.Route = (scenario.user_id, 42, wid)

    observed_seqs: list[int] = []

    async def observer(snap: route_runtime.RouteRuntimeSnapshot) -> None:
        observed_seqs.append(snap.monotonic_seq)

    unsubscribe = route_runtime.subscribe(route, observer)
    try:
        await route_runtime.mark_inbound_sent(route)
        await transcript_event_adapter.dispatch_transcript_event(
            _event(
                block_type="tool_use",
                tool_use_id="t1",
                tool_name="Bash",
                stop_reason="tool_use",
            ),
            [route],
        )
        await transcript_event_adapter.dispatch_transcript_event(
            _event(role="user", block_type="tool_result", tool_use_id="t1"),
            [route],
        )
    finally:
        unsubscribe()

    assert observed_seqs == sorted(observed_seqs)
    assert len(set(observed_seqs)) == 3


@pytest.mark.asyncio
async def test_event_callback_wiring_drives_route_runtime(
    scenario: ScenarioHarness,
) -> None:
    """Integration: bot.py's ``event_callback`` body resolves routes via
    ``session_manager.find_users_for_session`` and feeds
    ``transcript_event_adapter.dispatch_transcript_event``. We verify the
    route_runtime snapshot and the activity-digest renderer arrive at the
    expected state through the real adapter wiring.
    """
    from cctelegram.session import session_manager

    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(thread_id=42, window_id=wid, display_name="repo", cwd="/repo")
    # Bind the session_id so ``find_users_for_session`` returns this
    # route. ``get_window_state`` lazily creates the WindowState slot
    # which session_id can then be written into directly — same shape
    # ``load_session_map`` uses.
    session_manager.get_window_state(wid).session_id = "sess-int-1"

    # Replicate ``bot.py::create_bot``'s event_callback body (now a single
    # route_runtime feed via the adapter).
    async def event_callback(
        event: route_runtime.TranscriptLifecycleEvent | object,
    ) -> None:
        active = await session_manager.find_users_for_session(event.session_id)  # type: ignore[attr-defined]
        if not active:
            return
        routes = [(user_id, thread_id or 0, wid) for user_id, wid, thread_id in active]
        await transcript_event_adapter.dispatch_transcript_event(
            event,
            routes,  # type: ignore[arg-type]
        )

    await event_callback(
        _event(
            session_id="sess-int-1",
            block_type="tool_use",
            tool_use_id="int-1",
            tool_name="Bash",
            stop_reason="tool_use",
        )
    )

    route: route_runtime.Route = (scenario.user_id, 42, wid)
    snap = route_runtime.snapshot(route)
    assert snap.run_state is RunState.RUNNING_TOOL
    assert snap.open_tools == frozenset({"int-1"})

    # End the turn — IDLE_RECENT after tool_result + end_turn.
    await event_callback(
        _event(
            session_id="sess-int-1",
            role="user",
            block_type="tool_result",
            tool_use_id="int-1",
        )
    )
    await event_callback(
        _event(
            session_id="sess-int-1",
            block_type="text",
            stop_reason="end_turn",
        )
    )
    assert route_runtime.snapshot(route).run_state is RunState.IDLE_RECENT

    # message_queue's activity-digest renderer reads from route_runtime —
    # verify it picks up the same state.
    from cctelegram.handlers import message_queue

    state = message_queue.ActivityDigestState(message_id=0, window_id=wid)
    state.tool_count = 1
    state.completed_count = 1
    state.done = False
    rendered = message_queue._render_activity_digest(state, route=route)
    assert rendered.startswith("✅ Done")  # IDLE_RECENT → "✅ Done"
