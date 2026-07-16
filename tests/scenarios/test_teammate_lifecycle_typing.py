"""Scenario: the full agent-teams teammate lifecycle at the busy-signal seam
(GH #46 PR-2) — spawn → work (typing ON) → park (typing OFF) → wake (typing back
ON) → a delayed STALE park (stays ON) → a final park (OFF).

Black-box at the public seams — no monkeypatch of run-state internals:

  - the bot's ``apply_sidechain_activity`` fan-out (the sink the monitor's
    teammate registry feeds — a ``resumed`` map at bind AND at wake (r7 item 3:
    EVERY teammate bind relights via the tombstone-popping resumed lane, never
    ``launched``), a ``teammate_parks`` entry at park), and
  - ``transcript_event_adapter.dispatch_transcript_event`` for the parent
    lifecycle,

with reads only from ``route_runtime.snapshot(route)``.

PR-2 makes a teammate a FIRST-CLASS background key: its relight (via the
always-resumed lane, never ``launched``) survives the parent's end-of-turn
(typing stays on across parent turns), a wake relights it, and a park is its
ONLY close signal (a teammate leg ends in plain text — no
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
    TEAMMATE_RETRACT_RESUME_EPSILON_S,
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

    # BIND — the teammate's relight (via the always-resumed lane, r7 item 3,
    # never ``launched``: a fresh key with no tombstone treats resumed as a plain
    # launch — is_background, 2h TTL, projected RUNNING) lifts the parent-idle
    # route to typing (survives the end-of-turn prune; a first-class background
    # key). The bind resume ts (50) sits below every later park (200/250/400).
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(resumed={_KEY: 50.0})}
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
    """r2 P1 design-constraint pin: the registration retraction (the DISTINCT
    ``retraction_dones`` slot — r3 P1) emits an UNCONDITIONAL done for a
    pre-registration live key, and the runtime tombstone then NO-OPS a later
    ``launched`` (done-before-launch fail-closes) — so the bind-after-retraction
    relight MUST ride the RESUMED lane (the Fix-C tombstone-popping path).
    Sequence: pre-registration legacy tick lifts typing → the retraction done
    drops it → a plain ``launched`` cannot relight (negative control) → the
    bind's ``resumed`` relights → a genuine later park closes."""
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

    # Registration retraction (the distinct slot) → typing OFF.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(retraction_dones={_KEY})}
    )
    assert route_runtime.snapshot(route).typing_eligible is False

    # Negative control: a plain launched CANNOT relight the tombstoned key —
    # exactly why the bind must use the resumed lane.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(launched={_KEY})}
    )
    assert route_runtime.snapshot(route).typing_eligible is False

    # The bind relight (resumed lane, ts strictly below the generation's
    # spawned_ts — r3 item 2): pops the tombstone → typing back ON.
    spawn_ts = eot + 20
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(resumed={_KEY: spawn_ts - 0.001})}
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


@pytest.mark.asyncio
async def test_same_record_retraction_and_bind_resume_nets_live(
    scenario: ScenarioHarness,
) -> None:
    """r3 P1 REQUIRED pin (both engines): ONE aggregated ParentSidechainActivity
    record carrying BOTH the same-tick retraction AND the bind's resumed relight
    — through the PRODUCTION fan-out. The causal apply order (retraction-dones
    FIRST, resumed after: registration precedes bind) nets the key LIVE even
    when the monitor-side cancel was missed — the pre-fix shape applied resumed
    first and the synthetic park after, permanently tombstoning the just-bound
    key (probe-reproduced by both engines)."""
    from cctelegram.utils import parse_iso_timestamp

    route = await _bind_idle_route(scenario)
    eot = parse_iso_timestamp("2026-07-09T10:00:00.000Z")
    assert eot is not None

    # Pre-registration legacy tick → live key.
    await bot_module.apply_sidechain_activity(
        {
            _SID: ParentSidechainActivity(
                ticks={_KEY: SidechainTick(max_event_ts=eot + 10)}
            )
        }
    )
    assert route_runtime.snapshot(route).typing_eligible is True

    # ONE record: the retraction AND the bind-resume together.
    spawn_ts = eot + 20
    await bot_module.apply_sidechain_activity(
        {
            _SID: ParentSidechainActivity(
                retraction_dones={_KEY},
                resumed={_KEY: spawn_ts - 0.001},
            )
        }
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True  # LIVE — never the permanent dark
    assert snap.background_agents == (_KEY,)

    # Genuine later activity keeps flowing (the pre-fix bug blocked it).
    await bot_module.apply_sidechain_activity(
        {
            _SID: ParentSidechainActivity(
                ticks={_KEY: SidechainTick(max_event_ts=spawn_ts + 5)}
            )
        }
    )
    assert route_runtime.snapshot(route).typing_eligible is True

    # And the genuine park still closes.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(teammate_parks={_KEY: (spawn_ts + 60, False)})}
    )
    assert route_runtime.snapshot(route).typing_eligible is False


@pytest.mark.asyncio
async def test_genuine_unparseable_park_with_same_record_resume_still_tombstones(
    scenario: ScenarioHarness,
) -> None:
    """r3 P1 dominance preservation: the cancel/causal-order fix is scoped to the
    SYNTHETIC retraction slot ONLY — a GENUINE unparseable park (from a real
    idle_notification envelope) arriving in the same record as a resume STILL
    tombstones (parks apply after resumed; unparseable dominance untouched)."""
    from cctelegram.utils import parse_iso_timestamp

    route = await _bind_idle_route(scenario)
    eot = parse_iso_timestamp("2026-07-09T10:00:00.000Z")
    assert eot is not None

    await bot_module.apply_sidechain_activity(
        {
            _SID: ParentSidechainActivity(
                resumed={_KEY: eot + 20},
                teammate_parks={_KEY: (None, True)},  # genuine unparseable park
            )
        }
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False  # dominance holds — fail-dark
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_park_at_exactly_spawned_ts_closes_relit_key(
    scenario: ScenarioHarness,
) -> None:
    """r3 item 2 REQUIRED boundary pin (Codex P1, probe-confirmed): a genuine
    park stamped at EXACTLY the generation's spawned_ts passes the generation
    filter (only park_ts < spawned_ts drops) — and with the relight resume ts at
    ``spawned_ts - epsilon`` it is STRICTLY newer than the resume, so it CLOSES
    (pre-fix, resume ts == spawned_ts made the runtime resume gate suppress the
    tie and strand typing to the 2h TTL)."""
    from cctelegram.utils import parse_iso_timestamp

    route = await _bind_idle_route(scenario)
    eot = parse_iso_timestamp("2026-07-09T10:00:00.000Z")
    assert eot is not None
    spawn_ts = eot + 20

    # The retraction→bind relight, as the monitor emits it (resume strictly
    # below the spawn instant).
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(retraction_dones={_KEY})}
    )
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(resumed={_KEY: spawn_ts - 0.001})}
    )
    assert route_runtime.snapshot(route).typing_eligible is True

    # The tie boundary: a genuine park at EXACTLY spawned_ts → typing drops.
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(teammate_parks={_KEY: (spawn_ts, False)})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()


