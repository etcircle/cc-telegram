"""Scenario: a RESUMED background agent (SendMessage nudge) relights typing;
its cross-file sidechain done is timestamp-gated (Fix C, 2026-07-08).

Black-box at the public seams — no monkeypatch of run-state internals:

  - the bot's ``apply_sidechain_activity`` fan-out (the sink the monitor's
    SendMessage-resume branch feeds — a ``resumed`` map ``key -> resume_ts``,
    plus the sidechain ``ticks`` DONE-causality inputs), and
  - ``transcript_event_adapter.dispatch_transcript_event`` for the parent
    lifecycle,

with reads only from ``route_runtime.snapshot(route)``.

The multi-leg orchestration pattern (nudge a standing agent) ran fully dark
before Fix C: the agent's prior stop tombstoned its key and a machine wake
never resets tombstones, so the resume produced neither a launched key nor a
sidechain-activity relift. The resume is now the FOURTH launch source; its
resume ts (the parent tool_result event ts) lives on the runtime record so a
STALE prior-leg sidechain end_turn — this batch OR any later one — never kills
the relit key.
"""

from __future__ import annotations

from typing import Any

import pytest

from cctelegram import bot as bot_module
from cctelegram import route_runtime, transcript_event_adapter
from cctelegram.session_monitor import (
    ParentSidechainActivity,
    SidechainTick,
    TranscriptEvent,
)
from tests.conftest import ScenarioHarness

pytestmark = pytest.mark.scenario

_SID = "sess-resume"
_KEY = "a1951c4043e2c9561"


