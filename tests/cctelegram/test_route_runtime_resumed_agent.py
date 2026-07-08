"""Fix C (2026-07-08) route_runtime tests — resumed-agent relight.

``SendMessage`` to an already-existing background agent (the multi-leg
"nudge" pattern) is the FOURTH launch source. It pops the per-key done
tombstone (the SECOND, KEYED exception to "tombstones reset only on a genuine
user turn"), records the key ``is_background=True``, and stores a
``resumed_event_ts`` (the parent SendMessage tool_result's EVENT timestamp) on
the live record so the CROSS-FILE resume-vs-sidechain-done race is resolved by
the RUNTIME RECORD (outliving any single poll batch — Codex r3 cross-batch
hole).

Pins the r4 binding must-haves:
  (1) activity refresh PRESERVES ``resumed_event_ts``;
  (2) parent ``<task-notification>`` done stays UNCONDITIONAL;
  (3) sidechain done on a MISSING record == no-resumed_event_ts (tombstone,
      fail-closed);
  (4) the resume ts is the SendMessage tool_result event ts;
  (5) TTL-edge: resume → record TTL-expires → stale sidechain done tombstones →
      a LATER resume relights;
  (6) ``.resumed`` carries ``key -> resume_ts``; an unparseable later resume ts
      never erases an older parseable ``resumed_event_ts``.
"""

from __future__ import annotations

import pytest

from cctelegram import route_runtime
from cctelegram.route_runtime import (
    BG_BACKGROUND_TTL_SECONDS,
    BgDoneSource,
    RunState,
    TranscriptLifecycleEvent,
)

ROUTE: route_runtime.Route = (1, 42, "@7")
KEY = "a1951c4043e2c9561"
KEY2 = "b9f8e7d6c5b4a3210"

T0 = 1_750_000_000.0


def _evt(
    role: str = "assistant",
    block: str = "text",
    *,
    tool_use_id: str | None = None,
    tool_name: str | None = None,
    stop_reason: str | None = None,
    timestamp: float | None = None,
    is_task_notification: bool = False,
) -> TranscriptLifecycleEvent:
    return TranscriptLifecycleEvent(
        role=role,  # type: ignore[arg-type]
        block_type=block,  # type: ignore[arg-type]
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        stop_reason=stop_reason,
        timestamp=timestamp,
        is_task_notification=is_task_notification,
    )


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    route_runtime.reset_for_tests()
    monkeypatch.setattr(route_runtime, "_wall_now", lambda: T0)
    yield
    route_runtime.reset_for_tests()


def _st(route=ROUTE) -> route_runtime._RouteState:
    return route_runtime._state[route]


async def _idle_transcript(route=ROUTE, *, end_ts: float = 100.0) -> None:
    await route_runtime.ingest_transcript_event(
        route, _evt("assistant", "text", stop_reason="end_turn", timestamp=end_ts)
    )
    assert _st(route).idle_source == "transcript"


# ── the failing-case-today multi-leg cycle ───────────────────────────────


async def test_full_multi_leg_cycle_launch_done_resume_relight_done():
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    # Leg 1: launch → work → parent done (tombstone).
    await route_runtime.mark_background_agent_launched(ROUTE, KEY)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 90.0)
    await route_runtime.mark_background_agent_done(ROUTE, KEY)  # parent default
    assert KEY in _st().background_agents_done
    await _idle_transcript(end_ts=100.0)
    # A plain LAUNCH cannot relight the tombstone (proves the exception is
    # resume-only).
    snap = await route_runtime.mark_background_agent_launched(ROUTE, KEY)
    assert snap.background_agents == ()
    # Leg 2: RESUME → key live again + projected RUNNING + typing (the case
    # that ran fully dark before Fix C).
    snap = await route_runtime.mark_background_agent_resumed(ROUTE, KEY, 150.0)
    assert KEY not in _st().background_agents_done  # tombstone popped
    assert snap.run_state is RunState.RUNNING
    assert snap.typing_eligible is True
    assert snap.background_agents == (KEY,)
    assert _st().background_agents[KEY].is_background is True
    assert _st().background_agents[KEY].resumed_event_ts == 150.0
    # Leg 2 close: the resumed agent's next stop emits a <task-notification> →
    # the existing parent done path re-tombstones (UNCONDITIONAL even with a
    # resumed_event_ts — must-have 2).
    snap = await route_runtime.mark_background_agent_done(
        ROUTE, KEY, source=BgDoneSource.PARENT
    )
    assert KEY in _st().background_agents_done
    assert snap.background_agents == ()
    assert snap.run_state in (RunState.IDLE_RECENT, RunState.IDLE_CLEARED)


async def test_resume_of_never_launched_key_records():
    await _idle_transcript(end_ts=100.0)
    snap = await route_runtime.mark_background_agent_resumed(ROUTE, KEY, 150.0)
    assert snap.background_agents == (KEY,)
    assert _st().background_agents[KEY].is_background is True