def _make_hybrid_monitor(tmp_path):
    """A REAL SessionMonitor over tmp_path whose parent is _SID, for the r4
    hybrid pins (real check_for_updates → production apply_sidechain_activity)."""
    import json as _json

    from cctelegram.session_monitor import (
        SessionInfo,
        SessionMonitor,
        TrackedSession,
    )

    mon = SessionMonitor(
        projects_path=tmp_path / "projects",
        state_file=tmp_path / "monitor_state.json",
    )
    proj = tmp_path / "projects" / "-p"
    proj.mkdir(parents=True, exist_ok=True)
    parent_jsonl = proj / f"{_SID}.jsonl"
    parent_jsonl.write_text("")
    sub_dir = proj / _SID / "subagents"
    sub_dir.mkdir(parents=True, exist_ok=True)
    mon.state.update_session(
        TrackedSession(
            session_id=_SID,
            file_path=str(parent_jsonl),
            last_byte_offset=0,
        )
    )

    async def _scan():
        return [SessionInfo(session_id=_SID, file_path=parent_jsonl)]

    mon.scan_projects = _scan  # type: ignore[method-assign]

    def _append(entries):
        with open(parent_jsonl, "a") as f:
            for e in entries:
                f.write(_json.dumps(e) + "\n")

    return mon, sub_dir, _append


def _iso_utc(ts: float) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _spawn_pair(name: str, ts: str, tool_id: str = "tu_r4") -> list[dict]:
    return [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": "Agent",
                        "input": {"prompt": "go"},
                    }
                ]
            },
            "sessionId": _SID,
            "timestamp": ts,
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": [{"type": "text", "text": "Spawned successfully."}],
                    }
                ]
            },
            "sessionId": _SID,
            "timestamp": ts,
            "toolUseResult": {
                "status": "teammate_spawned",
                "name": name,
                "teammate_id": f"{name}@team",
                "agent_type": "explorer",
            },
        },
    ]


def _park_entry(name: str, payload_ts: str | None, entry_ts: str) -> dict:
    payload = f'{{"type":"idle_notification","from":"{name}"'
    if payload_ts is not None:
        payload += f',"timestamp":"{payload_ts}"'
    payload += "}"
    return {
        "type": "user",
        "message": {
            "content": (
                "Another Claude session sent a message:\n"
                f'<teammate-message teammate_id="{name}" color="blue">\n'
                f"{payload}\n"
                "</teammate-message>\n"
            )
        },
        "sessionId": _SID,
        "timestamp": entry_ts,
    }


@pytest.mark.asyncio
async def test_no_registry_stale_park_then_spawn_one_batch_binds_live(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """r4 P2 REQUIRED pin (Codex, probe-reproduced): stem pre-discovered at EOF
    (no registry rec) → ONE parent batch carries a delayed park at T1 followed by
    the fresh spawn at T2 → the park lands via the no-registry fallback (no
    spawned_ts existed to filter against), the spawn registers + binds — and the
    bind's RETROACTIVE generation filter drops the stale parseable park, so
    through real check_for_updates + the PRODUCTION fan-out the key is bound AND
    typing lifts (pre-fix: launched then the stale park → tombstoned dark). A
    genuine park at ts > spawned_ts still closes it."""
    import json as _json
    import os
    import time

    route = await _bind_idle_route(scenario)
    mon, sub_dir, _append = _make_hybrid_monitor(tmp_path)
    name = "explore-backend"
    key = _KEY  # aexplore-backend-11223344aabbccdd

    # Pre-discover the stem at EOF (tracked, NO registry rec yet).
    t2 = time.time() - 30
    sc = sub_dir / f"agent-{key}.jsonl"
    sc.write_text(
        _json.dumps(
            {
                "type": "assistant",
                "message": {"content": []},
                "timestamp": _iso_utc(t2 - 1),
            }
        )
        + "\n"
    )
    os.utime(sc, (t2 - 1, t2 - 1))
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())

    # ONE batch: the delayed park (T1 < T2) THEN the fresh spawn (T2).
    _append([_park_entry(name, _iso_utc(t2 - 2), _iso_utc(t2 - 2))])
    _append(_spawn_pair(name, _iso_utc(t2)))
    await mon.check_for_updates({_SID})
    await mon.check_sidechain_updates({_SID})
    act = mon.pop_sidechain_activity()
    await bot_module.apply_sidechain_activity(act)

    # Bound + LIVE: the stale park was retroactively generation-filtered.
    assert mon._teammate_registry[_SID][name].current_key == key
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True
    assert snap.background_agents == (key,)

    # A GENUINE park (ts > spawned_ts) still closes it.
    _append([_park_entry(name, _iso_utc(t2 + 60), _iso_utc(t2 + 60))])
    await mon.check_for_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_no_registry_unparseable_park_then_spawn_still_tombstones(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """r4 dominance variant: an UNPARSEABLE pre-registration park (no timestamp —
    it cannot be generation-checked) is KEPT by the retroactive filter and still
    tombstones the bound key through the production fan-out (fail-dark
    preserved; the retroactive drop is scoped strictly to parseable-and-stale)."""
    import json as _json
    import os
    import time

    route = await _bind_idle_route(scenario)
    mon, sub_dir, _append = _make_hybrid_monitor(tmp_path)
    name = "explore-backend"
    key = _KEY

    t2 = time.time() - 30
    sc = sub_dir / f"agent-{key}.jsonl"
    sc.write_text(
        _json.dumps(
            {
                "type": "assistant",
                "message": {"content": []},
                "timestamp": _iso_utc(t2 - 1),
            }
        )
        + "\n"
    )
    os.utime(sc, (t2 - 1, t2 - 1))
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())

    # ONE batch: an UNPARSEABLE park (no ts) THEN the spawn.
    _append([_park_entry(name, None, _iso_utc(t2 - 2))])
    _append(_spawn_pair(name, _iso_utc(t2)))
    await mon.check_for_updates({_SID})
    await mon.check_sidechain_updates({_SID})
    act = mon.pop_sidechain_activity()
    assert act[_SID].teammate_parks.get(key) == (None, True)  # kept (dominance)
    await bot_module.apply_sidechain_activity(act)

    assert mon._teammate_registry[_SID][name].current_key == key  # still binds
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False  # fail-dark holds
    assert snap.background_agents == ()


def _spawn_pair_result_first(name: str, ts: str, tool_id: str = "tu_r4") -> list[dict]:
    """The GH #42 result-before-use ordering: the spawn tool_result flushes
    BEFORE its Agent tool_use (the r1 retro-pairing lane)."""
    pair = _spawn_pair(name, ts, tool_id)
    return [pair[1], pair[0]]


