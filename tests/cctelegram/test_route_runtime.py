"""Unit tests for ``cctelegram.route_runtime`` — the Wave B snapshot
state machine.

Mirrors ``test_busy_indicator.py``'s transition coverage so a regression
in either path is visible immediately. Plus tests specific to the
snapshot interface that don't have a busy_indicator analogue:

  - Snapshots are frozen — mutating internal state does not change a
    captured snapshot.
  - ``monotonic_seq`` strictly increases across mutations.
  - Observers fire **after** commit and see the committed snapshot.
  - Per-route locks serialise within a route but do **not** serialise
    across routes.
  - ``mark_session_reset`` drops in-flight ``open_tools`` + context_usage.
  - Status card publish/clear bookkeeping mutates only the
    ``status_card_*`` snapshot fields.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from cctelegram import route_runtime
from cctelegram.handlers.busy_indicator import ContextUsage, RunState
from cctelegram.route_runtime import (
    RouteRuntimeSnapshot,
    TranscriptLifecycleEvent,
)


ROUTE: route_runtime.Route = (1, 42, "@7")
ROUTE_2: route_runtime.Route = (1, 99, "@9")


def _evt(
    role: str = "assistant",
    block: str = "text",
    *,
    tool_use_id: str | None = None,
    tool_name: str | None = None,
    stop_reason: str | None = None,
) -> TranscriptLifecycleEvent:
    """Test-side TranscriptLifecycleEvent constructor with safe defaults."""
    return TranscriptLifecycleEvent(
        role=role,  # type: ignore[arg-type]
        block_type=block,  # type: ignore[arg-type]
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        stop_reason=stop_reason,
    )


@pytest.fixture(autouse=True)
def _reset() -> None:
    route_runtime.reset_for_tests()
    yield
    route_runtime.reset_for_tests()


# ── default snapshot ────────────────────────────────────────────────────


def test_default_snapshot_for_unknown_route():
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.IDLE_CLEARED
    assert snap.open_tools == frozenset()
    assert snap.waiting_on_user_tools == frozenset()
    assert snap.context_usage is None
    assert snap.idle_clear_at is None
    assert snap.typing_eligible is False
    assert snap.status_card_visible is False
    assert snap.status_card_msg_id is None
    assert snap.broken_topic is False


# ── transition table ────────────────────────────────────────────────────


async def test_tool_use_opens_running_tool():
    snap = await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("assistant", "tool_use", tool_use_id="tool-1", tool_name="Bash"),
    )
    assert snap.run_state is RunState.RUNNING_TOOL
    assert snap.open_tools == frozenset({"tool-1"})
    assert snap.waiting_on_user_tools == frozenset()
    assert snap.typing_eligible is True


async def test_interactive_tool_opens_waiting_on_user():
    snap = await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt(
            "assistant",
            "tool_use",
            tool_use_id="tool-1",
            tool_name="AskUserQuestion",
        ),
    )
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.waiting_on_user_tools == frozenset({"tool-1"})
    # WAITING_ON_USER is not typing-eligible — the user is the one acting.
    assert snap.typing_eligible is False


async def test_tool_result_closes_slot():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "tool_result", tool_use_id="t1")
    )
    assert snap.run_state is RunState.RUNNING
    assert snap.open_tools == frozenset()


async def test_tool_result_stale_id_ignored():
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("user", "tool_result", tool_use_id="never-opened")
    )
    # Unknown id → no state change, stays at default IDLE_CLEARED.
    assert snap.run_state is RunState.IDLE_CLEARED


async def test_end_of_turn_text_idle_recent():
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn")
    )
    assert snap.run_state is RunState.IDLE_RECENT
    assert snap.idle_clear_at is not None
    assert snap.idle_clear_at > snap.last_event_at


async def test_idle_recent_decays_on_read():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn")
    )
    # Force decay by reaching past the deadline.
    state = route_runtime._state[ROUTE]
    state.idle_clear_at = 0.0  # immediate decay
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.IDLE_CLEARED


async def test_thinking_lights_up_running_when_idle():
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "thinking")
    )
    assert snap.run_state is RunState.RUNNING


async def test_thinking_preserves_running_tool():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "thinking")
    )
    assert snap.run_state is RunState.RUNNING_TOOL


# ── mark_* mutations ────────────────────────────────────────────────────


async def test_mark_inbound_sent_idle_to_running():
    snap = await route_runtime.mark_inbound_sent(ROUTE)
    assert snap.run_state is RunState.RUNNING


async def test_mark_inbound_sent_preserves_running_tool():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    snap = await route_runtime.mark_inbound_sent(ROUTE)
    assert snap.run_state is RunState.RUNNING_TOOL


async def test_mark_pane_idle_preserves_waiting_on_user():
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt(
            "assistant",
            "tool_use",
            tool_use_id="t1",
            tool_name="AskUserQuestion",
        ),
    )
    snap = await route_runtime.mark_pane_idle(ROUTE)
    assert snap.run_state is RunState.WAITING_ON_USER


async def test_mark_pane_idle_drops_lingering_tools():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    snap = await route_runtime.mark_pane_idle(ROUTE)
    assert snap.run_state is RunState.IDLE_CLEARED
    assert snap.open_tools == frozenset()


async def test_mark_topic_broken_and_recovered_round_trip():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    broken = await route_runtime.mark_topic_broken(ROUTE)
    assert broken.broken_topic is True
    assert broken.run_state is RunState.BROKEN_TOPIC

    recovered = await route_runtime.mark_topic_recovered(ROUTE)
    assert recovered.broken_topic is False
    assert recovered.run_state is RunState.RUNNING_TOOL


async def test_mark_topic_broken_idempotent_preserves_prior():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    await route_runtime.mark_topic_broken(ROUTE)
    # Second call must NOT overwrite pre_broken_state with BROKEN_TOPIC.
    await route_runtime.mark_topic_broken(ROUTE)
    recovered = await route_runtime.mark_topic_recovered(ROUTE)
    assert recovered.run_state is RunState.RUNNING_TOOL


async def test_mark_session_reset_drops_open_tools_and_usage():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    route_runtime.update_context_usage(ROUTE, 50_000, "claude-opus-4-7")
    snap = await route_runtime.mark_session_reset(ROUTE)
    assert snap.run_state is RunState.IDLE_CLEARED
    assert snap.open_tools == frozenset()
    assert snap.context_usage is None


# ── status card lifecycle ───────────────────────────────────────────────


def test_status_card_published_and_cleared():
    assert route_runtime.snapshot(ROUTE).status_card_visible is False
    route_runtime.mark_status_card_published(ROUTE, msg_id=42)
    snap = route_runtime.snapshot(ROUTE)
    assert snap.status_card_visible is True
    assert snap.status_card_msg_id == 42

    route_runtime.mark_status_card_cleared(ROUTE)
    snap = route_runtime.snapshot(ROUTE)
    assert snap.status_card_visible is False
    assert snap.status_card_msg_id is None


def test_mark_session_reset_preserves_status_card():
    """Status-card msg_id outlives a session reset.

    message_queue may want to edit the existing card to render the new
    session's first reply rather than send a fresh card.
    """
    # Direct mark — synchronous, no await required.
    route_runtime.mark_status_card_published(ROUTE, msg_id=99)
    asyncio.run(route_runtime.mark_session_reset(ROUTE))
    snap = route_runtime.snapshot(ROUTE)
    assert snap.status_card_msg_id == 99


# ── context usage ───────────────────────────────────────────────────────


def test_update_context_usage_default_max_200k():
    route_runtime.update_context_usage(ROUTE, 50_000, "claude")
    snap = route_runtime.snapshot(ROUTE)
    assert snap.context_usage == ContextUsage(tokens=50_000, max_tokens=200_000)


def test_update_context_usage_latches_1m_after_overflow():
    # First observation crosses the 200k threshold → latch to 1M.
    route_runtime.update_context_usage(ROUTE, 250_000, "claude")
    assert route_runtime.snapshot(ROUTE).context_usage == ContextUsage(
        tokens=250_000, max_tokens=1_000_000
    )
    # Subsequent observation below 200k stays on 1M (latch preserved).
    route_runtime.update_context_usage(ROUTE, 80_000, "claude")
    assert route_runtime.snapshot(ROUTE).context_usage == ContextUsage(
        tokens=80_000, max_tokens=1_000_000
    )


def test_update_context_usage_none_drops_entry():
    route_runtime.update_context_usage(ROUTE, 50_000, "claude")
    route_runtime.update_context_usage(ROUTE, None, None)
    assert route_runtime.snapshot(ROUTE).context_usage is None


# ── seed_open_tools ─────────────────────────────────────────────────────


def test_seed_open_tools_no_op_for_empty_input():
    route_runtime.seed_open_tools(ROUTE, {})
    assert route_runtime.snapshot(ROUTE).run_state is RunState.IDLE_CLEARED


def test_seed_open_tools_populates_run_state():
    route_runtime.seed_open_tools(ROUTE, {"t1": False, "t2": True})
    snap = route_runtime.snapshot(ROUTE)
    # Mixed open tools with one interactive → WAITING_ON_USER.
    assert snap.run_state is RunState.WAITING_ON_USER
    assert snap.open_tools == frozenset({"t1", "t2"})
    assert snap.waiting_on_user_tools == frozenset({"t2"})


async def test_seed_open_tools_skips_route_with_live_state():
    """Live events have higher authority than a JSONL replay snapshot."""
    await route_runtime.ingest_transcript_event(
        ROUTE,
        _evt("assistant", "tool_use", tool_use_id="live-tool", tool_name="Bash"),
    )
    # Replay should NOT overwrite the live state.
    route_runtime.seed_open_tools(ROUTE, {"replay-tool": False})
    snap = route_runtime.snapshot(ROUTE)
    assert snap.open_tools == frozenset({"live-tool"})


# ── snapshot immutability + monotonic seq ───────────────────────────────


async def test_snapshots_are_frozen_dataclasses():
    import dataclasses

    snap = await route_runtime.mark_inbound_sent(ROUTE)
    # ``replace`` returns a copy — that's allowed.
    _ = replace(snap, run_state=RunState.IDLE_CLEARED)
    # Direct attribute mutation must raise on a frozen dataclass.
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.run_state = RunState.IDLE_CLEARED  # type: ignore[misc]


async def test_monotonic_seq_strictly_increases():
    seqs: list[int] = []
    snap = await route_runtime.mark_inbound_sent(ROUTE)
    seqs.append(snap.monotonic_seq)
    for tid in ("t1", "t2", "t3"):
        snap = await route_runtime.ingest_transcript_event(
            ROUTE,
            _evt(
                "assistant",
                "tool_use",
                tool_use_id=tid,
                tool_name="Bash",
            ),
        )
        seqs.append(snap.monotonic_seq)
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


async def test_snapshot_read_does_not_consume_seq_when_no_decay():
    await route_runtime.mark_inbound_sent(ROUTE)
    before = route_runtime.snapshot(ROUTE).monotonic_seq
    after = route_runtime.snapshot(ROUTE).monotonic_seq
    # Pure reads return the same global seq.
    assert before == after


# ── observers ───────────────────────────────────────────────────────────


async def test_subscribe_fires_after_commit():
    seen: list[RouteRuntimeSnapshot] = []

    async def observer(snap: RouteRuntimeSnapshot) -> None:
        seen.append(snap)

    unsubscribe = route_runtime.subscribe(ROUTE, observer)
    try:
        snap = await route_runtime.mark_inbound_sent(ROUTE)
    finally:
        unsubscribe()
    assert seen == [snap]
    assert seen[0].run_state is RunState.RUNNING


async def test_subscribe_does_not_block_subsequent_ingest_in_observer():
    """An observer that calls back into ``snapshot`` must not deadlock.

    Snapshot is a pure read — no lock acquisition — so calling it from
    inside an observer is safe.
    """
    events: list[str] = []

    async def observer(snap: RouteRuntimeSnapshot) -> None:
        # Reading inside observer: read state should be the committed one.
        inner = route_runtime.snapshot(ROUTE)
        events.append(f"obs:{snap.run_state.value}:{inner.run_state.value}")

    route_runtime.subscribe(ROUTE, observer)
    await route_runtime.mark_inbound_sent(ROUTE)
    assert events == ["obs:RUNNING:RUNNING"]


async def test_unsubscribe_stops_future_callbacks():
    seen: list[RouteRuntimeSnapshot] = []

    async def observer(snap: RouteRuntimeSnapshot) -> None:
        seen.append(snap)

    unsubscribe = route_runtime.subscribe(ROUTE, observer)
    await route_runtime.mark_inbound_sent(ROUTE)
    unsubscribe()
    await route_runtime.mark_inbound_sent(ROUTE)
    assert len(seen) == 1


async def test_observer_exception_does_not_break_fan_out():
    """A bad observer must not prevent later observers from running."""
    seen: list[str] = []

    async def broken(_snap: RouteRuntimeSnapshot) -> None:
        raise RuntimeError("boom")

    async def good(snap: RouteRuntimeSnapshot) -> None:
        seen.append(snap.run_state.value)

    route_runtime.subscribe(ROUTE, broken)
    route_runtime.subscribe(ROUTE, good)
    await route_runtime.mark_inbound_sent(ROUTE)
    assert seen == ["RUNNING"]


# ── per-route lock isolation ────────────────────────────────────────────


async def test_independent_routes_do_not_serialise():
    """Two routes can ingest concurrently without blocking each other.

    Verified by ensuring the second ingest's snapshot reflects a
    higher seq AND completes within roughly the same tick — if they
    serialised on a single global lock, the second would wait for the
    first's lock release.
    """
    barrier = asyncio.Event()
    proceed = asyncio.Event()
    seen_route1_state: list[str] = []

    async def observer_route1(_snap: RouteRuntimeSnapshot) -> None:
        # Block route 1's commit until route 2 has independently ingested.
        seen_route1_state.append("entered")
        await proceed.wait()

    route_runtime.subscribe(ROUTE, observer_route1)

    async def trigger_route1() -> None:
        await route_runtime.mark_inbound_sent(ROUTE)
        seen_route1_state.append("done")
        barrier.set()

    async def trigger_route2() -> None:
        await route_runtime.mark_inbound_sent(ROUTE_2)

    task1 = asyncio.create_task(trigger_route1())
    # Wait until route1's observer is blocked.
    while "entered" not in seen_route1_state:
        await asyncio.sleep(0)
    # Route2 should be able to commit even though route1's observer is
    # stuck — the lock for ROUTE_2 is independent of ROUTE's.
    await asyncio.wait_for(trigger_route2(), timeout=1.0)
    # Now release route1 and let it finish.
    proceed.set()
    await asyncio.wait_for(task1, timeout=1.0)


async def test_same_route_serialises():
    """Two ingest calls on the SAME route must serialise.

    The state machine never sees half-applied state — every transition
    is atomic relative to other transitions on that route.
    """
    observed: list[int] = []

    async def observer(snap: RouteRuntimeSnapshot) -> None:
        observed.append(snap.monotonic_seq)

    route_runtime.subscribe(ROUTE, observer)
    # Fire several mutations concurrently; the lock must order them.
    await asyncio.gather(
        route_runtime.mark_inbound_sent(ROUTE),
        route_runtime.ingest_transcript_event(
            ROUTE,
            _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash"),
        ),
        route_runtime.ingest_transcript_event(
            ROUTE,
            _evt("assistant", "tool_use", tool_use_id="t2", tool_name="Bash"),
        ),
    )
    # All three observed snapshots had unique, strictly-increasing seqs.
    assert observed == sorted(observed)
    assert len(set(observed)) == 3


# ── clear_route ─────────────────────────────────────────────────────────


async def test_clear_route_drops_all_state():
    await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "tool_use", tool_use_id="t1", tool_name="Bash")
    )
    route_runtime.update_context_usage(ROUTE, 50_000, "claude")
    route_runtime.mark_status_card_published(ROUTE, msg_id=42)

    route_runtime.clear_route(ROUTE)
    snap = route_runtime.snapshot(ROUTE)
    assert snap.run_state is RunState.IDLE_CLEARED
    assert snap.context_usage is None
    assert snap.status_card_msg_id is None