async def test_plain_launch_still_cannot_relight_a_tombstone():
    """The exception is resume-ONLY: a plain launched key stays tombstoned."""
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    await route_runtime.mark_background_agent_done(ROUTE, KEY)
    snap = await route_runtime.mark_background_agent_launched(ROUTE, KEY)
    assert snap.background_agents == ()
    assert KEY in _st().background_agents_done


async def test_resume_key_normalization_applied():
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_resumed(ROUTE, f"agent-{KEY}", 150.0)
    assert KEY in _st().background_agents


# ── seed-idle twin (unseen post-restart parent) ──────────────────────────


async def test_seed_idle_resume_on_unseen_route_seeds_and_lifts():
    assert ROUTE not in route_runtime._state
    snap = await route_runtime.seed_idle_and_mark_background_agent_resumed(
        ROUTE, KEY, 150.0
    )
    assert ROUTE in route_runtime._state
    assert snap.run_state is RunState.RUNNING
    assert snap.typing_eligible is True
    assert _st().background_agents[KEY].is_background is True
    assert _st().background_agents[KEY].resumed_event_ts == 150.0


async def test_plain_resume_never_seeds_an_unseen_route():
    snap = await route_runtime.mark_background_agent_resumed(ROUTE, KEY, 150.0)
    assert ROUTE not in route_runtime._state
    assert snap.run_state is RunState.IDLE_CLEARED


# ── must-have 1: activity preserves resumed_event_ts ─────────────────────


async def test_activity_refresh_preserves_resumed_event_ts():
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_resumed(ROUTE, KEY, 150.0)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 160.0)
    assert _st().background_agents[KEY].resumed_event_ts == 150.0  # preserved
    assert _st().background_agents[KEY].last_event_ts == 160.0


# ── must-have 6: max-monotonic, unparseable never erases parseable ───────


async def test_resume_ts_is_max_monotonic_across_resumes():
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_resumed(ROUTE, KEY, 150.0)
    await route_runtime.mark_background_agent_resumed(ROUTE, KEY, 140.0)  # older
    assert _st().background_agents[KEY].resumed_event_ts == 150.0
    await route_runtime.mark_background_agent_resumed(ROUTE, KEY, 200.0)  # newer
    assert _st().background_agents[KEY].resumed_event_ts == 200.0


async def test_unparseable_later_resume_ts_never_erases_older_parseable():
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_resumed(ROUTE, KEY, 150.0)
    await route_runtime.mark_background_agent_resumed(ROUTE, KEY, None)  # unparseable
    assert _st().background_agents[KEY].resumed_event_ts == 150.0  # not erased


# ── SIDECHAIN done: runtime-record timestamp authority ───────────────────


async def test_sidechain_done_stale_prior_leg_keeps_live():
    """Resume ts=150, stale prior-leg sidechain end_turn ts=90 (<= resume) ⇒
    LIVE (the Codex cross-batch hole: same rule ANY batch)."""
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_resumed(ROUTE, KEY, 150.0)
    snap = await route_runtime.mark_background_agent_done(
        ROUTE, KEY, source=BgDoneSource.SIDECHAIN, end_turn_ts=90.0
    )
    assert KEY not in _st().background_agents_done  # NOT tombstoned
    assert snap.background_agents == (KEY,)
    assert snap.run_state is RunState.RUNNING


async def test_sidechain_done_equal_ts_keeps_live():
    """Equal ts is not causal proof of a post-resume completion (the
    strict-newer discipline of _maybe_clear_notification_by_ts)."""
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_resumed(ROUTE, KEY, 150.0)
    await route_runtime.mark_background_agent_done(
        ROUTE, KEY, source=BgDoneSource.SIDECHAIN, end_turn_ts=150.0
    )
    assert KEY not in _st().background_agents_done


async def test_sidechain_done_genuine_fast_finish_tombstones():
    """Resume ts=150, genuine post-resume sidechain end_turn ts=200 (> resume)
    ⇒ TOMBSTONED (agent finished fast after the nudge)."""
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_resumed(ROUTE, KEY, 150.0)
    snap = await route_runtime.mark_background_agent_done(
        ROUTE, KEY, source=BgDoneSource.SIDECHAIN, end_turn_ts=200.0
    )
    assert KEY in _st().background_agents_done
    assert snap.background_agents == ()


async def test_sidechain_done_unparseable_end_turn_ts_fails_closed():
    """Any end_turn timestamp missing/unparseable ⇒ fail-closed to DONE."""
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_resumed(ROUTE, KEY, 150.0)
    await route_runtime.mark_background_agent_done(
        ROUTE,
        KEY,
        source=BgDoneSource.SIDECHAIN,
        end_turn_ts=None,
        end_turn_ts_unparseable=True,
    )
    assert KEY in _st().background_agents_done


async def test_sidechain_done_unparseable_flag_wins_even_with_parseable_max():
    """A mixed tick (a parseable <= resume AND an unparseable end_turn) ⇒
    fail-closed rule wins (must-have 6: any unparseable observed end-turn)."""
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_resumed(ROUTE, KEY, 150.0)
    await route_runtime.mark_background_agent_done(
        ROUTE,
        KEY,
        source=BgDoneSource.SIDECHAIN,
        end_turn_ts=90.0,
        end_turn_ts_unparseable=True,
    )
    assert KEY in _st().background_agents_done