@pytest.mark.asyncio
async def test_orphan_park_between_stashed_spawn_and_late_use_closes_bind(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """r5 P1 REQUIRED pin (Codex, probe-reproduced): the exact repro ordering —
    spawn tool_result (stashed by the r1 retro-pairing) → GENUINE park → late
    Agent tool_use (registers) → sidechain discovery/bind. At park-record time
    there is NO registry rec AND no tracked stem, so pre-fix the park was
    dropped on the floor and the bind ran live to the 2h TTL (teammates have no
    other close signal). The orphan-park buffer retains it by name; the
    registration drain routes it through pending_park; the bind applies it —
    the key must NOT stay live (typing drops through the production fan-out)."""
    import json as _json
    import os
    import time

    route = await _bind_idle_route(scenario)
    mon, sub_dir, _append = _make_hybrid_monitor(tmp_path)
    name = "explore-backend"
    key = _KEY

    # The sidechain file exists ON DISK but is NOT tracked (never swept).
    t2 = time.time() - 30
    sc = sub_dir / f"agent-{key}.jsonl"
    sc.write_text(
        _json.dumps(
            {
                "type": "assistant",
                "message": {"content": []},
                "timestamp": _iso_utc(t2 + 0.05),
            }
        )
        + "\n"
    )
    os.utime(sc, (t2 + 0.05, t2 + 0.05))

    # ONE batch, the repro order: spawn RESULT (stashed) → genuine park (T3 ≥
    # spawn) → the late Agent tool_use (retro-pairs → registers → drains).
    pair = _spawn_pair_result_first(name, _iso_utc(t2))
    t3 = t2 + 5
    _append([pair[0]])
    _append([_park_entry(name, _iso_utc(t3), _iso_utc(t3))])
    _append([pair[1]])
    await mon.check_for_updates({_SID})
    await mon.check_sidechain_updates({_SID})
    act = mon.pop_sidechain_activity()
    # The drained park rode pending_park into the bind's record (the ISO
    # round-trip truncates to milliseconds - compare with tolerance).
    park_ts, unparseable = act[_SID].teammate_parks[key]
    assert unparseable is False and park_ts == pytest.approx(t3, abs=0.01)
    await bot_module.apply_sidechain_activity(act)

    # Bound — but NOT live: the retained park closed it.
    assert mon._teammate_registry[_SID][name].current_key == key
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_orphan_unparseable_park_through_same_ordering_still_tombstones(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """r5 dominance variant: an UNPARSEABLE park through the same orphan ordering
    keeps its unconditional dominance — retained as UnknownDone, drained (never
    generation-checked), applied at bind, tombstones the key (fail-dark)."""
    import json as _json
    import os
    import time

    route = await _bind_idle_route(scenario)
    mon, sub_dir, _append = _make_hybrid_monitor(tmp_path)
    name = "explore-backend"
    key = _KEY

    t2 = time.time() - 30
    sc = sub_dir / f"agent-{key}.jsonl"
    sc.write_text(
        _json.dumps(
            {
                "type": "assistant",
                "message": {"content": []},
                "timestamp": _iso_utc(t2 + 0.05),
            }
        )
        + "\n"
    )
    os.utime(sc, (t2 + 0.05, t2 + 0.05))

    pair = _spawn_pair_result_first(name, _iso_utc(t2))
    _append([pair[0]])
    _append([_park_entry(name, None, _iso_utc(t2 + 5))])  # unparseable (no ts)
    _append([pair[1]])
    await mon.check_for_updates({_SID})
    await mon.check_sidechain_updates({_SID})
    act = mon.pop_sidechain_activity()
    assert act[_SID].teammate_parks.get(key) == (None, True)  # dominance held
    await bot_module.apply_sidechain_activity(act)

    assert mon._teammate_registry[_SID][name].current_key == key
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_stale_orphan_park_generation_dropped_at_drain_bind_stays_live(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """r5 non-regression (the r4 case through the NEW buffer): a STALE orphan
    park (park_ts < the eventual generation's spawned_ts — a prior-leg park
    retained before any registration) is generation-DROPPED at the drain, so the
    fresh bind stays LIVE; a genuine later park still closes."""
    import json as _json
    import os
    import time

    route = await _bind_idle_route(scenario)
    mon, sub_dir, _append = _make_hybrid_monitor(tmp_path)
    name = "explore-backend"
    key = _KEY

    t2 = time.time() - 30
    # The STALE park (T1 < T2) arrives first — orphan-retained (no rec, no stem).
    _append([_park_entry(name, _iso_utc(t2 - 10), _iso_utc(t2 - 10))])
    await mon.check_for_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    assert name in mon._orphan_teammate_parks.get(_SID, {})

    # The spawn (T2) registers → drain drops the stale park (T1 < spawned_ts).
    _append(_spawn_pair(name, _iso_utc(t2)))
    await mon.check_for_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    rec = mon._teammate_registry[_SID][name]
    assert rec.pending_park is None  # dropped at drain, never buffered

    # The genuine file binds → LIVE (typing lifts).
    sc = sub_dir / f"agent-{key}.jsonl"
    sc.write_text(
        _json.dumps(
            {
                "type": "assistant",
                "message": {"content": []},
                "timestamp": _iso_utc(t2 + 0.05),
            }
        )
        + "\n"
    )
    os.utime(sc, (t2 + 0.05, t2 + 0.05))
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    assert mon._teammate_registry[_SID][name].current_key == key
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True  # the r4 case does NOT regress
    assert snap.background_agents == (key,)

    # A genuine park (> spawned_ts) still closes it.
    _append([_park_entry(name, _iso_utc(t2 + 60), _iso_utc(t2 + 60))])
    await mon.check_for_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    assert route_runtime.snapshot(route).typing_eligible is False


def _write_teammate_sidechain(sub_dir, key: str, first_ts: float, mtime: float):
    import json as _json
    import os

    sc = sub_dir / f"agent-{key}.jsonl"
    sc.write_text(
        _json.dumps(
            {
                "type": "assistant",
                "message": {"content": []},
                "timestamp": _iso_utc(first_ts),
            }
        )
        + "\n"
    )
    os.utime(sc, (mtime, mtime))
    return sc


@pytest.mark.asyncio
async def test_park_absorbed_by_tracked_indeterminate_stem_still_closes_bind(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """r6 A (Hermes P1, probe-reproduced): a TRACKED-but-INDETERMINATE stem match
    spent the park as an immediate close; late registration then RETRACTED the
    unbound stem, and the later bind's resumed relight popped that tombstone with
    pending_park EMPTY → live to TTL. RETAIN-ALWAYS (rule 1) dual-writes the
    named copy, so the drain + bind re-apply the park and the key closes."""
    import time

    route = await _bind_idle_route(scenario)
    mon, sub_dir, _append = _make_hybrid_monitor(tmp_path)
    name = "explore-backend"
    key = _KEY
    base = time.time() - 30

    # A MID-WRITE (indeterminate) same-name stem, PRE-TRACKED.
    sc = sub_dir / f"agent-{key}.jsonl"
    sc.write_text('{"type":"assistant","timestamp":"' + _iso_utc(base))  # no \n
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())

    # ONE batch: spawn RESULT (stashed) → genuine park → late Agent use.
    pair = _spawn_pair_result_first(name, _iso_utc(base))
    t_park = base + 5
    _append([pair[0]])
    _append([_park_entry(name, _iso_utc(t_park), _iso_utc(t_park))])
    _append([pair[1]])
    await mon.check_for_updates({_SID})
    await mon.check_sidechain_updates({_SID})  # stem still mid-write → no bind
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    rec = mon._teammate_registry[_SID][name]
    assert rec.current_key is None
    assert rec.pending_park is not None  # rule 1: the named copy survived

    # The first line completes → the stem binds via the resumed relight AND the
    # drained park closes it.
    import json as _json
    import os

    with open(sc, "w") as f:
        f.write(
            _json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": []},
                    "timestamp": _iso_utc(base + 0.05),
                }
            )
            + "\n"
        )
    os.utime(sc, (base + 0.05, base + 0.05))
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    assert mon._teammate_registry[_SID][name].current_key == key
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False  # the park closed the relit key
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_park_absorbed_by_stale_tracked_stem_still_closes_fresh_bind(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """r6 B (Codex P1a, probe-reproduced): a STALE tracked same-name stem (old
    hex) absorbed the park as its own immediate close; the fresh key then bound
    with launched and NO park → 2h strand. RETAIN-ALWAYS keeps the named copy;
    the fresh bind closes."""
    import time

    route = await _bind_idle_route(scenario)
    mon, sub_dir, _append = _make_hybrid_monitor(tmp_path)
    name = "explore-backend"
    stale_key = f"a{name}-00ddc0ffee00dd00"
    fresh_key = _KEY
    base = time.time() - 30

    # The STALE same-name stem (old first entry + old mtime), PRE-TRACKED.
    _write_teammate_sidechain(sub_dir, stale_key, base - 100, base - 100)
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())

    # ONE batch: new spawn RESULT (stashed) → genuine park → late Agent use.
    pair = _spawn_pair_result_first(name, _iso_utc(base))
    t_park = base + 5
    _append([pair[0]])
    _append([_park_entry(name, _iso_utc(t_park), _iso_utc(t_park))])
    _append([pair[1]])
    await mon.check_for_updates({_SID})
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    rec = mon._teammate_registry[_SID][name]
    assert stale_key in rec.retired_keys  # gate-False → quarantined
    assert rec.pending_park is not None  # the named copy survived (rule 1)

    # The FRESH sidechain appears → binds (relights via the resumed lane, r7
    # item 3) + the drained park closes it.
    _write_teammate_sidechain(sub_dir, fresh_key, base + 0.05, base + 0.05)
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    assert mon._teammate_registry[_SID][name].current_key == fresh_key
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_two_stashed_generations_newest_park_carries_through_rotation(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """r6 C (Codex P1b, probe-reproduced): two stashed same-name generations —
    the buffer reduced to the newest park T4, gen-1's registration drained it,
    and gen-2's ROTATION blind-cleared pending_park → gen-2 bound without its
    close → 2h strand. Rule 2: rotation RE-FILTERS instead of clears (T4 >= the
    new spawned_ts carries), so the gen-2 bind closes with T4."""
    import time

    route = await _bind_idle_route(scenario)
    mon, sub_dir, _append = _make_hybrid_monitor(tmp_path)
    name = "explore-backend"
    base = time.time() - 60
    t1, t2, t3, t4 = base, base + 2, base + 10, base + 12

    # ONE batch: gen-1 result → park T2 → gen-2 result → park T4 → late gen-1
    # use → late gen-2 use (both spawns result-before-use).
    p1 = _spawn_pair_result_first(name, _iso_utc(t1), tool_id="tu_g1")
    p2 = _spawn_pair_result_first(name, _iso_utc(t3), tool_id="tu_g2")
    _append([p1[0]])
    _append([_park_entry(name, _iso_utc(t2), _iso_utc(t2))])
    _append([p2[0]])
    _append([_park_entry(name, _iso_utc(t4), _iso_utc(t4))])
    _append([p1[1]])
    _append([p2[1]])
    await mon.check_for_updates({_SID})
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    rec = mon._teammate_registry[_SID][name]
    assert rec.spawn_generation == 2
    # Rule 2: the newest park (T4 >= gen-2 spawned_ts T3) CARRIED through the
    # rotation instead of being blind-cleared.
    assert rec.pending_park is not None
    assert rec.pending_park.ts is not None and rec.pending_park.ts >= t3

    # The genuine gen-2 sidechain binds → the carried T4 closes it.
    key = _KEY
    _write_teammate_sidechain(sub_dir, key, t3 + 0.05, t3 + 0.05)
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    assert mon._teammate_registry[_SID][name].current_key == key
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False  # closed by T4, not stranded
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_orphan_wake_after_park_keeps_bind_live(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """r6 D (Codex P2, probe-reproduced): spawn T1 → park T2 → wake T3 → late use
    → bind. Pre-fix the pre-registration wake was DROPPED, so the drained park
    tombstoned the key although T3 proved the teammate resumed — false-dark AND a
    broken transcript-true arbitration. Rule 3 retains the wake beside the park
    (the same pending pair the rec slots use); the bind applies both and the
    runtime resume gate arbitrates: park T2 <= wake T3 → suppressed → LIVE."""
    import time

    route = await _bind_idle_route(scenario)
    mon, sub_dir, _append = _make_hybrid_monitor(tmp_path)
    name = "explore-backend"
    key = _KEY
    base = time.time() - 30
    t1, t2, t3 = base, base + 2, base + 4

    # ONE batch: spawn RESULT (stashed) → park T2 → an IN-ORDER SendMessage wake
    # T3 (routing.target + input.to cross-check passes) → the late Agent use.
    pair = _spawn_pair_result_first(name, _iso_utc(t1))
    _append([pair[0]])
    _append([_park_entry(name, _iso_utc(t2), _iso_utc(t2))])
    _append(
        [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu_wk",
                            "name": "SendMessage",
                            "input": {"to": name},
                        }
                    ]
                },
                "sessionId": _SID,
                "timestamp": _iso_utc(t3),
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tu_wk", "content": "ok"}
                    ]
                },
                "sessionId": _SID,
                "timestamp": _iso_utc(t3),
                "toolUseResult": {
                    "success": True,
                    "routing": {"sender": "team-lead", "target": f"@{name}"},
                },
            },
        ]
    )
    _append([pair[1]])
    await mon.check_for_updates({_SID})
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    rec = mon._teammate_registry[_SID][name]
    assert rec.pending_wake is not None and rec.pending_wake >= t3 - 0.01
    assert rec.pending_park is not None  # both signals drained

    # The sidechain binds → wake T3 rides resumed, park T2 <= T3 is suppressed
    # by the runtime resume gate → the key stays LIVE (transcript-true).
    _write_teammate_sidechain(sub_dir, key, t1 + 0.05, t1 + 0.05)
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    assert mon._teammate_registry[_SID][name].current_key == key
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True  # the wake wins — no false-dark
    assert snap.background_agents == (key,)

    # A genuine later park (> T3) still closes it.
    _append([_park_entry(name, _iso_utc(base + 60), _iso_utc(base + 60))])
    await mon.check_for_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    assert route_runtime.snapshot(route).typing_eligible is False


