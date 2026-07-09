"""Scenario: the full agent-teams teammate lifecycle at the busy-signal seam
(GH #46 PR-2) — spawn → work (typing ON) → park (typing OFF) → wake (typing back
ON) → a delayed STALE park (stays ON) → a final park (OFF).

Black-box at the public seams — no monkeypatch of run-state internals:

  - the bot's ``apply_sidechain_activity`` fan-out (the sink the monitor's
    teammate registry feeds — a launched key at bind, a ``resumed`` map at wake,
    a ``teammate_parks`` entry at park), and
  - ``transcript_event_adapter.dispatch_transcript_event`` for the parent
    lifecycle,

with reads only from ``route_runtime.snapshot(route)``.

PR-2 makes a teammate a FIRST-CLASS background key: its launched key survives
the parent's end-of-turn (typing stays on across parent turns), a wake relights
it, and a park is its ONLY close signal (a teammate leg ends in plain text — no
sidechain end-of-turn, no ``<task-notification>``). The TEAMMATE done shares the
SIDECHAIN resume ts-gate PLUS a TEAMMATE-only stale-vs-activity gate, so a
redelivered OLD park never darkens a demonstrably-working teammate mid-leg.
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

_SID = "sess-teammate"
# The teammate's normalized background key (== agent-<key> sidechain stem minus
# the ``agent-`` prefix; ``a<name>-<hex>``).
_KEY = "aexplore-backend-11223344aabbccdd"


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
    await transcript_event_adapter.dispatch_transcript_event(
        _event(role="user", block_type="text", text="spawn a teammate"), [route]
    )
    await transcript_event_adapter.dispatch_transcript_event(
        _event(
            block_type="text",
            stop_reason="end_turn",
            text="spawned",
            timestamp="2026-07-09T10:00:00.000Z",
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
async def test_teammate_full_lifecycle_typing(scenario: ScenarioHarness) -> None:
    route = await _bind_idle_route(scenario)

    # BIND — the teammate's launched key lifts the parent-idle route to typing
    # (survives the end-of-turn prune; a first-class background key).
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(launched={_KEY})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True
    assert snap.background_agents == (_KEY,)

    # WORK — a sidechain heartbeat keeps it live (parent stays idle).
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(ticks={_KEY: SidechainTick(max_event_ts=100.0)})}
    )
    assert route_runtime.snapshot(route).typing_eligible is True

    # PARK — the teammate reports idle; its park ts (200) is strictly NEWER than
    # the last activity (100), so it tombstones → typing drops promptly.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(teammate_parks={_KEY: (200.0, False)})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()

    # WAKE — a SendMessage nudge resumes the SAME key (resume ts 300 pops the
    # tombstone) → typing back ON.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(resumed={_KEY: 300.0})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True
    assert snap.background_agents == (_KEY,)

    # DELAYED STALE PARK — an OLD park (ts 250, < resume 300) is redelivered a
    # later batch. The resume ts-gate keeps the key LIVE (a redelivered old park
    # must never darken a demonstrably-resumed teammate).
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(teammate_parks={_KEY: (250.0, False)})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True  # NOT killed by the stale park
    assert snap.background_agents == (_KEY,)

    # FINAL PARK — the genuine final park (ts 400 > resume 300) tombstones →
    # typing drops for good.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(teammate_parks={_KEY: (400.0, False)})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_preregistration_retract_then_bind_relights_via_resumed(
    scenario: ScenarioHarness,
) -> None:
    """r2 P1 design-constraint pin: the registration retraction emits an
    UNCONDITIONAL teammate-done for a pre-registration live key, and the runtime
    tombstone then NO-OPS a later ``launched`` (done-before-launch fail-closes)
    — so the bind-after-retraction relight MUST ride the RESUMED lane (the Fix-C
    tombstone-popping path). Sequence: pre-registration legacy tick lifts typing
    → the retraction done drops it → a plain ``launched`` cannot relight
    (negative control) → the bind's ``resumed`` relights → a genuine later park
    closes."""
    from cctelegram.utils import parse_iso_timestamp

    route = await _bind_idle_route(scenario)
    eot = parse_iso_timestamp("2026-07-09T10:00:00.000Z")
    assert eot is not None

    # Pre-registration: the candidate ticks as a LEGACY agent (ts-qualified
    # past the parent's end-of-turn) → live key, typing ON.
    await bot_module.apply_sidechain_activity(
        {
            _SID: ParentSidechainActivity(
                ticks={_KEY: SidechainTick(max_event_ts=eot + 10)}
            )
        }
    )
    assert route_runtime.snapshot(route).typing_eligible is True

    # Registration retraction: the unconditional teammate-done → typing OFF.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(teammate_parks={_KEY: (None, True)})}
    )
    assert route_runtime.snapshot(route).typing_eligible is False

    # Negative control: a plain launched CANNOT relight the tombstoned key —
    # exactly why the bind must use the resumed lane.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(launched={_KEY})}
    )
    assert route_runtime.snapshot(route).typing_eligible is False

    # The bind relight (resumed lane, ts = the generation's spawned_ts): pops
    # the tombstone → typing back ON.
    spawn_ts = eot + 20
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(resumed={_KEY: spawn_ts})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True
    assert snap.background_agents == (_KEY,)

    # A genuine current-generation park (strictly newer than the resume ts)
    # closes it — the strict-newer gate passes.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(teammate_parks={_KEY: (spawn_ts + 60, False)})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()