async def test_sidechain_done_no_resumed_ts_is_byte_identical_today():
    """A plain-launch key (no resumed_event_ts) sidechain done ⇒ TOMBSTONED,
    exactly like today (a fresh launch is a fresh agent id; no stale end_turn
    can exist)."""
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_launched(ROUTE, KEY)
    await route_runtime.mark_background_agent_activity(ROUTE, KEY, 150.0)
    assert _st().background_agents[KEY].resumed_event_ts is None
    await route_runtime.mark_background_agent_done(
        ROUTE, KEY, source=BgDoneSource.SIDECHAIN, end_turn_ts=90.0
    )
    assert KEY in _st().background_agents_done  # fail-closed / today's behavior


async def test_sidechain_done_missing_record_tombstones_fail_closed():
    """must-have 3: a MISSING record is treated exactly like no-resumed_event_ts
    (tombstone, fail-closed)."""
    await _idle_transcript(end_ts=100.0)
    assert KEY not in _st().background_agents
    await route_runtime.mark_background_agent_done(
        ROUTE, KEY, source=BgDoneSource.SIDECHAIN, end_turn_ts=90.0
    )
    assert KEY in _st().background_agents_done


# ── must-have 2: parent done UNCONDITIONAL ───────────────────────────────


async def test_parent_done_tombstones_even_with_resumed_event_ts():
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_resumed(ROUTE, KEY, 150.0)
    snap = await route_runtime.mark_background_agent_done(
        ROUTE, KEY, source=BgDoneSource.PARENT, end_turn_ts=90.0
    )
    # Parent transcript order is authoritative — unconditional tombstone even
    # though the sidechain rule would have kept it live at end_turn_ts=90.
    assert KEY in _st().background_agents_done
    assert snap.background_agents == ()


async def test_parent_done_default_source_is_parent_unconditional():
    """The existing positional call (no source) defaults to PARENT and stays
    byte-identical: unconditional tombstone."""
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_resumed(ROUTE, KEY, 150.0)
    await route_runtime.mark_background_agent_done(ROUTE, KEY)  # default PARENT
    assert KEY in _st().background_agents_done


# ── must-have 5: the TTL edge ────────────────────────────────────────────


async def test_ttl_edge_resume_expires_stale_done_tombstones_then_resume_relights(
    monkeypatch: pytest.MonkeyPatch,
):
    await _idle_transcript(end_ts=100.0)
    # Resume records the key with resumed_event_ts=150 (is_background → 2h TTL).
    await route_runtime.mark_background_agent_resumed(ROUTE, KEY, 150.0)
    assert route_runtime.snapshot(ROUTE).run_state is RunState.RUNNING
    # The record TTL-expires (no heartbeat for > BG_BACKGROUND_TTL).
    monkeypatch.setattr(
        route_runtime, "_wall_now", lambda: T0 + BG_BACKGROUND_TTL_SECONDS + 1
    )
    # A stale prior-leg sidechain done now lands: the record is GONE (expired),
    # so it is treated as no-resumed_event_ts → TOMBSTONE (accepted: the runtime
    # already judged the agent too silent).
    await route_runtime.mark_background_agent_done(
        ROUTE, KEY, source=BgDoneSource.SIDECHAIN, end_turn_ts=140.0
    )
    assert KEY in _st().background_agents_done
    # A LATER resume pops the tombstone and relights — expiry never permanently
    # poisons future legs.
    snap = await route_runtime.mark_background_agent_resumed(
        ROUTE, KEY, T0 + BG_BACKGROUND_TTL_SECONDS + 5
    )
    assert KEY not in _st().background_agents_done
    assert snap.run_state is RunState.RUNNING
    assert snap.background_agents == (KEY,)


# ── genuine user turn still resets tombstones (untouched) ─────────────────


async def test_genuine_user_turn_still_resets_all_tombstones():
    await _idle_transcript(end_ts=100.0)
    await route_runtime.mark_background_agent_resumed(ROUTE, KEY, 150.0)
    await route_runtime.mark_background_agent_done(
        ROUTE, KEY, source=BgDoneSource.PARENT
    )
    assert KEY in _st().background_agents_done
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    assert _st().background_agents_done == set()


# ── resumed key survives the end-of-turn prune (is_background) ────────────


async def test_resumed_key_survives_end_of_turn_prune():
    await route_runtime.ingest_transcript_event(ROUTE, _evt("user", "text"))
    await route_runtime.mark_background_agent_resumed(ROUTE, KEY, 5.0)
    snap = await route_runtime.ingest_transcript_event(
        ROUTE, _evt("assistant", "text", stop_reason="end_turn", timestamp=100.0)
    )
    assert snap.background_agents == (KEY,)  # is_background=True survives
    assert snap.run_state is RunState.RUNNING