@pytest.mark.asyncio
async def test_item1_pregeneration_wake_cannot_revive_parked_newer_generation(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """r7 item 1 (Hermes P1, probe-reproduced, cross-batch retro-pair): a
    RESULT-BEFORE-USE wake stashed with a gen-1-era ts T_w is retro-paired onto
    the BOUND gen-2 key AFTER gen-2 parked. Pre-fix the registered-rec wake path
    sent T_w straight to ``resumed[current_key]`` (no generation filter), the
    runtime resume pop revived the parked gen-2 key → false typing until the next
    park / the 2h TTL. The r7 fix applies ``_generation_filter_wake`` at the START
    of the registered path: T_w < gen-2 ``spawned_ts`` → REFUSED → the parked key
    stays dark."""
    import time

    route = await _bind_idle_route(scenario)
    mon, sub_dir, _append = _make_hybrid_monitor(tmp_path)
    name = "explore-backend"
    k1 = f"a{name}-11111111aaaaaaaa"  # gen-1 key
    k2 = _KEY  # gen-2 key
    base = time.time() - 90
    t_s1, t_s2 = base, base + 30  # gen-1 / gen-2 spawn instants
    t_w = base + 5  # the wake ts: gen-1-era, STRICTLY BELOW t_s2

    # gen-1 spawn (in-order) + gen-1 sidechain binds k1 → typing ON.
    _append(_spawn_pair(name, _iso_utc(t_s1), tool_id="g1"))
    await mon.check_for_updates({_SID})
    _write_teammate_sidechain(sub_dir, k1, t_s1 + 0.05, t_s1 + 0.05)
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    assert mon._teammate_registry[_SID][name].current_key == k1
    assert route_runtime.snapshot(route).typing_eligible is True

    # gen-2 respawn (rotation) at t_s2 → gen-2; gen-2 sidechain binds k2 → ON.
    _append(_spawn_pair(name, _iso_utc(t_s2), tool_id="g2"))
    await mon.check_for_updates({_SID})
    _write_teammate_sidechain(sub_dir, k2, t_s2 + 0.05, t_s2 + 0.05)
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    rec = mon._teammate_registry[_SID][name]
    assert rec.spawn_generation == 2 and rec.current_key == k2
    assert route_runtime.snapshot(route).typing_eligible is True

    # gen-2 park (>= t_s2) → k2 tombstoned → typing OFF.
    _append([_park_entry(name, _iso_utc(t_s2 + 20), _iso_utc(t_s2 + 20))])
    await mon.check_for_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    assert route_runtime.snapshot(route).typing_eligible is False
    assert route_runtime.snapshot(route).background_agents == ()

    # A STALE gen-1-era wake T_w delivered late via retro-pairing (the wake
    # tool_result flushes BEFORE its SendMessage tool_use, so it is stashed at
    # T_w, then applied to the BOUND gen-2 rec when the tool_use arrives).
    wake_result = {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_late_wake", "content": "ok"}
            ]
        },
        "sessionId": _SID,
        "timestamp": _iso_utc(t_w),
        "toolUseResult": {
            "success": True,
            "routing": {"sender": "team-lead", "target": f"@{name}"},
        },
    }
    wake_use = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_late_wake",
                    "name": "SendMessage",
                    "input": {"to": name},
                }
            ]
        },
        "sessionId": _SID,
        "timestamp": _iso_utc(t_s2 + 25),  # delivered NOW (well after gen-2)
    }
    _append([wake_result])  # result first → stashed at T_w
    _append([wake_use])  # the late use → retro-pairs the wake onto k2
    await mon.check_for_updates({_SID})
    act = mon.pop_sidechain_activity()
    # The pre-generation wake is REFUSED — no resume for the parked key.
    assert k2 not in act.get(_SID, ParentSidechainActivity()).resumed
    await bot_module.apply_sidechain_activity(act)
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False  # the parked gen-2 key was NOT revived
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_item2_bound_old_generation_does_not_consume_new_generation_park(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """r7 item 2 (Codex P1, probe-reproduced): gen-1 bound → a gen-2 spawn RESULT
    stashed (result-before-use) → the gen-2 park T4 arrives while the rec is STILL
    gen-1 (T4 >= gen-1 spawned_ts, so not dropped) → pre-fix it applied ONLY to
    gen-1's current_key immediately and was GONE → the late gen-2 tool_use rotates
    with pending_park=None → gen-2 binds LIVE → 2h strand. The r7 fix (universal
    park dual-write) retains a NAMED copy of EVERY park; the rotation's drain
    generation-filters T4 (>= gen-2 spawned_ts) into pending_park → the gen-2 bind
    closes with T4."""
    import time

    route = await _bind_idle_route(scenario)
    mon, sub_dir, _append = _make_hybrid_monitor(tmp_path)
    name = "explore-backend"
    k1 = f"a{name}-11111111aaaaaaaa"  # gen-1 key
    k2 = _KEY  # gen-2 key
    base = time.time() - 90
    t1, t3, t4 = base, base + 30, base + 35  # gen-1 spawn, gen-2 spawn, gen-2 park

    # gen-1 spawn (in-order) + gen-1 sidechain binds k1 → typing ON.
    _append(_spawn_pair(name, _iso_utc(t1), tool_id="g1"))
    await mon.check_for_updates({_SID})
    _write_teammate_sidechain(sub_dir, k1, t1 + 0.05, t1 + 0.05)
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    assert mon._teammate_registry[_SID][name].current_key == k1
    assert route_runtime.snapshot(route).typing_eligible is True

    # ONE batch: gen-2 spawn RESULT (stashed) → gen-2 park T4 (rec still gen-1) →
    # the late gen-2 Agent tool_use (registers/rotates → drains the retained T4).
    g2 = _spawn_pair_result_first(name, _iso_utc(t3), tool_id="g2")
    _append([g2[0]])
    _append([_park_entry(name, _iso_utc(t4), _iso_utc(t4))])
    _append([g2[1]])
    await mon.check_for_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    rec = mon._teammate_registry[_SID][name]
    assert rec.spawn_generation == 2 and rec.current_key is None
    # The retained copy of T4 drained into gen-2's pending_park (NOT lost to the
    # gen-1 immediate close).
    assert rec.pending_park is not None
    assert rec.pending_park.ts is not None and rec.pending_park.ts >= t3

    # The genuine gen-2 sidechain binds → the carried T4 closes it (NOT stranded).
    _write_teammate_sidechain(sub_dir, k2, t3 + 0.05, t3 + 0.05)
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    assert mon._teammate_registry[_SID][name].current_key == k2
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False  # closed by T4, not the 2h strand
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_item3_rotation_cleared_provenance_bind_stays_live(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """r7 item 3 probe (i) (Codex P2, probe-reproduced): a stem retracted during
    late gen-1 registration (tombstoned in the runtime) that becomes gate-positive
    and BINDS during the gen-2 rotation. Pre-fix the rotation CLEARED the r6
    ``done_retracted_keys`` provenance set, so the bind emitted ``launched`` which
    no-ops against the still-live runtime tombstone → the positively-bound key ran
    DARK. The r7 fix (always-resumed bind) pops the tombstone on every bind → the
    bound key is LIVE."""
    import json as _json
    import os
    import time

    route = await _bind_idle_route(scenario)
    mon, sub_dir, _append = _make_hybrid_monitor(tmp_path)
    name = "explore-backend"
    key = _KEY
    base = time.time() - 90
    t_s1, t_s2 = base, base + 30  # gen-1 / gen-2 spawn instants
    first_ts = t_s2 + 0.05  # the stem's genuine gen-2-era first-entry ts

    # A same-name stem, tracked but INDETERMINATE (mid-write, no newline) so gen-1
    # registration RETRACTS it (unbound) rather than binding it.
    sc = sub_dir / f"agent-{key}.jsonl"
    sc.write_text('{"type":"assistant","timestamp":"' + _iso_utc(first_ts))  # no \n
    os.utime(sc, (first_ts, first_ts))
    await mon.check_sidechain_updates({_SID})  # register at EOF (indeterminate)
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())

    # gen-1 spawn → registration retracts the unbound indeterminate stem →
    # the runtime tombstones the key.
    _append(_spawn_pair(name, _iso_utc(t_s1), tool_id="g1"))
    await mon.check_for_updates({_SID})
    act1 = mon.pop_sidechain_activity()
    assert key in act1[_SID].retraction_dones  # retracted at gen-1 registration
    await bot_module.apply_sidechain_activity(act1)  # key now tombstoned
    assert mon._teammate_registry[_SID][name].current_key is None
    assert route_runtime.snapshot(route).background_agents == ()  # dark (tombstoned)

    # The stem's first line COMPLETES (a genuine gen-2-era file, first entry >=
    # t_s2 - the gen>=2 tolerance).
    with open(sc, "w") as f:
        f.write(
            _json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": []},
                    "timestamp": _iso_utc(first_ts),
                }
            )
            + "\n"
        )
    os.utime(sc, (first_ts, first_ts))

    # gen-2 respawn (rotation clears the r6 provenance set) → the pre-spawn scan
    # binds the now-complete stem BEFORE the gen-2 retraction runs (so it is NOT
    # re-retracted). The bind must relight through the RESUMED lane (pop the
    # gen-1 tombstone), never the tombstone-blocked launched.
    _append(_spawn_pair(name, _iso_utc(t_s2), tool_id="g2"))
    await mon.check_for_updates({_SID})
    act2 = mon.pop_sidechain_activity()
    rec = mon._teammate_registry[_SID][name]
    assert rec.spawn_generation == 2 and rec.current_key == key
    assert act2[_SID].launched == set()  # NOT the tombstone-blocked lane
    assert act2[_SID].resumed == {
        key: rec.spawned_ts - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }
    await bot_module.apply_sidechain_activity(act2)
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True  # the tombstone was popped → LIVE
    assert snap.background_agents == (key,)