def _event(**kw: Any) -> TranscriptEvent:
    defaults: dict[str, Any] = dict(
        session_id=_SID,
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


async def _bind_idle_route(scenario: ScenarioHarness):
    wid = scenario.add_window(window_name="repo", cwd="/repo")
    scenario.bind_thread(
        thread_id=42, window_id=wid, display_name="repo", cwd="/repo", session_id=_SID
    )
    route = (scenario.user_id, 42, wid)
    # A genuine user turn then an end-of-turn → stored idle with a known stamp.
    await transcript_event_adapter.dispatch_transcript_event(
        _event(role="user", block_type="text", text="go"), [route]
    )
    await transcript_event_adapter.dispatch_transcript_event(
        _event(
            block_type="text",
            stop_reason="end_turn",
            text="done",
            timestamp="2026-07-08T10:00:00.000Z",
        ),
        [route],
    )
    snap = route_runtime.snapshot(route)
    assert snap.run_state in (
        route_runtime.RunState.IDLE_RECENT,
        route_runtime.RunState.IDLE_CLEARED,
    )
    return route


@pytest.mark.asyncio
async def test_full_multi_leg_launch_done_resume_relights_typing(
    scenario: ScenarioHarness,
) -> None:
    route = await _bind_idle_route(scenario)

    # Leg 1: launch → run → parent completion tombstones the key.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(launched={_KEY})}
    )
    assert route_runtime.snapshot(route).typing_eligible is True
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(completed={_KEY})}
    )
    assert route_runtime.snapshot(route).typing_eligible is False

    # A plain re-launch cannot relight the tombstone (proves resume-only).
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(launched={_KEY})}
    )
    assert route_runtime.snapshot(route).typing_eligible is False

    # Leg 2: RESUME (SendMessage nudge) — pops the tombstone, lifts typing.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(resumed={_KEY: 100.0})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.run_state is route_runtime.RunState.RUNNING
    assert snap.typing_eligible is True
    assert snap.background_agents == (_KEY,)

    # Leg 2 close: the resumed agent's next stop fires a <task-notification> →
    # PARENT done → unconditional tombstone → clean idle.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(completed={_KEY})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_cross_batch_stale_end_turn_after_resume_stays_live(
    scenario: ScenarioHarness,
) -> None:
    """Codex cross-batch: resume batch N (ts=100) → a stale prior-leg sidechain
    end_turn FIRST observed batch N+1 (ts=90 ≤ 100) ⇒ LIVE. The guard lives on
    the runtime record, so it outlives the batch."""
    route = await _bind_idle_route(scenario)
    # Batch N: resume ts=100.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(resumed={_KEY: 100.0})}
    )
    assert route_runtime.snapshot(route).typing_eligible is True
    # Batch N+1: a stale sidechain end_turn (ts=90) — NO resume this batch.
    await bot_module.apply_sidechain_activity(
        {
            _SID: ParentSidechainActivity(
                ticks={
                    _KEY: SidechainTick(
                        max_event_ts=90.0, saw_end_of_turn=True, max_end_turn_ts=90.0
                    )
                }
            )
        }
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True  # NOT killed by the stale done
    assert snap.background_agents == (_KEY,)


@pytest.mark.asyncio
async def test_same_batch_stale_end_turn_and_resume_stays_live(
    scenario: ScenarioHarness,
) -> None:
    """r2 dual-P1 through the SIDECHAIN lane, ONE batch: a stale prior-leg
    end_turn (ts=90) + the resume (ts=100) net LIVE (apply order resumed →
    activity → sidechain-done, timestamp-gated)."""
    route = await _bind_idle_route(scenario)
    await bot_module.apply_sidechain_activity(
        {
            _SID: ParentSidechainActivity(
                resumed={_KEY: 100.0},
                ticks={
                    _KEY: SidechainTick(
                        max_event_ts=90.0, saw_end_of_turn=True, max_end_turn_ts=90.0
                    )
                },
            )
        }
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True
    assert snap.background_agents == (_KEY,)


@pytest.mark.asyncio
async def test_same_batch_stale_end_turn_with_newer_activity_stays_live(
    scenario: ScenarioHarness,
) -> None:
    """Adversarial pin (Codex+Hermes r1 review fold): the fan-out must feed
    ``max_end_turn_ts`` (the DONE-causality field) to the sidechain done seam,
    NEVER ``max_event_ts`` (the activity max).

    THE POINT: every other test in this file uses
    ``max_event_ts == max_end_turn_ts``, so a future fan-out edit that
    accidentally passed ``tick.max_event_ts`` as ``end_turn_ts`` would slip
    through them all. Here the two fields DIVERGE: resume ts=100.0, activity
    ``max_event_ts=150.0`` (a NEWER post-resume non-end-turn write, > resume),
    end-turn ``max_end_turn_ts=90.0`` (a stale prior-leg end_turn, ≤ resume).
    The correct comparison (end_turn_ts=90 ≤ resume 100) keeps the key LIVE;
    the buggy comparison (max_event_ts=150 > resume 100) would tombstone it —
    so this test fails exactly when the fan-out feeds the wrong field."""
    route = await _bind_idle_route(scenario)
    await bot_module.apply_sidechain_activity(
        {
            _SID: ParentSidechainActivity(
                resumed={_KEY: 100.0},
                ticks={
                    _KEY: SidechainTick(
                        max_event_ts=150.0,
                        saw_end_of_turn=True,
                        max_end_turn_ts=90.0,
                    )
                },
            )
        }
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True  # NOT tombstoned by the activity max
    assert snap.background_agents == (_KEY,)


@pytest.mark.asyncio
async def test_same_batch_fast_finish_end_turn_tombstones(
    scenario: ScenarioHarness,
) -> None:
    """A GENUINE post-resume end_turn (ts=110 > resume 100) ⇒ TOMBSTONED."""
    route = await _bind_idle_route(scenario)
    await bot_module.apply_sidechain_activity(
        {
            _SID: ParentSidechainActivity(
                resumed={_KEY: 100.0},
                ticks={
                    _KEY: SidechainTick(
                        max_event_ts=110.0, saw_end_of_turn=True, max_end_turn_ts=110.0
                    )
                },
            )
        }
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_unparseable_sidechain_end_turn_fails_closed(
    scenario: ScenarioHarness,
) -> None:
    """An unparseable sidechain end_turn ts ⇒ fail-closed to DONE (TOMBSTONED)."""
    route = await _bind_idle_route(scenario)
    await bot_module.apply_sidechain_activity(
        {
            _SID: ParentSidechainActivity(
                resumed={_KEY: 100.0},
                ticks={
                    _KEY: SidechainTick(
                        saw_end_of_turn=True, end_turn_ts_unparseable=True
                    )
                },
            )
        }
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()