@pytest.mark.asyncio
async def test_item3_dual_write_tombstoned_never_retracted_bind_stays_live(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """r7 item 3 probe (ii) (Codex P2, probe-reproduced): a STALE no-registry park
    tombstones an already-tracked eventual key via the dual-write fallback's
    immediate all-stems close (the key was NEVER retracted, so it never entered
    the r6 ``done_retracted_keys`` set); the retained copy is generation-DROPPED at
    the drain, so pending_park is empty. Pre-fix the bind emitted ``launched``
    which no-ops against the stale-park tombstone → DARK. The r7 fix (always-
    resumed bind) pops the tombstone → LIVE, and the generation-dropped park never
    re-closes it."""
    import time

    route = await _bind_idle_route(scenario)
    mon, sub_dir, _append = _make_hybrid_monitor(tmp_path)
    name = "explore-backend"
    key = _KEY
    base = time.time() - 90
    t_stale, t_s = base, base + 30  # stale park (prior leg), then the spawn
    first_ts = t_s + 0.05  # the tracked stem is a genuine gen-1 (t_s)-era file

    # A genuine same-name stem, tracked (complete) before the spawn. Its first
    # entry is t_s-era, so the spawn's pre-spawn scan binds it.
    _write_teammate_sidechain(sub_dir, key, first_ts, first_ts)
    await mon.check_sidechain_updates({_SID})  # track (legacy, no registry yet)
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())

    # A STALE park (park_ts < the eventual spawned_ts) arrives with NO registry
    # rec → the no-registry fallback closes ALL matched tracked stems immediately
    # → the tracked key is TOMBSTONED (never retracted). Its retained copy will be
    # generation-dropped at the drain.
    _append([_park_entry(name, _iso_utc(t_stale), _iso_utc(t_stale))])
    await mon.check_for_updates({_SID})
    park_act = mon.pop_sidechain_activity()
    assert key in park_act[_SID].teammate_parks  # immediate all-stems close
    await bot_module.apply_sidechain_activity(park_act)  # key tombstoned
    assert route_runtime.snapshot(route).background_agents == ()  # dark

    # The spawn registers (t_s > t_stale) → drains the retained STALE park
    # (generation-dropped, < spawned_ts) → pending_park empty → the pre-spawn
    # scan binds the tracked key via the RESUMED lane (pops the tombstone).
    _append(_spawn_pair(name, _iso_utc(t_s), tool_id="g1"))
    await mon.check_for_updates({_SID})
    act = mon.pop_sidechain_activity()
    rec = mon._teammate_registry[_SID][name]
    assert rec.current_key == key
    assert rec.pending_park is None  # the stale park was generation-dropped
    assert act[_SID].launched == set()
    assert act[_SID].resumed == {
        key: rec.spawned_ts - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }
    assert key not in act[_SID].teammate_parks  # no re-close
    await bot_module.apply_sidechain_activity(act)
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True  # tombstone popped → LIVE (not dark)
    assert snap.background_agents == (key,)


async def _bind_gen1_within_tolerance(scenario, tmp_path, name, key, first_gap):
    """Bind a gen-1 teammate whose sidechain first entry is ``first_gap`` seconds
    BELOW the spawn event ts (within the gen-1 5s mtime-skew tolerance) — the r8
    item-1 look-alike shape. Returns ``(route, mon, sub_dir, _append, spawned_ts,
    first_ts)`` with the key BOUND + relit (typing ON)."""
    import os
    import time

    route = await _bind_idle_route(scenario)
    mon, sub_dir, _append = _make_hybrid_monitor(tmp_path)
    spawned_ts = time.time() - 60
    first_ts = spawned_ts - first_gap  # BELOW spawned_ts, within the 5s skew

    _append(_spawn_pair(name, _iso_utc(spawned_ts)))
    await mon.check_for_updates({_SID})
    # mtime within the 5s skew so the mtime prefilter passes; first entry below.
    sc = _write_teammate_sidechain(sub_dir, key, first_ts, spawned_ts - 1.0)
    os.utime(sc, (spawned_ts - 1.0, spawned_ts - 1.0))
    # The bind floors against the ISO-serialized first entry (ms-rounded), so read
    # it back the SAME way the code does.
    first_ts_read = mon._read_first_entry_ts(sc)
    assert first_ts_read is not None
    await mon.check_sidechain_updates({_SID})
    act = mon.pop_sidechain_activity()
    rec = mon._teammate_registry[_SID][name]
    assert rec.current_key == key
    # r8 item 1: the resume ts is floored at the bound file's OWN first entry.
    assert act[_SID].resumed == {
        key: min(rec.spawned_ts, first_ts_read) - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }
    assert act[_SID].resumed[key] < first_ts_read
    await bot_module.apply_sidechain_activity(act)
    assert route_runtime.snapshot(route).typing_eligible is True
    return route, mon, sub_dir, _append, rec.spawned_ts, first_ts_read


@pytest.mark.asyncio
async def test_item1_prespawn_endturn_on_within_tolerance_bind_goes_dark(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """r8 item 1 (Hermes P1, probe-reproduced): a look-alike candidate binds within
    the gen-1 mtime-skew tolerance with a first entry BELOW ``spawned_ts`` (here
    2s below). Its TRAILING sidechain end_turn is also pre-spawn (≥ the file's own
    first entry, ≤ spawned_ts). Pre-fix the r7 always-resumed stamp of
    ``spawned_ts - ε`` SHIELDED that end_turn at the runtime SIDECHAIN done gate
    (``end_turn_ts <= resumed_event_ts`` keeps the key LIVE) → the key stranded
    LIVE for the full 2h TTL, where the pre-r7 ``launched`` path fail-closed to
    DONE. The r8 fix floors the resume at ``min(spawned_ts, first_ts) - ε`` — below
    the bound file's own first entry — so the trailing end_turn is STRICTLY NEWER
    and TOMBSTONES → the key goes DARK, not live."""
    name = "explore-backend"
    key = _KEY
    route, mon, _sub, _append, spawned_ts, first_ts = await _bind_gen1_within_tolerance(
        scenario, tmp_path, name, key, first_gap=2.0
    )

    # A trailing SIDECHAIN end_turn at the file's first entry (pre-spawn, ≥ first
    # entry) MUST tombstone the key (not be shielded by the relight resume).
    end_turn_ts = first_ts  # ≥ the bound file's first entry, well below spawned_ts
    assert end_turn_ts < spawned_ts
    await bot_module.apply_sidechain_activity(
        {
            _SID: ParentSidechainActivity(
                ticks={
                    key: SidechainTick(
                        max_event_ts=end_turn_ts,
                        saw_end_of_turn=True,
                        max_end_turn_ts=end_turn_ts,
                    )
                }
            )
        }
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False  # DARK — the pre-spawn end_turn closed it
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_item1_genuine_post_spawn_park_still_closes_within_tolerance_bind(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """r8 item 1 CONTROL: the floor never over-suppresses a GENUINE close. Same
    below-spawn bind (first entry 0.5s below spawned_ts), but a genuine POST-spawn
    park (ts > spawned_ts > the resume floor) still tombstones → typing drops. The
    r3 tie fix is preserved (a park at exactly spawned_ts stays strictly newer than
    the resume floor and closes)."""
    import time

    name = "explore-backend"
    key = _KEY
    (
        route,
        mon,
        _sub,
        _append,
        spawned_ts,
        _first_ts,
    ) = await _bind_gen1_within_tolerance(scenario, tmp_path, name, key, first_gap=0.5)

    # A genuine park AFTER the spawn closes the relit key.
    park_ts = spawned_ts + 5
    _append([_park_entry(name, _iso_utc(park_ts), _iso_utc(time.time()))])
    await mon.check_for_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False  # genuine post-spawn park closed it
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_item1_normal_bind_floor_reduces_to_spawned_ts_minus_eps(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """r8 item 1 CONTROL (normal case, the byte-for-byte r7 preservation): a bind
    whose first entry is AFTER the spawn (the measured 1–7 ms real shape) floors at
    ``spawned_ts - ε`` (``min(spawned_ts, first_ts) == spawned_ts``), and a stale
    prior-leg sidechain end_turn (≤ spawned_ts - ε, the r7-accepted vanishingly-rare
    residual) still keeps the key LIVE while a genuine post-spawn end_turn closes
    it."""
    import os
    import time

    route = await _bind_idle_route(scenario)
    mon, sub_dir, _append = _make_hybrid_monitor(tmp_path)
    name = "explore-backend"
    key = _KEY
    spawned_ts = time.time() - 60
    first_ts = spawned_ts + 0.05  # AFTER the spawn (the normal real shape)

    _append(_spawn_pair(name, _iso_utc(spawned_ts)))
    await mon.check_for_updates({_SID})
    sc = _write_teammate_sidechain(sub_dir, key, first_ts, first_ts)
    os.utime(sc, (first_ts, first_ts))
    await mon.check_sidechain_updates({_SID})
    act = mon.pop_sidechain_activity()
    rec = mon._teammate_registry[_SID][name]
    # min(spawned_ts, first_ts) == spawned_ts → r7-identical resume ts.
    assert act[_SID].resumed == {
        key: rec.spawned_ts - TEAMMATE_RETRACT_RESUME_EPSILON_S
    }
    await bot_module.apply_sidechain_activity(act)
    assert route_runtime.snapshot(route).typing_eligible is True

    # A genuine post-spawn sidechain end_turn (ts > resume floor) closes it.
    await bot_module.apply_sidechain_activity(
        {
            _SID: ParentSidechainActivity(
                ticks={
                    key: SidechainTick(
                        max_event_ts=spawned_ts + 10,
                        saw_end_of_turn=True,
                        max_end_turn_ts=spawned_ts + 10,
                    )
                }
            )
        }
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()


# ── GH #59: a teammate's OWN run_in_background Bash launch is sidechain-only ──
#
# The T1.2 structured background-Bash lane reads only the PARENT transcript, so a
# bound teammate that launches a background bash (a long pytest suite) then PARKS
# went typing/🟡 dark for the whole post-park run — the bash ran unseen while the
# teammate stem key was (correctly) tombstoned by the park (GH #46). The fix
# records the SAME bare bash key from the run-state-authoritative teammate
# sidechain; the EXISTING parent queue-op close + the 2 h is_background TTL close
# it, and the teammate PARK closes ONLY the stem key, so the bash key survives.

from pathlib import Path as _Path  # noqa: E402

_GH59_FIXTURE = (
    _Path(__file__).resolve().parent.parent
    / "cctelegram"
    / "fixtures"
    / "teammate_sidechain_bash_v2.1.211.jsonl"
)
# The incident sidechain stem == fixture agentId; normalized key drops "agent-".
_GH59_STEM = "avis2-backend-7041d9b743d26f2e"
_GH59_NAME = "vis2-backend"
_GH59_BASH_KEY = "bgbnvxcbx"


def _gh59_fixture_entries() -> list[dict]:
    import json as _json

    return [
        _json.loads(line)
        for line in _GH59_FIXTURE.read_text().splitlines()
        if line.strip()
    ]


def _queue_op_close_entry(task_id: str, entry_ts: str) -> dict:
    """The INCIDENT's busy-parent ``queue-operation``/``enqueue`` close shape
    (parent transcript line 428, CC 2.1.211): the ``<task-notification>`` rides
    a TOP-LEVEL ``content`` string on a ``type:"queue-operation"`` entry — no
    ``message`` — which ``transcript_parser`` SYNTHESIZES into the same
    ``lifecycle_only`` user-text entry (``utils.is_task_notification`` gated),
    so the parent lane extracts its ``<task-id>`` into ``rec.completed`` → a
    PARENT unconditional tombstone."""
    return {
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": entry_ts,
        "sessionId": _SID,
        "content": (
            "<task-notification>\n"
            f"<task-id>{task_id}</task-id>\n"
            "<tool-use-id>toolu_bgbash01</tool-use-id>\n"
            f"<output-file>/tmp/tasks/{task_id}.output</output-file>\n"
            "<status>completed</status>\n"
            '<summary>Background command "Run full agent suite in background" '
            "completed (exit code 0)</summary>\n"
            "</task-notification>"
        ),
    }


@pytest.mark.asyncio
async def test_gh59_incident_e2e_unregistered_bash_survives_park(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """T5: the INCIDENT end-to-end via the UNREGISTERED path (rec is None — the
    logs showed vis2-backend was unregistered, the in-memory registry not being
    restart-reconciled). The sidechain appears (no spawn parsed) → its OWN bash
    launch → PARK → (typing STAYS on because the bash key survives) → parent
    queue-op close → typing drops. RED against unmodified code at the
    "typing stays on after park" assertion (pre-fix the launch was never recorded,
    so the park's stem tombstone left the route dark)."""
    route = await _bind_idle_route(scenario)
    mon, sub_dir, _append = _make_hybrid_monitor(tmp_path)

    # UNREGISTERED teammate sidechain (legacy → feed_run_state True). Register at
    # EOF, then the OWN background-Bash launch batch appears.
    sc = sub_dir / f"agent-{_GH59_STEM}.jsonl"
    sc.write_text("")
    await mon.check_sidechain_updates({_SID})  # register at EOF
    with open(sc, "a") as f:
        import json as _json

        for e in _gh59_fixture_entries():
            f.write(_json.dumps(e) + "\n")
    await mon.check_sidechain_updates({_SID})
    act = mon.pop_sidechain_activity()
    assert _GH59_BASH_KEY in act[_SID].launched  # the fix records the bash key
    await bot_module.apply_sidechain_activity(act)

    # Both the stem key (via ticks) and the bash key (via launched) lift typing.
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True
    assert _GH59_BASH_KEY in snap.background_agents

    # PARK — the teammate reports idle; the park (ts NEWER than the stem's last
    # sidechain write) tombstones ONLY the stem key (GH #46).
    _append(
        [
            _park_entry(
                _GH59_NAME, "2026-07-16T19:32:46.000Z", "2026-07-16T19:32:46.000Z"
            )
        ]
    )
    await mon.check_for_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())

    # THE RED ASSERTION: the bash key SURVIVES the park → typing STAYS on.
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True
    assert snap.background_agents == (_GH59_BASH_KEY,)

    # PARENT QUEUE-OP CLOSE — the incident's actual close shape: the bash's
    # <task-notification> lands as a busy-parent queue-operation/enqueue entry
    # (the parser synthesizes the lifecycle_only user-text entry) and tombstones
    # the bash key (source=PARENT, unconditional) → typing drops.
    _append([_queue_op_close_entry(_GH59_BASH_KEY, "2026-07-16T19:43:22.797Z")])
    await mon.check_for_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_gh59_registered_spawn_bind_bash_survives_park(
    scenario: ScenarioHarness, tmp_path
) -> None:
    """T5 variant: the REGISTERED spawn→bind path. A teammate is spawned + bound,
    launches its OWN background bash on the BOUND sidechain (feed_run_state True
    for the bound current_key), then parks — the bash key survives the park."""
    import os
    import time

    route = await _bind_idle_route(scenario)
    mon, sub_dir, _append = _make_hybrid_monitor(tmp_path)

    # Spawn + bind: pre-discover the stem at EOF, then the spawn registers + binds.
    spawn_ts = time.time() - 30
    sc = sub_dir / f"agent-{_GH59_STEM}.jsonl"
    sc.write_text(
        __import__("json").dumps(
            {
                "type": "assistant",
                "message": {"content": []},
                "timestamp": _iso_utc(spawn_ts + 0.01),
            }
        )
        + "\n"
    )
    os.utime(sc, (spawn_ts + 0.01, spawn_ts + 0.01))
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())

    _append(_spawn_pair(_GH59_NAME, _iso_utc(spawn_ts)))
    await mon.check_for_updates({_SID})
    await mon.check_sidechain_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    assert mon._teammate_registry[_SID][_GH59_NAME].current_key == _GH59_STEM

    # The bound teammate launches its OWN background bash (structured meta).
    launch = {
        "type": "user",
        "message": {
            "content": [
                {
                    "tool_use_id": "toolu_bgbash01",
                    "type": "tool_result",
                    "content": "Command running in background with ID: bgbnvxcbx.",
                    "is_error": False,
                }
            ]
        },
        "sessionId": _SID,
        "isSidechain": True,
        "agentId": _GH59_STEM,
        "timestamp": _iso_utc(spawn_ts + 5),
        "toolUseResult": {
            "stdout": "",
            "stderr": "",
            "interrupted": False,
            "backgroundTaskId": _GH59_BASH_KEY,
        },
    }
    with open(sc, "a") as f:
        f.write(__import__("json").dumps(launch) + "\n")
    await mon.check_sidechain_updates({_SID})
    act = mon.pop_sidechain_activity()
    assert _GH59_BASH_KEY in act[_SID].launched  # bound → feed_run_state True
    await bot_module.apply_sidechain_activity(act)
    assert _GH59_BASH_KEY in route_runtime.snapshot(route).background_agents

    # PARK — closes the stem only; the bash key survives → typing stays on.
    _append([_park_entry(_GH59_NAME, _iso_utc(spawn_ts + 60), _iso_utc(spawn_ts + 60))])
    await mon.check_for_updates({_SID})
    await bot_module.apply_sidechain_activity(mon.pop_sidechain_activity())
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is True
    assert snap.background_agents == (_GH59_BASH_KEY,)


@pytest.mark.asyncio
async def test_gh59_fanout_same_tick_close_and_launch_nets_idle(
    scenario: ScenarioHarness,
) -> None:
    """T4 (ordering 1): a SAME-tick close+launch — the fan-out always applies
    launched BEFORE completed, so the key is born tombstoned-in-effect → idle."""
    route = await _bind_idle_route(scenario)
    await bot_module.apply_sidechain_activity(
        {
            _SID: ParentSidechainActivity(
                launched={_GH59_BASH_KEY}, completed={_GH59_BASH_KEY}
            )
        }
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()


@pytest.mark.asyncio
async def test_gh59_fanout_earlier_tick_close_then_launch_noops(
    scenario: ScenarioHarness,
) -> None:
    """T4 (ordering 2): a close applied in an EARLIER tick tombstones the key;
    a later launch NO-OPS against the tombstone → idle."""
    route = await _bind_idle_route(scenario)
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(completed={_GH59_BASH_KEY})}
    )
    await bot_module.apply_sidechain_activity(
        {_SID: ParentSidechainActivity(launched={_GH59_BASH_KEY})}
    )
    snap = route_runtime.snapshot(route)
    assert snap.typing_eligible is False
    assert snap.background_agents == ()
